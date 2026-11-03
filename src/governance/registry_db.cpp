// registry_db.cpp — 见 include/starling/governance/registry_db.hpp。

#include "starling/governance/registry_db.hpp"

#include "world_store_internal.hpp"

#include "starling/persistence/connection.hpp"
#include "starling/persistence/sqlite_helpers.hpp"
#include "starling/persistence/stmt.hpp"

#include <sqlite3.h>

#include <algorithm>
#include <mutex>
#include <optional>
#include <string>
#include <system_error>
#include <utility>

namespace starling::governance {

namespace fs = std::filesystem;
namespace pst = starling::persistence;

namespace {

constexpr int kSchemaVersion = 1;
constexpr int kDefaultRetention = 5;
constexpr std::int64_t kMaxPageLimit = 500;

// 建表 SQL。世界目录用 dir_id 命名，world_labels / snapshots 按世界名关联。
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
    PRIMARY KEY (world, id)
);
)sql";

constexpr const char* kWorldColumns =
    "name, dir_id, tenant, db_path, snapshot_dir, label, created_at, seed, read_only, "
    "reclaimable, retention_group, fork_of_world, fork_of_snapshot, settings_json, holders, "
    "statements, last_write_at, stats_at";

std::string text_col(sqlite3_stmt* stmt, int col) {
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

}  // namespace

struct RegistryDb::Impl {
    fs::path data_dir;
    mutable std::mutex mutex;
    mutable std::optional<pst::Connection> conn;

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
        impl->conn.emplace(pst::Connection::open(file));
        impl->conn->exec(kSchemaSql);
        pst::TransactionGuard guard(*impl->conn);
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
    const std::string sql = std::string("SELECT ") + kWorldColumns + " FROM worlds w" + where +
                            " ORDER BY created_at DESC, name ASC LIMIT ? OFFSET ?";
    pst::Stmt stmt(impl_->db(), sql.c_str());
    const int used = bind_labels(stmt);
    sqlite3_bind_int64(stmt.raw(), used + 1, query.limit);
    sqlite3_bind_int64(stmt.raw(), used + 2, query.offset);
    while (stmt.step_row()) { page.worlds.push_back(read_world(stmt.raw())); }
    for (auto& rec : page.worlds) { rec.labels = impl_->load_labels(rec.name); }
    return page;
}

WorldRecord RegistryDb::create(const std::string& name, const std::string& label,
                               const std::map<std::string, std::string>& labels,
                               const std::string& settings_json, std::string_view now_iso) {
    if (!is_valid_world_name(name)) {
        throw WorldStoreError("invalid_world_name",
                              "'" + name + "' does not match [A-Za-z0-9][A-Za-z0-9._-]{0,127}");
    }
    const std::scoped_lock lock(impl_->mutex);
    if (impl_->find(name)) { throw WorldStoreError("world_exists", "world '" + name + "' exists"); }
    const fs::path worlds_root = impl_->data_dir / "worlds";
    internal::require_worlds_root_inside(impl_->data_dir, worlds_root);
    std::string dir_id = internal::random_hex();
    while (impl_->dir_id_taken(dir_id) || fs::exists(worlds_root / dir_id)) {
        dir_id = internal::random_hex();
    }
    const fs::path home = worlds_root / dir_id;
    std::error_code mkdir_err;
    fs::create_directories(home / "snapshots", mkdir_err);
    if (mkdir_err) {
        throw WorldStoreError("snapshot_failed",
                              "cannot create " + home.string() + ": " + mkdir_err.message());
    }
    try {
        pst::TransactionGuard guard(*impl_->conn);
        pst::Stmt stmt(impl_->db(),
                       "INSERT INTO worlds (name, dir_id, tenant, db_path, snapshot_dir, label, "
                       "created_at, settings_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)");
        stmt.bind(name, dir_id, name, (home / "world.db").string(),
                  (home / "snapshots").string(), label, now_iso, settings_json);
        stmt.step_done();
        impl_->insert_labels(name, labels);
        guard.commit();
    } catch (...) {
        std::error_code rm_err;
        fs::remove_all(home, rm_err);
        throw;
    }
    return impl_->require(name);
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
    internal::require_resolved_home(worlds_root, dir_id, "snapshot_failed");
    if (!internal::remove_within(worlds_root, worlds_root / dir_id)) {
        throw WorldStoreError("snapshot_failed", "world directory of '" + name +
                                                     "' resolves outside " + worlds_root.string() +
                                                     "; refusing to delete");
    }
    // 先删目录再摘注册项：目录删到一半失败时注册项还在，可重试。
    pst::TransactionGuard guard(*impl_->conn);
    for (const char* sql : {"DELETE FROM world_labels WHERE name = ?",
                            "DELETE FROM snapshots WHERE world = ?",
                            "DELETE FROM worlds WHERE name = ?"}) {
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
    (void)impl_->require(name);
    pst::TransactionGuard guard(*impl_->conn);
    pst::Stmt stmt(impl_->db(), "UPDATE worlds SET settings_json = ? WHERE name = ?");
    stmt.bind(settings_json, name);
    stmt.step_done();
    guard.commit();
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
                   "sessions_complete, session_id, content_mark FROM snapshots WHERE world = ? "
                   "ORDER BY created_at DESC, id DESC");
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
        out.push_back(std::move(entry));
    }
    return out;
}

void RegistryDb::put_snapshot(const std::string& world, const SnapshotEntry& entry) {
    const std::scoped_lock lock(impl_->mutex);
    (void)impl_->require(world);
    pst::TransactionGuard guard(*impl_->conn);
    pst::Stmt stmt(impl_->db(),
                   "INSERT INTO snapshots (world, id, path, kind, reason, note, created_at, "
                   "world_time, size_bytes, sessions_complete, session_id, content_mark) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)");
    stmt.bind(world, entry.id, entry.path, entry.kind, entry.reason, entry.note, entry.created_at,
              entry.world_time, entry.size_bytes);
    if (entry.sessions_complete) {
        sqlite3_bind_int64(stmt.raw(), 10, *entry.sessions_complete);
    } else {
        sqlite3_bind_null(stmt.raw(), 10);
    }
    pst::detail::bind_sv(stmt.raw(), 11, entry.session_id);
    pst::detail::bind_sv(stmt.raw(), 12, entry.content_mark);
    try {
        stmt.step_done();
    } catch (const pst::SqliteError& err) {
        throw WorldStoreError("snapshot_failed", "cannot record snapshot '" + entry.id +
                                                     "' of '" + world + "': " + err.what());
    }
    guard.commit();
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

}  // namespace starling::governance
