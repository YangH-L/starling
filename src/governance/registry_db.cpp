// registry_db.cpp — 见 include/starling/governance/registry_db.hpp。

#include "starling/governance/registry_db.hpp"

#include "registry_db_internal.hpp"
#include "world_store_internal.hpp"

#include "starling/governance/world_settings.hpp"
#include "starling/persistence/connection.hpp"
#include "starling/persistence/sqlite_adapter.hpp"
#include "starling/persistence/sqlite_helpers.hpp"
#include "starling/persistence/stmt.hpp"

#include <sqlite3.h>

#include <algorithm>
#include <array>
#include <functional>
#include <memory>
#include <mutex>
#include <optional>
#include <ranges>
#include <string>
#include <system_error>
#include <utility>
#include <vector>

namespace starling::governance {

namespace fs = std::filesystem;
namespace pst = starling::persistence;

namespace {

constexpr int kSchemaVersion = 1;
constexpr int kDefaultRetention = 5;
constexpr std::int64_t kMaxPageLimit = 500;

// 建表 SQL。世界目录用 dir_id 命名，world_labels / snapshots 按世界名关联。
// snapshots.seq 是登记顺序（全表递增），snapshots() 按它降序列出：「最新」指最后登记的那份，与
// worlds.json 时代「插到列表开头」一致。不用 created_at 排：同一秒拍的几份 created_at 相同，id 的
// 后缀是随机的，按 id 兜底会让刚拍的那份排到后面、被保留策略当成最旧的删掉。
constexpr const char* kSchemaSql = R"sql(
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS worlds (
    name TEXT PRIMARY KEY,
    dir_id TEXT NOT NULL UNIQUE,
    tenant TEXT NOT NULL,
    db_path TEXT NOT NULL,
    snapshot_dir TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    seed INTEGER NOT NULL DEFAULT 0,
    read_only INTEGER NOT NULL DEFAULT 0,
    reclaimable INTEGER NOT NULL DEFAULT 0,
    retention_group TEXT NOT NULL DEFAULT '',
    fork_of_world TEXT NOT NULL DEFAULT '',
    fork_of_snapshot TEXT NOT NULL DEFAULT '',
    settings_json TEXT NOT NULL DEFAULT '{}',
    holders INTEGER NOT NULL DEFAULT 0,
    statements INTEGER NOT NULL DEFAULT 0,
    last_write_at TEXT NOT NULL DEFAULT '',
    stats_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS world_labels (
    name TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (name, key)
);
CREATE INDEX IF NOT EXISTS idx_world_labels_kv ON world_labels (key, value);
CREATE TABLE IF NOT EXISTS snapshots (
    world TEXT NOT NULL,
    id TEXT NOT NULL,
    path TEXT NOT NULL,
    kind TEXT NOT NULL,
    reason TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    world_time TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    sessions_complete INTEGER,
    session_id TEXT NOT NULL DEFAULT '',
    content_mark TEXT NOT NULL DEFAULT '',
    seq INTEGER NOT NULL,
    digest TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (world, id)
);
)sql";

// 第 3 期给 snapshots 加了 digest 列，schema_version 仍是 1：之前建的注册表（含用户 8787 的
// ~/.starling/registry.db）在 CREATE TABLE IF NOT EXISTS 之后还是旧形状，打开时就地补列。只加一列、
// 带默认值，旧条目读出空串（rollback_world 对空 digest 不比较）。列已在时什么都不做，所以幂等。
// 调用方已在 BEGIN IMMEDIATE 之内（RegistryDb::open）：查与补之间别的连接插不进来。
void add_snapshot_digest_column(pst::Connection& conn) {
    bool present = false;
    {
        pst::Stmt cols(conn.raw(), "SELECT 1 FROM pragma_table_info('snapshots') WHERE name = 'digest'");
        present = cols.step_row();
    }
    if (!present) { conn.exec("ALTER TABLE snapshots ADD COLUMN digest TEXT NOT NULL DEFAULT ''"); }
}

// 裁定 R40、R51：合并结果里每个继承来的 endpoint（provider_name 非空）对 provider 此刻的种类与地址核对
// （require_endpoint_unchanged，spec §8.3）。表里没有这个 provider（已被删掉）同样拒绝。
// 调用方持注册表锁、在写之前调用：抛 endpoint_changed 时什么都没写。
void require_inherited_endpoints(const WorldSettings& settings, const ProviderEndpoints& current) {
    const std::array<std::pair<const char*, const std::optional<EndpointSettings>*>, 4> roles{{
        {"extraction", &settings.extraction},
        {"embedding", &settings.embedding},
        {"chat", &settings.chat},
        {"consolidation", &settings.consolidation},
    },};
    for (const auto& [role, endpoint] : roles) {
        if (!endpoint->has_value() || (*endpoint)->provider_name.empty()) { continue; }
        const auto found = current.find((*endpoint)->provider_name);
        require_endpoint_unchanged(**endpoint,
                                   found == current.end()
                                       ? std::optional<ProviderEndpoint>()
                                       : std::optional<ProviderEndpoint>(found->second),
                                   role);
    }
}

constexpr const char* kWorldColumns =
    "name, dir_id, tenant, db_path, snapshot_dir, label, created_at, seed, read_only, "
    "reclaimable, retention_group, fork_of_world, fork_of_snapshot, settings_json, holders, "
    "statements, last_write_at, stats_at";

std::string text_col(sqlite3_stmt* stmt, int col) {
    // NOLINTNEXTLINE(cppcoreguidelines-pro-type-reinterpret-cast)
    const auto* txt = reinterpret_cast<const char*>(sqlite3_column_text(stmt, col));
    return txt == nullptr ? std::string() : std::string(txt);
}

WorldRecord read_world(sqlite3_stmt* stmt) {
    WorldRecord rec;
    rec.name = text_col(stmt, 0);
    rec.dir_id = text_col(stmt, 1);
    rec.tenant = text_col(stmt, 2);
    rec.db_path = text_col(stmt, 3);
    rec.snapshot_dir = text_col(stmt, 4);
    rec.label = text_col(stmt, 5);
    rec.created_at = text_col(stmt, 6);
    rec.seed = sqlite3_column_int(stmt, 7) != 0;
    rec.read_only = sqlite3_column_int(stmt, 8) != 0;
    rec.reclaimable = sqlite3_column_int(stmt, 9) != 0;
    rec.retention_group = text_col(stmt, 10);
    rec.fork_of_world = text_col(stmt, 11);
    rec.fork_of_snapshot = text_col(stmt, 12);
    rec.settings_json = text_col(stmt, 13);
    rec.holders = sqlite3_column_int64(stmt, 14);
    rec.statements = sqlite3_column_int64(stmt, 15);
    rec.last_write_at = text_col(stmt, 16);
    rec.stats_at = text_col(stmt, 17);
    return rec;
}

[[noreturn]] void registry_corrupt(const std::string& detail) {
    throw WorldStoreError("registry_corrupt", detail);
}

// 登记一份快照，seq 取全表最大值 + 1（调用方已持锁、在事务里）。SQLite 错误（撞主键等）原样抛出。
void insert_snapshot(sqlite3* dbh, const std::string& world, const SnapshotEntry& entry) {
    pst::Stmt stmt(dbh,
                   "INSERT INTO snapshots (world, id, path, kind, reason, note, created_at, "
                   "world_time, size_bytes, sessions_complete, session_id, content_mark, digest, seq) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                   "(SELECT COALESCE(MAX(seq), 0) + 1 FROM snapshots))");
    stmt.bind(world, entry.id, entry.path, entry.kind, entry.reason, entry.note, entry.created_at,
              entry.world_time, entry.size_bytes);
    if (entry.sessions_complete) {
        sqlite3_bind_int64(stmt.raw(), 10, *entry.sessions_complete);
    } else {
        sqlite3_bind_null(stmt.raw(), 10);
    }
    pst::detail::bind_sv(stmt.raw(), 11, entry.session_id);
    pst::detail::bind_sv(stmt.raw(), 12, entry.content_mark);
    pst::detail::bind_sv(stmt.raw(), 13, entry.digest);
    stmt.step_done();
}

void insert_meta(sqlite3* dbh, const char* key, const std::string& value) {
    pst::Stmt stmt(dbh, "INSERT INTO meta (key, value) VALUES (?, ?)");
    stmt.bind(key, value);
    stmt.step_done();
}

}  // namespace

namespace {
// seed 蕴含 read_only（spec §6.2）。
WorldFlags normalized_flags(WorldFlags flags) {
    if (flags.seed) { flags.read_only = true; }
    return flags;
}

// 只读世界的后台恒关（merge_settings 的 read_only）。不是只读时原样返回（create 的设置是不透明文本）。
std::string settings_for(const std::string& settings_json, const WorldFlags& flags) {
    if (!flags.read_only) { return settings_json; }
    return settings_to_json(merge_settings(settings_from_json(settings_json), "{}", true));
}

void require_valid_name(const std::string& name) {
    if (!is_valid_world_name(name)) {
        throw WorldStoreError("invalid_world_name",
                              "'" + name + "' does not match [A-Za-z0-9][A-Za-z0-9._-]{0,127}");
    }
}
}  // namespace

struct RegistryDb::Impl {
    fs::path data_dir;
    mutable std::mutex mutex;
    mutable std::unique_ptr<pst::Connection> conn;

    [[nodiscard]] sqlite3* db() const { return conn->raw(); }

    // 调用方已持锁。
    [[nodiscard]] std::map<std::string, std::string> load_labels(const std::string& name) const {
        std::map<std::string, std::string> out;
        pst::Stmt stmt(db(), "SELECT key, value FROM world_labels WHERE name = ?");
        stmt.bind(name);
        while (stmt.step_row()) {
            out.emplace(text_col(stmt.raw(), 0), text_col(stmt.raw(), 1));
        }
        return out;
    }

    [[nodiscard]] std::optional<WorldRecord> find(const std::string& name) const {
        const std::string sql = std::string("SELECT ") + kWorldColumns +
                                " FROM worlds WHERE name = ?";
        pst::Stmt stmt(db(), sql.c_str());
        stmt.bind(name);
        if (!stmt.step_row()) { return std::nullopt; }
        WorldRecord rec = read_world(stmt.raw());
        rec.labels = load_labels(name);
        return rec;
    }

    [[nodiscard]] WorldRecord require(const std::string& name) const {
        auto rec = find(name);
        if (!rec) { throw WorldStoreError("world_not_found", "no world named '" + name + "'"); }
        return *rec;
    }

    [[nodiscard]] bool dir_id_taken(const std::string& dir_id) const {
        pst::Stmt stmt(db(), "SELECT 1 FROM worlds WHERE dir_id = ?");
        stmt.bind(dir_id);
        return stmt.step_row();
    }

    // 调用方已持锁。
    [[nodiscard]] std::string default_name() const {
        pst::Stmt stmt(db(), "SELECT value FROM meta WHERE key = 'default_world'");
        return stmt.step_row() ? text_col(stmt.raw(), 0) : std::string("default");
    }

    // 调用方已持锁；world_not_found。
    void set_settings(const std::string& name, const std::string& settings_json) const {
        (void)require(name);
        pst::TransactionGuard guard(*conn);
        pst::Stmt stmt(db(), "UPDATE worlds SET settings_json = ? WHERE name = ?");
        stmt.bind(settings_json, name);
        stmt.step_done();
        guard.commit();
    }

    // 调用方已持锁，且 name 已过 is_valid_world_name。名字已被占用抛 world_exists；否则选一个注册表与
    // 磁盘上都没有的 dir_id，建 worlds/<dir_id>/ 与 snapshots/，返回世界目录（还没有注册项）。
    [[nodiscard]] fs::path reserve_home(const std::string& name) const {
        if (find(name)) { throw WorldStoreError("world_exists", "world '" + name + "' exists"); }
        const fs::path worlds_root = data_dir / "worlds";
        internal::require_worlds_root_inside(data_dir, worlds_root);
        std::string dir_id = internal::random_hex();
        while (dir_id_taken(dir_id) || fs::exists(worlds_root / dir_id)) {
            dir_id = internal::random_hex();
        }
        const fs::path home = worlds_root / dir_id;
        std::error_code mkdir_err;
        fs::create_directories(home / "snapshots", mkdir_err);
        if (mkdir_err) {
            throw WorldStoreError("snapshot_failed",
                                  "cannot create " + home.string() + ": " + mkdir_err.message());
        }
        return home;
    }

    // 调用方已持锁。在一个事务里写 worlds 行与标签；home 是 reserve_home 建的世界目录。
    void insert_world(const NewWorld& spec, const fs::path& home) const {
        const WorldFlags flags = normalized_flags(spec.flags);
        const std::string settings = settings_for(spec.settings_json, flags);
        pst::TransactionGuard guard(*conn);
        pst::Stmt stmt(db(),
                       "INSERT INTO worlds (name, dir_id, tenant, db_path, snapshot_dir, label, "
                       "created_at, settings_json, seed, read_only, reclaimable, retention_group, "
                       "fork_of_world, fork_of_snapshot) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)");
        stmt.bind(spec.name, home.filename().string(), spec.tenant, (home / "world.db").string(),
                  (home / "snapshots").string(), spec.label, spec.now_iso, settings,
                  static_cast<int>(flags.seed), static_cast<int>(flags.read_only),
                  static_cast<int>(flags.reclaimable), flags.retention_group, flags.fork_of_world,
                  flags.fork_of_snapshot);
        stmt.step_done();
        insert_labels(spec.name, spec.labels);
        guard.commit();
    }

    // 调用方已持锁，且 name 已过 is_valid_world_name。init_db 为真时（create_world，裁定 R34）先在新
    // 世界目录里经持久层打开一次库文件（迁移建表）再关掉，然后才写注册项：注册项一出现，库与 schema
    // 就已经在，检视新世界读到的是空表，不是「还没有库」。不建引擎（裁定 R21）。任何一步失败都删掉
    // 整个世界目录，注册项不写。租户就是世界名。
    WorldRecord create_row(const NewWorld& spec, bool init_db) const {
        const fs::path home = reserve_home(spec.name);
        try {
            if (init_db) { initialise_world_db(home / "world.db"); }
            insert_world(spec, home);
        } catch (...) {
            std::error_code rm_err;
            fs::remove_all(home, rm_err);
            throw;
        }
        return require(spec.name);
    }

    // 经持久层打开 db_file（SqliteAdapter::open 跑迁移到最新），随即关闭（并回 WAL）。失败抛
    // snapshot_failed（与建目录失败同一个码），由 create_row 删掉世界目录。
    static void initialise_world_db(const fs::path& db_file) {
        try {
            auto adapter = pst::SqliteAdapter::open(db_file);
            adapter->close();
        } catch (const WorldStoreError&) {
            throw;
        } catch (const std::exception& err) {
            throw WorldStoreError("snapshot_failed",
                                  "cannot initialise " + db_file.string() + ": " + err.what());
        }
    }

    void insert_labels(const std::string& name,
                       const std::map<std::string, std::string>& labels) const {
        for (const auto& [key, value] : labels) {
            if (key.empty()) { throw WorldStoreError("invalid_labels", "empty label key"); }
            pst::Stmt stmt(db(), "INSERT INTO world_labels (name, key, value) VALUES (?, ?, ?)");
            stmt.bind(name, key, value);
            stmt.step_done();
        }
    }
};

OpenMode decide_open_mode(const WorldRecord& rec, bool for_client_write) {
    if (rec.read_only || rec.seed) { return OpenMode::ReadOnly; }   // seed 蕴含 read_only
    if (rec.reclaimable && !for_client_write) { return OpenMode::ReadOnly; }
    return OpenMode::ReadWrite;
}

WorldSettings effective_world_settings(const WorldRecord& rec) {
    const bool read_only_open = decide_open_mode(rec, false) == OpenMode::ReadOnly;
    return merge_settings(settings_from_json(rec.settings_json), "{}", read_only_open);
}

RegistryDb::RegistryDb(std::unique_ptr<Impl> impl) : impl_(std::move(impl)) {}
RegistryDb::RegistryDb(RegistryDb&&) noexcept = default;
RegistryDb& RegistryDb::operator=(RegistryDb&&) noexcept = default;
RegistryDb::~RegistryDb() = default;

RegistryDb RegistryDb::open(const fs::path& data_dir, const fs::path& default_db_path,
                            const std::string& default_tenant) {
    auto impl = std::make_unique<Impl>();
    impl->data_dir = fs::absolute(data_dir);
    const fs::path file = impl->data_dir / "registry.db";
    try {
        std::error_code mkdir_err;
        fs::create_directories(impl->data_dir, mkdir_err);
        impl->conn = std::make_unique<pst::Connection>(pst::Connection::open(file));
        impl->conn->exec(kSchemaSql);
        pst::TransactionGuard guard(*impl->conn);
        // 在 BEGIN IMMEDIATE 之内查列、补列：两个进程同时第一次打开旧注册表时，后到的一个等前一个提交，
        // 再查就看得见新列，不会第二次 ALTER（duplicate column → registry_corrupt）。
        add_snapshot_digest_column(*impl->conn);
        std::optional<std::string> version;
        {
            pst::Stmt stmt(impl->db(), "SELECT value FROM meta WHERE key = 'schema_version'");
            if (stmt.step_row()) { version = text_col(stmt.raw(), 0); }
        }
        if (!version) {
            const fs::path home = impl->data_dir / "worlds" / "default";
            const std::string now = internal::iso_now();
            pst::Stmt meta(impl->db(), "INSERT INTO meta (key, value) VALUES (?, ?)");
            meta.bind("schema_version", std::to_string(kSchemaVersion));
            meta.step_done();
            pst::Stmt dflt(impl->db(), "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)");
            dflt.bind("default_world", "default");
            dflt.step_done();
            pst::Stmt keep(impl->db(), "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)");
            keep.bind("snapshot_retention", std::to_string(kDefaultRetention));
            keep.step_done();
            pst::Stmt world(impl->db(),
                            "INSERT OR IGNORE INTO worlds (name, dir_id, tenant, db_path, "
                            "snapshot_dir, label, created_at, settings_json) "
                            "VALUES ('default', 'default', ?, ?, ?, ?, ?, '{}')");
            world.bind(default_tenant, fs::absolute(default_db_path).string(),
                       (home / "snapshots").string(), "主世界", now);
            world.step_done();
        } else if (*version != std::to_string(kSchemaVersion)) {
            registry_corrupt("unsupported registry schema_version '" + *version + "' in " +
                             file.string());
        }
        guard.commit();
        // 默认世界必须在册（手改过库的话在此暴露）。
        if (!impl->find("default")) { registry_corrupt("default world missing in " + file.string()); }
    } catch (const pst::SqliteError& err) {
        registry_corrupt("cannot open registry " + file.string() + ": " + err.what());
    }
    return RegistryDb(std::move(impl));
}

RegistryDb RegistryDb::open_or_import(const fs::path& data_dir, const fs::path& default_db_path,
                                      const std::string& default_tenant) {
    const fs::path dir = fs::absolute(data_dir);
    // lstat 语义：悬空符号链接也算存在，与 import_worlds_json 一致。
    const auto exists = [](const fs::path& file) {
        std::error_code err;
        return fs::symlink_status(file, err).type() != fs::file_type::not_found;
    };
    if (exists(dir / "worlds.json") && !exists(dir / "registry.db")) {
        import_worlds_json(dir);
    }
    auto reg = open(dir, default_db_path, default_tenant);
    validate_registry(reg);
    return reg;
}

std::string RegistryDb::default_world() const {
    const std::scoped_lock lock(impl_->mutex);
    pst::Stmt stmt(impl_->db(), "SELECT value FROM meta WHERE key = 'default_world'");
    return stmt.step_row() ? text_col(stmt.raw(), 0) : std::string("default");
}

WorldRecord RegistryDb::get(const std::string& name) const {
    const std::scoped_lock lock(impl_->mutex);
    return impl_->require(name);
}

WorldPage RegistryDb::list(const WorldQuery& query) const {
    if (query.limit < 1 || query.limit > kMaxPageLimit || query.offset < 0) {
        throw WorldStoreError("invalid_world_query",
                              "limit must be 1-500 and offset >= 0 (got limit=" +
                                  std::to_string(query.limit) +
                                  ", offset=" + std::to_string(query.offset) + ")");
    }
    std::string where;
    for (std::size_t idx = 0; idx < query.labels.size(); ++idx) {
        where += where.empty() ? " WHERE " : " AND ";
        where += "EXISTS (SELECT 1 FROM world_labels l WHERE l.name = w.name AND l.key = ? "
                 "AND l.value = ?)";
    }
    const auto bind_labels = [&query](pst::Stmt& stmt) {
        int idx = 0;
        for (const auto& [key, value] : query.labels) {
            pst::detail::bind_sv(stmt.raw(), ++idx, key);
            pst::detail::bind_sv(stmt.raw(), ++idx, value);
        }
        return idx;
    };
    const std::scoped_lock lock(impl_->mutex);
    WorldPage page;
    {
        const std::string sql = "SELECT COUNT(*) FROM worlds w" + where;
        pst::Stmt stmt(impl_->db(), sql.c_str());
        bind_labels(stmt);
        if (stmt.step_row()) { page.total = sqlite3_column_int64(stmt.raw(), 0); }
    }
    // spec §14：按最近写入时刻降序（审查 M-1）。last_write_at 是统计缓存列（关闭时与每 30 秒刷新），空串 = 从没写过，
    // 排在最后；再按建世界时刻降序、名字升序兜底。空串在这一列里就是「没有值」（列 NOT NULL DEFAULT ''），所以用
    // last_write_at = '' 而不是 NULLS LAST。
    const std::string sql = std::string("SELECT ") + kWorldColumns + " FROM worlds w" + where +
                            " ORDER BY last_write_at = '' ASC, last_write_at DESC, created_at DESC, "
                            "name ASC LIMIT ? OFFSET ?";
    pst::Stmt stmt(impl_->db(), sql.c_str());
    const int used = bind_labels(stmt);
    sqlite3_bind_int64(stmt.raw(), used + 1, query.limit);
    sqlite3_bind_int64(stmt.raw(), used + 2, query.offset);
    while (stmt.step_row()) { page.worlds.push_back(read_world(stmt.raw())); }
    for (auto& rec : page.worlds) { rec.labels = impl_->load_labels(rec.name); }
    return page;
}

// NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
WorldRecord RegistryDb::create(const std::string& name, const std::string& label,
                               const std::map<std::string, std::string>& labels,
                               const std::string& settings_json, std::string_view now_iso,
                               const WorldFlags& flags) {
    require_valid_name(name);
    const std::scoped_lock lock(impl_->mutex);
    return impl_->create_row(NewWorld{.name = name, .tenant = name, .label = label, .labels = labels,
                                      .settings_json = settings_json,
                                      .now_iso = std::string(now_iso), .flags = flags,},
                             false);
}

// NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
WorldRecord RegistryDb::create_world(const std::string& name, const std::string& label,
                                     const std::map<std::string, std::string>& labels,
                                     const std::string& overrides_json, std::string_view now_iso,
                                     const std::optional<ProviderEndpoints>& provider_endpoints,
                                     const WorldFlags& flags) {
    require_valid_name(name);
    const WorldFlags normal = normalized_flags(flags);
    const std::scoped_lock lock(impl_->mutex);
    const auto base = settings_from_json(impl_->require(impl_->default_name()).settings_json);
    const auto merged =
        merge_settings(base, overrides_json.empty() ? "{}" : overrides_json, normal.read_only);
    if (provider_endpoints) { require_inherited_endpoints(merged, *provider_endpoints); }
    return impl_->create_row(NewWorld{.name = name, .tenant = name, .label = label, .labels = labels,
                                      .settings_json = settings_to_json(merged),
                                      .now_iso = std::string(now_iso), .flags = normal,},
                             true);
}

WorldRecord RegistryDb::create_populated(
    const NewWorld& spec, const std::function<void(const fs::path& db_file)>& populate) {
    require_valid_name(spec.name);
    fs::path home;
    {
        const std::scoped_lock lock(impl_->mutex);
        home = impl_->reserve_home(spec.name);
    }
    const auto discard = [&home] {
        std::error_code rm_err;
        fs::remove_all(home, rm_err);
    };
    try {
        populate(home / "world.db");   // 不持锁：复制可能要几百毫秒
        const std::scoped_lock lock(impl_->mutex);
        // 第 1 步之后名字可能被别的调用抢先登记（dir_id 不会撞：目录已经在，reserve_home 跳过它）。
        if (impl_->find(spec.name)) {
            throw WorldStoreError("world_exists", "world '" + spec.name + "' exists");
        }
        // 裁定 R31 M3：与 create_world 同一条规则，在登记的这次持锁内、写之前核对继承来的 endpoint。
        if (spec.provider_endpoints) {
            require_inherited_endpoints(settings_from_json(spec.settings_json), *spec.provider_endpoints);
        }
        impl_->insert_world(spec, home);
        return impl_->require(spec.name);
    } catch (...) {
        discard();
        throw;
    }
}

std::string RegistryDb::update_world_settings(
    const std::string& name, const std::string& overrides_json,
    const std::optional<ProviderEndpoints>& provider_endpoints) {
    const std::scoped_lock lock(impl_->mutex);
    const auto rec = impl_->require(name);
    const auto merged =
        merge_settings(settings_from_json(rec.settings_json),
                       overrides_json.empty() ? "{}" : overrides_json, rec.read_only);
    if (provider_endpoints) { require_inherited_endpoints(merged, *provider_endpoints); }
    const auto text = settings_to_json(merged);
    impl_->set_settings(name, text);
    return text;
}

std::string RegistryDb::reconfirm_world_endpoints(
    const std::string& name, const std::map<std::string, EndpointSettings>& current) {
    const std::scoped_lock lock(impl_->mutex);
    const auto rec = impl_->require(name);
    WorldSettings settings = settings_from_json(rec.settings_json);
    for (auto* role : {&settings.extraction, &settings.embedding, &settings.chat,
                       &settings.consolidation,}) {
        if (!role->has_value() || (*role)->provider_name.empty()) { continue; }
        const std::string provider_name = (*role)->provider_name;
        const auto found = current.find(provider_name);
        if (found == current.end()) {
            role->reset();                       // provider 已被删掉：解绑
            continue;
        }
        EndpointSettings fresh = found->second;
        fresh.provider_name = provider_name;     // 名字保持不变
        *role = std::move(fresh);
    }
    settings = merge_settings(settings, "{}", rec.read_only);   // 只读世界的后台恒关
    const auto text = settings_to_json(settings);
    impl_->set_settings(name, text);
    return text;
}

std::string RegistryDb::replace_world_sections(const std::string& name,
                                               const std::string& sections_json) {
    const std::scoped_lock lock(impl_->mutex);
    const auto rec = impl_->require(name);
    WorldSettings replaced = replace_sections(settings_from_json(rec.settings_json),
                                              sections_json.empty() ? "{}" : sections_json);
    // 只读世界的后台恒关，与 update_world_settings（merge_settings 的 read_only）同一条规则。
    replaced = merge_settings(replaced, "{}", rec.read_only);
    const auto text = settings_to_json(replaced);
    impl_->set_settings(name, text);
    return text;
}

void RegistryDb::remove(const std::string& name) {
    const std::scoped_lock lock(impl_->mutex);
    const WorldRecord rec = impl_->require(name);
    std::string dflt = "default";
    {
        pst::Stmt stmt(impl_->db(), "SELECT value FROM meta WHERE key = 'default_world'");
        if (stmt.step_row()) { dflt = text_col(stmt.raw(), 0); }
    }
    if (name == dflt) {
        throw WorldStoreError("world_protected", "the default world cannot be removed");
    }
    // 删除目标固定为 <data_dir>/worlds/<dir_id>。dir_id 来自库里，可能被手改，所以先验形状，
    // 再解析符号链接核对布局，最后由 remove_within 兜底；任何一步越界都抛 snapshot_failed，
    // 什么都不删，注册项保留。
    const std::string& dir_id = rec.dir_id;
    if (dir_id.empty() || dir_id == "." || dir_id == ".." ||
        dir_id.find('/') != std::string::npos || dir_id.find('\\') != std::string::npos) {
        throw WorldStoreError("snapshot_failed",
                              "invalid dir_id '" + dir_id + "' for world '" + name + "'");
    }
    const fs::path worlds_root = impl_->data_dir / "worlds";
    try {
        internal::require_worlds_root_inside(impl_->data_dir, worlds_root);
    } catch (const WorldStoreError& err) {
        throw WorldStoreError("snapshot_failed", err.what());
    }
    internal::require_resolved_home(worlds_root, dir_id, "snapshot_failed", name);
    if (!internal::remove_within(worlds_root, worlds_root / dir_id)) {
        throw WorldStoreError("snapshot_failed", "world directory of '" + name +
                                                     "' resolves outside " + worlds_root.string() +
                                                     "; refusing to delete");
    }
    // 先删目录再摘注册项：目录删到一半失败时注册项还在，可重试。
    pst::TransactionGuard guard(*impl_->conn);
    for (const char* sql : {"DELETE FROM world_labels WHERE name = ?",
                            "DELETE FROM snapshots WHERE world = ?",
                            "DELETE FROM worlds WHERE name = ?",}) {
        pst::Stmt stmt(impl_->db(), sql);
        stmt.bind(name);
        stmt.step_done();
    }
    guard.commit();
}

void RegistryDb::set_labels(const std::string& name,
                            const std::map<std::string, std::string>& labels) {
    const std::scoped_lock lock(impl_->mutex);
    (void)impl_->require(name);
    pst::TransactionGuard guard(*impl_->conn);
    if (!impl_->load_labels(name).empty()) {
        throw WorldStoreError("labels_immutable",
                              "world '" + name + "' already has labels; they cannot change");
    }
    impl_->insert_labels(name, labels);
    guard.commit();
}

void RegistryDb::update_settings(const std::string& name, const std::string& settings_json) {
    const std::scoped_lock lock(impl_->mutex);
    impl_->set_settings(name, settings_json);
}

void RegistryDb::update_stats(const std::string& name, std::int64_t holders,
                              std::int64_t statements, const std::string& last_write_at,
                              std::string_view now_iso) {
    const std::scoped_lock lock(impl_->mutex);
    (void)impl_->require(name);
    pst::TransactionGuard guard(*impl_->conn);
    pst::Stmt stmt(impl_->db(),
                   "UPDATE worlds SET holders = ?, statements = ?, last_write_at = ?, "
                   "stats_at = ? WHERE name = ?");
    stmt.bind(holders, statements, last_write_at, now_iso, name);
    stmt.step_done();
    guard.commit();
}

std::vector<SnapshotEntry> RegistryDb::snapshots(const std::string& world) const {
    const std::scoped_lock lock(impl_->mutex);
    (void)impl_->require(world);
    pst::Stmt stmt(impl_->db(),
                   "SELECT id, path, kind, reason, note, created_at, world_time, size_bytes, "
                   "sessions_complete, session_id, content_mark, digest FROM snapshots "
                   "WHERE world = ? ORDER BY seq DESC");
    stmt.bind(world);
    std::vector<SnapshotEntry> out;
    while (stmt.step_row()) {
        sqlite3_stmt* raw = stmt.raw();
        SnapshotEntry entry;
        entry.id = text_col(raw, 0);
        entry.path = text_col(raw, 1);
        entry.kind = text_col(raw, 2);
        entry.reason = text_col(raw, 3);
        entry.note = text_col(raw, 4);
        entry.created_at = text_col(raw, 5);
        entry.world_time = text_col(raw, 6);
        entry.size_bytes = sqlite3_column_int64(raw, 7);
        if (sqlite3_column_type(raw, 8) != SQLITE_NULL) {
            entry.sessions_complete = sqlite3_column_int64(raw, 8);
        }
        entry.session_id = text_col(raw, 9);
        entry.content_mark = text_col(raw, 10);
        entry.digest = text_col(raw, 11);
        out.push_back(std::move(entry));
    }
    return out;
}

void RegistryDb::put_snapshot(const std::string& world, const SnapshotEntry& entry) {
    const std::scoped_lock lock(impl_->mutex);
    (void)impl_->require(world);
    try {
        pst::TransactionGuard guard(*impl_->conn);
        insert_snapshot(impl_->db(), world, entry);
        guard.commit();
    } catch (const pst::SqliteError& err) {
        throw WorldStoreError("snapshot_failed", "cannot record snapshot '" + entry.id +
                                                     "' of '" + world + "': " + err.what());
    }
}

void RegistryDb::drop_snapshot(const std::string& world, const std::string& snapshot_id) {
    const std::scoped_lock lock(impl_->mutex);
    (void)impl_->require(world);
    pst::TransactionGuard guard(*impl_->conn);
    pst::Stmt stmt(impl_->db(), "DELETE FROM snapshots WHERE world = ? AND id = ?");
    stmt.bind(world, snapshot_id);
    stmt.step_done();
    if (sqlite3_changes(impl_->db()) == 0) {
        throw WorldStoreError("snapshot_not_found",
                              "no snapshot '" + snapshot_id + "' in world '" + world + "'");
    }
    guard.commit();
}

int RegistryDb::snapshot_retention() const {
    const std::scoped_lock lock(impl_->mutex);
    pst::Stmt stmt(impl_->db(), "SELECT value FROM meta WHERE key = 'snapshot_retention'");
    if (!stmt.step_row()) { return kDefaultRetention; }
    try {
        return std::stoi(text_col(stmt.raw(), 0));
    } catch (const std::exception&) {
        registry_corrupt("snapshot_retention is not an integer");
    }
}

const fs::path& RegistryDb::data_dir() const { return impl_->data_dir; }

namespace internal {

void write_fresh_registry(const fs::path& file, const std::string& default_world,
                          int snapshot_retention, const std::vector<ImportedWorld>& worlds) {
    try {
        auto conn = pst::Connection::open(file);
        conn.exec(kSchemaSql);
        {
            pst::TransactionGuard guard(conn);
            sqlite3* dbh = conn.raw();
            insert_meta(dbh, "schema_version", std::to_string(kSchemaVersion));
            insert_meta(dbh, "default_world", default_world);
            insert_meta(dbh, "snapshot_retention", std::to_string(snapshot_retention));
            for (const auto& world : worlds) {
                const WorldRecord& rec = world.record;
                pst::Stmt stmt(dbh,
                               "INSERT INTO worlds (name, dir_id, tenant, db_path, snapshot_dir, "
                               "label, created_at, settings_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)");
                stmt.bind(rec.name, rec.dir_id, rec.tenant, rec.db_path, rec.snapshot_dir,
                          rec.label, rec.created_at, rec.settings_json);
                stmt.step_done();
                // 最旧的先登记：seq 递增，snapshots() 按 seq 降序读回来就是原来的顺序。
                for (const auto& snap : std::ranges::reverse_view(world.snapshots)) {
                    insert_snapshot(dbh, rec.name, snap);
                }
            }
            guard.commit();
        }
        // 切回 DELETE 日志模式：WAL 合并进主文件、-wal / -shm 删掉，改名只需挪一个文件。
        conn.exec("PRAGMA journal_mode = DELETE");
        conn.close();
    } catch (const pst::SqliteError& err) {
        registry_corrupt("cannot write " + file.string() + ": " + err.what());
    }
}

}  // namespace internal

}  // namespace starling::governance
