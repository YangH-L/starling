// test_world_fork.cpp — 分叉、种子与升级种子（spec §6.1、§6.2）。
// 库都经正常写路径（SqliteAdapter::open，真实迁移）建出，语句用裸 INSERT 写入，零 LLM、零网络。
// 摘要用 memoryops::digest 在只读连接上算，与 fork_world 返回的值对照。

#include "starling/governance/world_store.hpp"

#include "starling/crypto/sha256.hpp"
#include "starling/governance/registry_db.hpp"
#include "starling/governance/world_settings.hpp"
#include "starling/memory/memory_digest.hpp"
#include "starling/persistence/connection.hpp"
#include "starling/persistence/migration_runner.hpp"
#include "starling/persistence/sqlite_adapter.hpp"
#include "starling/persistence/stmt.hpp"

#include <gtest/gtest.h>
#include <sqlite3.h>
#include <unistd.h>

#include <array>
#include <atomic>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iterator>
#include <map>
#include <regex>
#include <set>
#include <string>
#include <system_error>

namespace starling::governance {
namespace {

namespace fs = std::filesystem;

constexpr const char* kNow = "2026-11-06T00:00:00Z";

class TempDir {
public:
    TempDir() {
        static std::atomic<int> seq{0};
        path_ = fs::temp_directory_path() /
                ("starling-fork-" + std::to_string(::getpid()) + "-" + std::to_string(++seq));
        fs::remove_all(path_);
        fs::create_directories(path_);
    }
    ~TempDir() { std::error_code err; fs::remove_all(path_, err); }
    TempDir(const TempDir&) = delete;
    TempDir& operator=(const TempDir&) = delete;
    TempDir(TempDir&&) = delete;
    TempDir& operator=(TempDir&&) = delete;
    [[nodiscard]] const fs::path& path() const { return path_; }
private:
    fs::path path_;
};

std::string code_of(const std::function<void()>& action) {
    try { action(); } catch (const WorldStoreError& err) { return err.code(); }
    return "<no throw>";
}

std::string file_sha256(const fs::path& file) {
    std::ifstream input(file, std::ios::binary);
    const std::string bytes{std::istreambuf_iterator<char>(input),
                            std::istreambuf_iterator<char>()};
    return crypto::sha256_hex(bytes);
}

RegistryDb open_registry(const TempDir& tmp) {
    return RegistryDb::open(tmp.path(), tmp.path() / "dashboard.db", "default");
}

// worlds/ 下现有的目录名：断言「失败的分叉不留目录」。
std::set<std::string> world_dirs(const RegistryDb& reg) {
    std::set<std::string> out;
    const fs::path root = reg.data_dir() / "worlds";
    if (!fs::exists(root)) { return out; }
    for (const auto& item : fs::directory_iterator(root)) {
        out.insert(item.path().filename().string());
    }
    return out;
}

void run_sql(const fs::path& db_file, const std::string& sql) {
    sqlite3* raw = nullptr;
    ASSERT_EQ(sqlite3_open(db_file.string().c_str(), &raw), SQLITE_OK);
    const persistence::SqliteHandle handle(raw);
    char* err = nullptr;
    const int code = sqlite3_exec(raw, sql.c_str(), nullptr, nullptr, &err);
    const std::string why = err != nullptr ? err : "";
    sqlite3_free(err);
    ASSERT_EQ(code, SQLITE_OK) << why;
}

// 往已迁移的库写 count 条语句 st-<first> ..（经写路径打开，关闭时合并 WAL）。
void insert_statements(const fs::path& db_file, const std::string& tenant, int first, int count) {
    auto adapter = persistence::SqliteAdapter::open(db_file);
    for (int idx = first; idx < first + count; ++idx) {
        persistence::Stmt ins(adapter->connection().raw(),
            "INSERT INTO statements(id,tenant_id,holder_id,holder_perspective,subject_kind,"
            "subject_id,predicate,object_kind,object_value,canonical_object_hash,"
            "canonical_object_hash_version,modality,polarity,confidence,observed_at,salience,"
            "affect_json,activation,last_accessed,provenance,created_at,updated_at) "
            "VALUES(?1,?2,'self','first_person','entity','thing','likes','str',?3,?4,'v1',"
            "'believes','pos',0.5,'2026-10-28T00:00:00Z',0.5,'{}',0.0,'2026-10-28T00:00:00Z',"
            "'user_input','2026-10-28T00:00:00Z','2026-10-28T00:00:00Z')");
        const std::string value = "object-" + std::to_string(idx);
        ins.bind("st-" + std::to_string(idx), tenant, value, crypto::sha256_hex(value));
        ins.step_done();
    }
    adapter->close();
}

std::string digest_of(const fs::path& db_file, const std::string& tenant) {
    auto conn = persistence::Connection::open_read_only(db_file);
    return memoryops::digest(conn, tenant);
}

int schema_of(const fs::path& db_file) {
    auto adapter = persistence::SqliteAdapter::open_read_only(db_file);
    return adapter->schema_version();
}

// 建一个已初始化（迁移到最新）的世界，写 statements 条语句。
WorldRecord world_with_statements(RegistryDb& reg, const std::string& name, int statements) {
    const WorldRecord rec = reg.create_world(name, "", {{"team", "src"}}, "{}", kNow);
    insert_statements(rec.db_path, rec.tenant, 0, statements);
    return rec;
}

ForkOptions fork_opts() {
    ForkOptions opts;
    opts.now_iso = kNow;
    return opts;
}

}  // namespace

TEST(WorldFork, ForkCopiesContentAndDigestMatches) {
    const TempDir tmp;
    auto reg = open_registry(tmp);
    (void)reg.update_world_settings("default", R"({"source_lane":{"k_source":7}})");
    const WorldRecord src = world_with_statements(reg, "deploy-1", 3);
    const std::string src_sha = file_sha256(src.db_path);
    const std::string src_digest = digest_of(src.db_path, src.tenant);

    ForkOptions opts = fork_opts();
    opts.labels = {{"run", "r1"}};
    opts.retention_group = "grp-a";
    opts.reclaimable = true;
    opts.settings_overrides_json = R"({"source_lane":{"dim":512}})";
    const ForkResult forked = fork_world(reg, "deploy-1", "rollout-1", opts);

    EXPECT_EQ(forked.digest, src_digest);
    EXPECT_EQ(digest_of(forked.world.db_path, forked.world.tenant), src_digest);
    EXPECT_EQ(file_sha256(src.db_path), src_sha);   // 来源字节不变
    EXPECT_EQ(forked.world.fork_of_world, "deploy-1");
    EXPECT_EQ(forked.world.fork_of_snapshot, "");
    EXPECT_EQ(forked.world.tenant, src.tenant);      // 租户取来源（statements.tenant_id 不可变）
    EXPECT_NE(forked.world.dir_id, src.dir_id);
    EXPECT_EQ(fs::path(forked.world.db_path),
              reg.data_dir() / "worlds" / forked.world.dir_id / "world.db");
    EXPECT_TRUE(fs::is_directory(forked.world.snapshot_dir));
    EXPECT_FALSE(fs::exists(forked.world.db_path + ".partial"));
    EXPECT_EQ(forked.world.labels, (std::map<std::string, std::string>{{"run", "r1"}}));
    EXPECT_EQ(forked.world.retention_group, "grp-a");
    EXPECT_TRUE(forked.world.reclaimable);
    EXPECT_FALSE(forked.world.seed);
    EXPECT_FALSE(forked.world.read_only);
    // 设置 = 来源的设置（它继承了默认世界的 k_source=7）+ 覆盖（dim=512）。
    const auto settings = settings_from_json(forked.world.settings_json);
    EXPECT_EQ(settings.source_lane.k_source, 7);
    EXPECT_EQ(settings.source_lane.dim, 512);
    // 注册表里的记录与返回值一致。
    EXPECT_EQ(reg.get("rollout-1").fork_of_world, "deploy-1");
    validate_world(reg, "rollout-1");
}

TEST(WorldFork, ForkFromSnapshot) {
    const TempDir tmp;
    auto reg = open_registry(tmp);
    const WorldRecord src = world_with_statements(reg, "deploy-1", 2);
    const SnapshotEntry snap = take_snapshot(reg, "deploy-1", SnapshotOptions{});
    ASSERT_FALSE(snap.digest.empty());
    insert_statements(src.db_path, src.tenant, 10, 2);   // 快照之后的写入不进分叉
    ASSERT_NE(digest_of(src.db_path, src.tenant), snap.digest);
    const std::string snap_sha = file_sha256(snap.path);

    ForkOptions opts = fork_opts();
    opts.snapshot_id = snap.id;
    const ForkResult forked = fork_world(reg, "deploy-1", "replay-1", opts);
    EXPECT_EQ(forked.digest, snap.digest);
    EXPECT_EQ(digest_of(forked.world.db_path, forked.world.tenant), snap.digest);
    EXPECT_EQ(forked.world.fork_of_world, "deploy-1");
    EXPECT_EQ(forked.world.fork_of_snapshot, snap.id);
    EXPECT_EQ(reg.get("replay-1").fork_of_snapshot, snap.id);
    EXPECT_EQ(file_sha256(snap.path), snap_sha);

    opts.snapshot_id = "20261106T000000Z-0000";
    EXPECT_EQ(code_of([&] { (void)fork_world(reg, "deploy-1", "replay-2", opts); }),
              "snapshot_not_found");
}

TEST(WorldFork, ForkRefusesSchemaBehindSource) {
    const TempDir tmp;
    auto reg = open_registry(tmp);
    const WorldRecord src = world_with_statements(reg, "deploy-1", 1);
    run_sql(src.db_path, "DELETE FROM schema_migrations "
                         "WHERE version = (SELECT MAX(version) FROM schema_migrations)");
    ASSERT_LT(schema_of(src.db_path), persistence::MigrationRunner::latest_embedded_version());
    const auto dirs = world_dirs(reg);
    const std::string src_sha = file_sha256(src.db_path);

    EXPECT_EQ(code_of([&] { (void)fork_world(reg, "deploy-1", "rollout-1", fork_opts()); }),
              "world_schema_behind");
    EXPECT_EQ(world_dirs(reg), dirs);
    EXPECT_EQ(code_of([&] { (void)reg.get("rollout-1"); }), "world_not_found");
    EXPECT_EQ(file_sha256(src.db_path), src_sha);
}

TEST(WorldFork, ForkDigestMismatchLeavesNothing) {
    const TempDir tmp;
    auto reg = open_registry(tmp);
    (void)world_with_statements(reg, "deploy-1", 3);
    const auto dirs = world_dirs(reg);

    ForkOptions opts = fork_opts();
    opts.after_copy_for_test = [](const fs::path& partial) {
        run_sql(partial, "UPDATE statements SET object_value='tampered' WHERE id='st-1'");
    };
    EXPECT_EQ(code_of([&] { (void)fork_world(reg, "deploy-1", "rollout-1", opts); }),
              "fork_digest_mismatch");
    EXPECT_EQ(world_dirs(reg), dirs);
    EXPECT_EQ(code_of([&] { (void)reg.get("rollout-1"); }), "world_not_found");
}

TEST(WorldFork, ForkNameTakenIsWorldExists) {
    const TempDir tmp;
    auto reg = open_registry(tmp);
    (void)world_with_statements(reg, "deploy-1", 1);
    (void)reg.create_world("taken", "", {}, "{}", kNow);
    const auto dirs = world_dirs(reg);
    EXPECT_EQ(code_of([&] { (void)fork_world(reg, "deploy-1", "taken", fork_opts()); }),
              "world_exists");
    EXPECT_EQ(code_of([&] { (void)fork_world(reg, "deploy-1", "default", fork_opts()); }),
              "world_exists");
    EXPECT_EQ(code_of([&] { (void)fork_world(reg, "deploy-1", "../x", fork_opts()); }),
              "invalid_world_name");
    EXPECT_EQ(code_of([&] { (void)fork_world(reg, "nope", "fresh", fork_opts()); }),
              "world_not_found");
    EXPECT_EQ(world_dirs(reg), dirs);
}

TEST(WorldFork, SeedImpliesReadOnly) {
    const TempDir tmp;
    auto reg = open_registry(tmp);
    (void)world_with_statements(reg, "deploy-1", 2);
    ForkOptions opts = fork_opts();
    opts.seed = true;
    const ForkResult seeded = fork_world(reg, "deploy-1", "seed-1", opts);
    EXPECT_TRUE(seeded.world.seed);
    EXPECT_TRUE(seeded.world.read_only);
    // 只读世界的后台恒关。
    const auto background = settings_from_json(seeded.world.settings_json).background;
    EXPECT_EQ(background.tick_interval_s, 0.0);
    EXPECT_FALSE(background.ingest);
    EXPECT_EQ(background.auto_snapshot, (AutoSnapshotConfig{.on_session = false, .interval_hours = 0}));
    const WorldRecord stored = reg.get("seed-1");
    EXPECT_TRUE(stored.seed);
    EXPECT_TRUE(stored.read_only);
    // 建空世界时指定 seed 同样蕴含只读。
    const WorldRecord empty_seed =
        reg.create_world("seed-2", "", {}, "{}", kNow, std::nullopt, WorldFlags{.seed = true});
    EXPECT_TRUE(empty_seed.seed);
    EXPECT_TRUE(empty_seed.read_only);
}

TEST(WorldFork, UpgradeSeedMigratesCopyAndKeepsOld) {
    const TempDir tmp;
    auto reg = open_registry(tmp);
    const WorldRecord old_seed =
        reg.create("seed-v1", "情境一", {{"scenario", "s1"}}, "{}", kNow, WorldFlags{.seed = true});
    // 旧版 Starling 写出的种子：有内容（3 条语句），schema_migrations 少了最后一条（落后于当前代码）。
    insert_statements(old_seed.db_path, old_seed.tenant, 0, 3);
    run_sql(old_seed.db_path, "DELETE FROM schema_migrations "
                              "WHERE version = (SELECT MAX(version) FROM schema_migrations)");
    ASSERT_LT(schema_of(old_seed.db_path), persistence::MigrationRunner::latest_embedded_version());
    const std::string old_sha = file_sha256(old_seed.db_path);

    const ForkResult upgraded = upgrade_seed(reg, "seed-v1", "seed-v2", kNow);
    EXPECT_EQ(schema_of(upgraded.world.db_path),
              persistence::MigrationRunner::latest_embedded_version());
    EXPECT_EQ(upgraded.digest, digest_of(upgraded.world.db_path, upgraded.world.tenant));
    EXPECT_TRUE(upgraded.world.seed);
    EXPECT_TRUE(upgraded.world.read_only);
    EXPECT_EQ(upgraded.world.fork_of_world, "seed-v1");
    EXPECT_EQ(upgraded.world.tenant, old_seed.tenant);
    EXPECT_EQ(upgraded.world.labels, old_seed.labels);
    // 内容随复制过来：旧种子的 3 条语句都在新种子里，值不变。
    {
        auto conn = persistence::Connection::open_read_only(upgraded.world.db_path);
        persistence::Stmt rows(conn.raw(),
                               "SELECT id, object_value FROM statements WHERE tenant_id = ?1 ORDER BY id");
        rows.bind(old_seed.tenant);
        std::map<std::string, std::string> found;
        while (rows.step_row()) {
            // NOLINTNEXTLINE(cppcoreguidelines-pro-type-reinterpret-cast)
            found.emplace(reinterpret_cast<const char*>(sqlite3_column_text(rows.raw(), 0)),
                          // NOLINTNEXTLINE(cppcoreguidelines-pro-type-reinterpret-cast)
                          reinterpret_cast<const char*>(sqlite3_column_text(rows.raw(), 1)));
        }
        EXPECT_EQ(found, (std::map<std::string, std::string>{
                             {"st-0", "object-0"}, {"st-1", "object-1"}, {"st-2", "object-2"}}));
    }
    // 迁移后的副本是一份文件：没有挂着的 -wal / -shm，也没有 partial。
    EXPECT_FALSE(fs::exists(upgraded.world.db_path + "-wal"));
    EXPECT_FALSE(fs::exists(upgraded.world.db_path + "-shm"));
    EXPECT_FALSE(fs::exists(upgraded.world.db_path + ".partial"));
    // 旧种子原样保留。
    EXPECT_EQ(file_sha256(old_seed.db_path), old_sha);
    EXPECT_TRUE(reg.get("seed-v1").seed);
}

TEST(WorldFork, UpgradeSeedRequiresASeed) {
    const TempDir tmp;
    auto reg = open_registry(tmp);
    (void)world_with_statements(reg, "deploy-1", 1);
    const auto dirs = world_dirs(reg);
    EXPECT_EQ(code_of([&] { (void)upgrade_seed(reg, "deploy-1", "seed-v2", kNow); }),
              "world_not_seed");   // 裁定 R31 M2
    EXPECT_EQ(world_dirs(reg), dirs);
    EXPECT_EQ(code_of([&] { (void)reg.get("seed-v2"); }), "world_not_found");
}

// 来源开着（引擎的写连接，WAL 里有还没合并的提交）：从这条连接复制，读到它最近一次提交。
TEST(WorldFork, ForkFromConnectionSeesTheOpenWorldsCommits) {
    const TempDir tmp;
    auto reg = open_registry(tmp);
    const WorldRecord src = world_with_statements(reg, "deploy-1", 2);
    auto writer = persistence::SqliteAdapter::open(src.db_path);
    {
        persistence::Stmt ins(writer->connection().raw(),
            "UPDATE statements SET object_value='changed-while-open' WHERE id='st-0'");
        ins.step_done();
    }
    const std::string live = memoryops::digest(writer->connection(), src.tenant);
    const ForkResult forked =
        fork_world_from_connection(reg, writer->connection(), "deploy-1", "rollout-1", fork_opts());
    EXPECT_EQ(forked.digest, live);
    EXPECT_EQ(digest_of(forked.world.db_path, forked.world.tenant), live);
    writer->close();

    ForkOptions from_snapshot = fork_opts();
    from_snapshot.snapshot_id = "20261106T000000Z-0000";
    auto again = persistence::SqliteAdapter::open(src.db_path);
    EXPECT_THROW((void)fork_world_from_connection(reg, again->connection(), "deploy-1",
                                                  "rollout-2", from_snapshot),
                 std::invalid_argument);
}

// 裁定 R11：分叉途中来源被别的连接写了（摘要与复制之间），不能报成 fork_digest_mismatch：
// 来源的 data_version 变了 → world_busy（瞬态，调用方重试），不留目录、不留注册项。
TEST(WorldFork, ConcurrentWriteDuringForkIsWorldBusy) {
    const TempDir tmp;
    auto reg = open_registry(tmp);
    const WorldRecord src = world_with_statements(reg, "deploy-1", 2);
    auto writer = persistence::SqliteAdapter::open(src.db_path);
    const auto dirs = world_dirs(reg);

    ForkOptions opts = fork_opts();
    opts.after_copy_for_test = [&src](const fs::path&) {
        insert_statements(src.db_path, src.tenant, 50, 1);   // 第二条连接写来源
    };
    EXPECT_EQ(code_of([&] {
                  (void)fork_world_from_connection(reg, writer->connection(), "deploy-1",
                                                   "rollout-1", opts);
              }),
              "world_busy");
    EXPECT_EQ(world_dirs(reg), dirs);
    EXPECT_EQ(code_of([&] { (void)reg.get("rollout-1"); }), "world_not_found");

    // 同一条连接自己写（引擎的另一个线程共用这条连接）同样看得出来。
    opts.after_copy_for_test = [&writer](const fs::path&) {
        persistence::Stmt upd(writer->connection().raw(),
                              "UPDATE statements SET object_value='own-write' WHERE id='st-0'");
        upd.step_done();
    };
    EXPECT_EQ(code_of([&] {
                  (void)fork_world_from_connection(reg, writer->connection(), "deploy-1",
                                                   "rollout-1", opts);
              }),
              "world_busy");
    EXPECT_EQ(world_dirs(reg), dirs);
    writer->close();
}

// 共用的引擎连接上开着事务（或有没走完的语句）时不能 VACUUM：报 world_busy（瞬态，调用方重试），
// 不报 snapshot_failed；不留目录、不留注册项。
TEST(WorldFork, ForkFromConnectionInAnOpenTransactionIsWorldBusy) {
    const TempDir tmp;
    auto reg = open_registry(tmp);
    const WorldRecord src = world_with_statements(reg, "deploy-1", 2);
    auto writer = persistence::SqliteAdapter::open(src.db_path);
    const auto dirs = world_dirs(reg);

    writer->connection().exec("BEGIN");
    EXPECT_EQ(code_of([&] {
                  (void)fork_world_from_connection(reg, writer->connection(), "deploy-1",
                                                   "rollout-1", fork_opts());
              }),
              "world_busy");
    writer->connection().exec("ROLLBACK");
    EXPECT_EQ(world_dirs(reg), dirs);
    EXPECT_EQ(code_of([&] { (void)reg.get("rollout-1"); }), "world_not_found");

    // 自动提交模式下、还有一条读到一半的语句：VACUUM 报 "SQL statements in progress"。
    {
        persistence::Stmt reading(writer->connection().raw(), "SELECT id FROM statements");
        ASSERT_TRUE(reading.step_row());
        EXPECT_EQ(code_of([&] {
                      (void)fork_world_from_connection(reg, writer->connection(), "deploy-1",
                                                       "rollout-1", fork_opts());
                  }),
                  "world_busy");
    }
    EXPECT_EQ(world_dirs(reg), dirs);
    EXPECT_EQ(code_of([&] { (void)reg.get("rollout-1"); }), "world_not_found");

    // 语句走完之后照常分叉。
    const ForkResult forked =
        fork_world_from_connection(reg, writer->connection(), "deploy-1", "rollout-1", fork_opts());
    EXPECT_EQ(forked.world.fork_of_world, "deploy-1");
    writer->close();
}


namespace {

// 世界目录这一层的条目（不进 snapshots/ 子目录）：断言「只读打开没有在库旁边留下文件」。
std::set<std::string> home_entries(const fs::path& db_file) {
    std::set<std::string> out;
    for (const auto& item : fs::directory_iterator(db_file.parent_path())) {
        out.insert(item.path().filename().string());
    }
    return out;
}

// 库头第 19 字节（读版本）：2 = WAL 模式。
int read_version_byte(const fs::path& db_file) {
    std::ifstream input(db_file, std::ios::binary);
    std::array<char, 20> header{};
    input.read(header.data(), static_cast<std::streamsize>(header.size()));
    return static_cast<int>(header.back());
}

}  // namespace

// 裁定 R28（审查 I1）：create_world 建的库是 WAL 头，写连接正常关闭后旁边没有 -wal / -shm。world_store 里
// 每条只读打开都走 Connection::open_read_only 的同一条规则（这种库用 immutable=1），不在库旁边新建文件。
TEST(WorldReadOnlyOpen, WalHeaderLibraryWithoutSidecarsGrowsNoFiles) {
    const TempDir tmp;
    auto reg = open_registry(tmp);
    const WorldRecord src = world_with_statements(reg, "deploy-1", 2);
    ASSERT_EQ(read_version_byte(src.db_path), 2);
    ASSERT_FALSE(fs::exists(src.db_path + "-wal"));
    ASSERT_FALSE(fs::exists(src.db_path + "-shm"));
    const auto before = home_entries(src.db_path);
    const std::string sha = file_sha256(src.db_path);

    EXPECT_EQ(world_stats(src.db_path, src.tenant).statements, 2);
    EXPECT_EQ(home_entries(src.db_path), before) << "world_stats";
    verify_world_tenant(src.db_path, src.tenant);
    EXPECT_EQ(home_entries(src.db_path), before) << "verify_world_tenant";
    (void)snapshot_world_time(src.db_path, src.tenant);
    (void)list_sessions(reg, "deploy-1", 10);
    EXPECT_EQ(home_entries(src.db_path), before) << "snapshot_world_time / list_sessions";
    (void)take_snapshot(reg, "deploy-1", SnapshotOptions{});
    EXPECT_EQ(home_entries(src.db_path), before) << "take_snapshot";
    (void)fork_world(reg, "deploy-1", "rollout-1", fork_opts());
    EXPECT_EQ(home_entries(src.db_path), before) << "fork_world";
    EXPECT_EQ(file_sha256(src.db_path), sha);
}

// 裁定 R29（审查 I2）：分叉用 VACUUM INTO 复制整个文件，摘要只比来源租户。来源库里有别的租户的行时，
// 新世界每次打开都会 world_tenant_mismatch，所以分叉在 partial 上核对租户（与回滚第 6–7 步同一个函数），
// 不过就抛 world_tenant_mismatch：不改名、不登记、不留目录。三条复制路径都覆盖。
TEST(WorldFork, ForeignTenantRowInSourceRefusesTheFork) {
    const TempDir tmp;
    auto reg = open_registry(tmp);
    const WorldRecord src = world_with_statements(reg, "deploy-1", 2);
    insert_statements(src.db_path, "someone-else", 100, 1);
    const auto dirs = world_dirs(reg);
    const std::string src_sha = file_sha256(src.db_path);

    EXPECT_EQ(code_of([&] { (void)fork_world(reg, "deploy-1", "rollout-1", fork_opts()); }),
              "world_tenant_mismatch");
    EXPECT_EQ(world_dirs(reg), dirs);
    EXPECT_EQ(code_of([&] { (void)reg.get("rollout-1"); }), "world_not_found");

    {
        auto writer = persistence::SqliteAdapter::open(src.db_path);
        EXPECT_EQ(code_of([&] {
                      (void)fork_world_from_connection(reg, writer->connection(), "deploy-1",
                                                       "rollout-2", fork_opts());
                  }),
                  "world_tenant_mismatch");
        writer->close();
    }
    EXPECT_EQ(world_dirs(reg), dirs);
    EXPECT_EQ(code_of([&] { (void)reg.get("rollout-2"); }), "world_not_found");

    const WorldRecord old_seed = reg.create("seed-v1", "", {}, "{}", kNow, WorldFlags{.seed = true});
    insert_statements(old_seed.db_path, old_seed.tenant, 0, 1);
    insert_statements(old_seed.db_path, "someone-else", 100, 1);
    const auto with_seed = world_dirs(reg);
    EXPECT_EQ(code_of([&] { (void)upgrade_seed(reg, "seed-v1", "seed-v2", kNow); }),
              "world_tenant_mismatch");
    EXPECT_EQ(world_dirs(reg), with_seed);
    EXPECT_EQ(code_of([&] { (void)reg.get("seed-v2"); }), "world_not_found");
}

// 裁定 R31 M1：只读世界（含种子）不能恢复。预检与回滚都在任何文件 I/O 之前抛 world_read_only，库字节不变、
// 不建归档。
TEST(WorldRollback, ReadOnlyWorldIsRefusedBeforeAnyIo) {
    const TempDir tmp;
    auto reg = open_registry(tmp);
    (void)world_with_statements(reg, "deploy-1", 2);
    ForkOptions opts = fork_opts();
    opts.seed = true;
    const ForkResult seeded = fork_world(reg, "deploy-1", "seed-1", opts);
    const SnapshotEntry snap = take_snapshot(reg, "seed-1", SnapshotOptions{});
    const std::string sha = file_sha256(seeded.world.db_path);
    const auto entries = home_entries(seeded.world.db_path);

    EXPECT_EQ(code_of([&] { check_rollback(reg, "seed-1", snap.id, RollbackOptions{}); }),
              "world_read_only");
    EXPECT_EQ(code_of([&] { (void)rollback_world(reg, "seed-1", snap.id, RollbackOptions{}); }),
              "world_read_only");
    // 快照 id 不存在时同样先报只读（只读这一条与输入无关，重试也过不了）。
    EXPECT_EQ(code_of([&] {
                  (void)rollback_world(reg, "seed-1", "20261106T000000Z-0000", RollbackOptions{});
              }),
              "world_read_only");
    EXPECT_EQ(file_sha256(seeded.world.db_path), sha);
    EXPECT_EQ(home_entries(seeded.world.db_path), entries);   // 没有 archive/、没有 partial
}

// 裁定 R31 M3：分叉带设置覆盖时，与 create_world 一样在登记的同一次持锁内用 provider_endpoints 核对继承来的
// endpoint（R40、R51）。地址变了 → endpoint_changed，不留文件、不登记。不给表（nullopt）时不核对。
TEST(WorldFork, ProviderEndpointsAreCheckedAtRegistration) {
    const TempDir tmp;
    auto reg = open_registry(tmp);
    (void)reg.update_world_settings(
        "default", R"({"embedding":{"provider":"openai","provider_name":"p","base_url":"http://h",)"
                   R"("model":"e1","dim":8,"max_tokens":1,"timeout_ms":1000}})");
    (void)world_with_statements(reg, "deploy-1", 2);
    const ProviderEndpoints same{{"p", ProviderEndpoint{.provider = "openai", .base_url = "http://h"}}};
    const ProviderEndpoints moved{{"p", ProviderEndpoint{.provider = "openai", .base_url = "http://moved"}}};
    const auto dirs = world_dirs(reg);

    ForkOptions opts = fork_opts();
    opts.settings_overrides_json = R"({"source_lane":{"k_source":9}})";
    opts.provider_endpoints = moved;
    EXPECT_EQ(code_of([&] { (void)fork_world(reg, "deploy-1", "rollout-1", opts); }),
              "endpoint_changed");
    EXPECT_EQ(world_dirs(reg), dirs);
    EXPECT_EQ(code_of([&] { (void)reg.get("rollout-1"); }), "world_not_found");
    {
        auto writer = persistence::SqliteAdapter::open(reg.get("deploy-1").db_path);
        EXPECT_EQ(code_of([&] {
                      (void)fork_world_from_connection(reg, writer->connection(), "deploy-1",
                                                       "rollout-1", opts);
                  }),
                  "endpoint_changed");
        writer->close();
    }
    EXPECT_EQ(world_dirs(reg), dirs);
    // provider 已被删掉（表里没有这个名字）同样拒绝。
    opts.provider_endpoints = ProviderEndpoints{};
    EXPECT_EQ(code_of([&] { (void)fork_world(reg, "deploy-1", "rollout-1", opts); }),
              "endpoint_changed");
    EXPECT_EQ(world_dirs(reg), dirs);

    opts.provider_endpoints = same;
    const ForkResult forked = fork_world(reg, "deploy-1", "rollout-1", opts);
    EXPECT_EQ(settings_from_json(forked.world.settings_json).source_lane.k_source, 9);
    opts.provider_endpoints = std::nullopt;   // 不给表：不核对
    (void)fork_world(reg, "deploy-1", "rollout-2", opts);
}

// 裁定 R31 M4：分叉看向量后端，非 sqlite 抛 snapshot_unsupported_backend（与快照同一规则、同一个判定函数），
// 在任何文件 I/O 之前。
TEST(WorldFork, NonSqliteVectorBackendIsRefusedBeforeAnyIo) {
    const TempDir tmp;
    auto reg = open_registry(tmp);
    const WorldRecord src = world_with_statements(reg, "deploy-1", 1);
    const SnapshotEntry snap = take_snapshot(reg, "deploy-1", SnapshotOptions{});
    const auto dirs = world_dirs(reg);
    ForkOptions opts = fork_opts();
    opts.vector_backend = "zvec";
    EXPECT_EQ(code_of([&] { (void)fork_world(reg, "deploy-1", "rollout-1", opts); }),
              "snapshot_unsupported_backend");
    opts.snapshot_id = snap.id;
    EXPECT_EQ(code_of([&] { (void)fork_world(reg, "deploy-1", "rollout-1", opts); }),
              "snapshot_unsupported_backend");
    opts.snapshot_id.clear();
    {
        auto writer = persistence::SqliteAdapter::open(src.db_path);
        EXPECT_EQ(code_of([&] {
                      (void)fork_world_from_connection(reg, writer->connection(), "deploy-1",
                                                       "rollout-1", opts);
                  }),
                  "snapshot_unsupported_backend");
        writer->close();
    }
    EXPECT_EQ(world_dirs(reg), dirs);
    EXPECT_EQ(code_of([&] { (void)reg.get("rollout-1"); }), "world_not_found");
    opts.vector_backend = "sqlite";
    (void)fork_world(reg, "deploy-1", "rollout-1", opts);
}

// 裁定 R31 M5、R32：回滚在替换库之后失败时，C++ 用 WorldStoreError::replaced() 告诉调用方「库已替换」，错误码保持
// 原样。替换之前的失败 replaced() 为 false。after_replace_for_test 在 rename 之后、算摘要之前调用。
TEST(WorldRollback, ReplacedMarkerSaysWhetherTheLibraryWasSwapped) {
    const TempDir tmp;
    auto reg = open_registry(tmp);
    const WorldRecord src = world_with_statements(reg, "deploy-1", 2);
    const SnapshotEntry snap = take_snapshot(reg, "deploy-1", SnapshotOptions{});
    insert_statements(src.db_path, src.tenant, 10, 1);
    const auto replaced_of = [&](const RollbackOptions& opts) {
        try {
            (void)rollback_world(reg, "deploy-1", snap.id, opts);
        } catch (const WorldStoreError& err) {
            return std::make_pair(err.code(), err.replaced());
        }
        return std::make_pair(std::string("<no throw>"), false);
    };

    // 替换之前失败（before_replace 抛）：库原样不动，replaced=false。
    RollbackOptions before;
    before.before_replace = [](const fs::path&) { throw std::runtime_error("injected before"); };
    const std::string sha = file_sha256(src.db_path);
    EXPECT_EQ(replaced_of(before), std::make_pair(std::string("snapshot_failed"), false));
    EXPECT_EQ(file_sha256(src.db_path), sha);

    // 替换之后失败（after_replace_for_test 抛）：码仍是 snapshot_failed，replaced=true，库已是快照的内容。
    RollbackOptions after;
    after.after_replace_for_test = [] { throw std::runtime_error("injected after"); };
    EXPECT_EQ(replaced_of(after), std::make_pair(std::string("snapshot_failed"), true));
    EXPECT_EQ(digest_of(src.db_path, src.tenant), snap.digest);

    // restore_digest_mismatch 一定是 replaced=true。
    insert_statements(src.db_path, src.tenant, 20, 1);
    run_sql(snap.path, "UPDATE statements SET object_value='tampered' WHERE id='st-0'");
    EXPECT_EQ(replaced_of(RollbackOptions{}),
              std::make_pair(std::string("restore_digest_mismatch"), true));

    // 别处抛的 WorldStoreError 默认 replaced=false。
    try {
        (void)reg.get("nope");
        FAIL() << "expected world_not_found";
    } catch (const WorldStoreError& err) {
        EXPECT_EQ(err.code(), "world_not_found");
        EXPECT_FALSE(err.replaced());
    }
}

// 裁定 R33（R22 M4）：可回收世界看板只读打开，与只读世界一样不拍自动快照。设置里的后台（继承自来源）原样存着，
// 生效的设置（effective_world_settings，按 decide_open_mode）后台全关，take_auto_snapshot 按它跳过（disabled）。
TEST(WorldAutoSnapshot, ReclaimableWorldIsSkippedLikeReadOnly) {
    const TempDir tmp;
    auto reg = open_registry(tmp);
    const WorldRecord src = world_with_statements(reg, "deploy-1", 2);
    set_auto_snapshot(reg, "deploy-1", AutoSnapshotConfig{.on_session = true, .interval_hours = 1});
    ForkOptions opts = fork_opts();
    opts.reclaimable = true;
    (void)fork_world(reg, "deploy-1", "rollout-1", opts);
    const WorldRecord rec = reg.get("rollout-1");
    ASSERT_EQ(settings_from_json(rec.settings_json).background.auto_snapshot,
              (AutoSnapshotConfig{.on_session = true, .interval_hours = 1}));   // 存着的设置没被改
    const auto background = effective_world_settings(rec).background;
    EXPECT_EQ(background.tick_interval_s, 0.0);
    EXPECT_FALSE(background.ingest);
    EXPECT_EQ(background.auto_snapshot, (AutoSnapshotConfig{.on_session = false, .interval_hours = 0}));

    const auto interval = take_auto_snapshot(reg, "rollout-1",
                                             AutoTrigger{"interval", "", "2030-01-01T00:00:00Z"}, {});
    const auto session = take_auto_snapshot(reg, "rollout-1",
                                            AutoTrigger{"session", "s-new", "2030-01-01T00:00:00Z"}, {});
    for (const auto& result : {interval, session}) {
        EXPECT_FALSE(result.taken.has_value());
        EXPECT_EQ(result.skipped, "disabled");
        EXPECT_TRUE(result.refused.empty());
    }
    EXPECT_TRUE(reg.snapshots("rollout-1").empty());
    // 对照：可写的来源照常拍。
    EXPECT_TRUE(take_auto_snapshot(reg, "deploy-1",
                                   AutoTrigger{"interval", "", "2030-01-01T00:00:00Z"}, {})
                    .taken.has_value());
    EXPECT_EQ(effective_world_settings(reg.get("deploy-1")).background.auto_snapshot,
              (AutoSnapshotConfig{.on_session = true, .interval_hours = 1}));
    (void)src;
}

// 裁定 R31 M7：ForkOptions.now_iso 用 parse_utc_seconds 校验（与 take_snapshot 一致），形状不对抛 invalid_settings，
// 在任何文件 I/O 之前；空串取当前时间。升级种子的 now 走同一条校验。
TEST(WorldFork, MalformedNowIsInvalidSettingsBeforeAnyIo) {
    const TempDir tmp;
    auto reg = open_registry(tmp);
    (void)world_with_statements(reg, "deploy-1", 1);
    const WorldRecord seed = reg.create("seed-v1", "", {}, "{}", kNow, WorldFlags{.seed = true});
    insert_statements(seed.db_path, seed.tenant, 0, 1);
    const auto dirs = world_dirs(reg);
    for (const char* bad : {"yesterday", "2026-11-06 00:00:00", "2026-11-06T00:00:00.123Z",
                            "2026-13-06T00:00:00Z"}) {
        ForkOptions opts;
        opts.now_iso = bad;
        EXPECT_EQ(code_of([&] { (void)fork_world(reg, "deploy-1", "rollout-1", opts); }),
                  "invalid_settings") << bad;
        EXPECT_EQ(code_of([&] { (void)upgrade_seed(reg, "seed-v1", "seed-v2", bad); }),
                  "invalid_settings") << bad;
    }
    EXPECT_EQ(world_dirs(reg), dirs);
    EXPECT_EQ(code_of([&] { (void)reg.get("rollout-1"); }), "world_not_found");
    const ForkResult forked = fork_world(reg, "deploy-1", "rollout-1", ForkOptions{});
    EXPECT_TRUE(std::regex_match(forked.world.created_at,
                                 std::regex(R"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)")))
        << forked.world.created_at;
}

// 裁定 R22 M6：新分叉（与升级种子）的世界登记时带着统计缓存，值在复制出来的库上算，与来源当前的统计相同；
// stats_at 是登记时刻。新世界在列表上不再显示 0。
TEST(WorldFork, RegisteredStatsMatchTheSource) {
    const TempDir tmp;
    auto reg = open_registry(tmp);
    const WorldRecord src = world_with_statements(reg, "deploy-1", 3);
    const WorldStats live = world_stats(src.db_path, src.tenant);
    ASSERT_EQ(live.statements, 3);
    reg.update_stats("deploy-1", live.holders, live.statements, live.last_write_at, kNow);

    const ForkResult forked = fork_world(reg, "deploy-1", "rollout-1", fork_opts());
    const WorldRecord got = reg.get("rollout-1");
    EXPECT_EQ(got.statements, live.statements);
    EXPECT_EQ(got.holders, live.holders);
    EXPECT_EQ(got.last_write_at, live.last_write_at);
    EXPECT_EQ(got.stats_at, kNow);
    EXPECT_EQ(forked.world.statements, live.statements);
    EXPECT_EQ(got.statements, reg.get("deploy-1").statements);

    const WorldRecord old_seed = reg.create("seed-v1", "", {}, "{}", kNow, WorldFlags{.seed = true});
    insert_statements(old_seed.db_path, old_seed.tenant, 0, 2);
    const ForkResult upgraded = upgrade_seed(reg, "seed-v1", "seed-v2", kNow);
    EXPECT_EQ(reg.get("seed-v2").statements, 2);
    EXPECT_EQ(upgraded.world.holders, world_stats(upgraded.world.db_path, old_seed.tenant).holders);

    // create_world（没有统计）照旧是 0 与空串。
    const WorldRecord fresh = reg.create_world("fresh", "", {}, "{}", kNow);
    EXPECT_EQ(fresh.statements, 0);
    EXPECT_EQ(fresh.stats_at, "");
}

}  // namespace starling::governance
