// RegistryDb（SQLite 注册表 registry.db）：默认世界、命名规则、标签不可变、分页、删除边界、
// 统计与快照条目往返。
#include "starling/governance/registry_db.hpp"
#include "starling/governance/world_settings.hpp"

#include <gtest/gtest.h>
#include <sqlite3.h>
#include <unistd.h>

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <functional>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace {

namespace fs = std::filesystem;
using namespace starling::governance;

class TempDir {
public:
    TempDir() {
        static std::atomic<int> counter{0};
        path_ = fs::temp_directory_path() / ("starling_registry_db_" + std::to_string(::getpid()) +
                                             "_" + std::to_string(counter.fetch_add(1)));
        fs::remove_all(path_);
        fs::create_directories(path_);
    }
    ~TempDir() { std::error_code err; fs::remove_all(path_, err); }
    TempDir(const TempDir&) = delete;
    TempDir& operator=(const TempDir&) = delete;
    [[nodiscard]] const fs::path& path() const { return path_; }
private:
    fs::path path_;
};

std::string code_of(const std::function<void()>& action) {
    try { action(); } catch (const WorldStoreError& err) { return err.code(); }
    return "<no throw>";
}

RegistryDb open_db(const TempDir& tmp) {
    return RegistryDb::open(tmp.path(), tmp.path() / "dashboard.db", "default");
}

void sql_exec(const fs::path& db_file, const std::string& sql) {
    sqlite3* raw = nullptr;
    ASSERT_EQ(sqlite3_open(db_file.string().c_str(), &raw), SQLITE_OK);
    char* err = nullptr;
    const int rc = sqlite3_exec(raw, sql.c_str(), nullptr, nullptr, &err);
    if (err != nullptr) { sqlite3_free(err); }
    sqlite3_close(raw);
    ASSERT_EQ(rc, SQLITE_OK);
}

constexpr const char* kNow = "2026-11-03T00:00:00Z";

}  // namespace

TEST(RegistryDb, OpenCreatesDefaultWorldOnce) {
    const TempDir tmp;
    { auto reg = open_db(tmp); }
    auto reg = open_db(tmp);
    EXPECT_EQ(reg.default_world(), "default");
    const auto page = reg.list(WorldQuery{});
    EXPECT_EQ(page.total, 1);
    ASSERT_EQ(page.worlds.size(), 1U);
    const auto& dflt = page.worlds.front();
    EXPECT_EQ(dflt.name, "default");
    EXPECT_EQ(dflt.dir_id, "default");
    EXPECT_EQ(dflt.tenant, "default");
    EXPECT_EQ(fs::path(dflt.db_path), tmp.path() / "dashboard.db");
    EXPECT_TRUE(fs::path(dflt.db_path).is_absolute());
    EXPECT_EQ(dflt.label, "主世界");
    EXPECT_EQ(dflt.settings_json, "{}");
    EXPECT_EQ(reg.snapshot_retention(), 5);
    EXPECT_EQ(reg.data_dir(), tmp.path());
    EXPECT_TRUE(fs::exists(tmp.path() / "registry.db"));
}

TEST(RegistryDb, WorldNameRules) {
    EXPECT_TRUE(is_valid_world_name("rise.deploy-S1_a"));
    EXPECT_TRUE(is_valid_world_name("Run-A"));
    EXPECT_TRUE(is_valid_world_name(std::string(128, 'a')));
    EXPECT_FALSE(is_valid_world_name(std::string(129, 'a')));
    EXPECT_FALSE(is_valid_world_name(""));
    EXPECT_FALSE(is_valid_world_name(".hidden"));
    EXPECT_FALSE(is_valid_world_name("-x"));
    EXPECT_FALSE(is_valid_world_name("a/b"));
    EXPECT_FALSE(is_valid_world_name("a b"));
}

TEST(RegistryDb, CaseDistinctNamesGetDistinctDirs) {
    const TempDir tmp;
    auto reg = open_db(tmp);
    const auto upper = reg.create("Run-A", "", {}, "{}", kNow);
    const auto lower = reg.create("run-a", "", {}, "{}", kNow);
    EXPECT_NE(upper.dir_id, lower.dir_id);
    EXPECT_NE(upper.db_path, lower.db_path);
    EXPECT_EQ(upper.dir_id.size(), 16U);
    EXPECT_TRUE(fs::is_directory(tmp.path() / "worlds" / upper.dir_id));
    EXPECT_TRUE(fs::is_directory(tmp.path() / "worlds" / lower.dir_id));
    EXPECT_EQ(fs::path(upper.db_path), tmp.path() / "worlds" / upper.dir_id / "world.db");
    EXPECT_EQ(fs::path(upper.snapshot_dir), tmp.path() / "worlds" / upper.dir_id / "snapshots");
    EXPECT_EQ(upper.tenant, "Run-A");
    EXPECT_EQ(upper.created_at, kNow);
}

TEST(RegistryDb, CreateRejectsDuplicateAndBadName) {
    const TempDir tmp;
    auto reg = open_db(tmp);
    reg.create("Run-A", "", {}, "{}", kNow);
    EXPECT_EQ(code_of([&] { reg.create("Run-A", "", {}, "{}", kNow); }), "world_exists");
    EXPECT_EQ(code_of([&] { reg.create(".x", "", {}, "{}", kNow); }), "invalid_world_name");
    EXPECT_EQ(reg.list(WorldQuery{}).total, 2);
}

// 裁定 R34：create_world 经持久层建好新世界的库文件（迁移建表），建完即关；新世界检视起来是空的，
// 不是「还没有库」。只建注册项的 create 照旧不建库文件。
TEST(RegistryDb, CreateWorldInitialisesTheWorldDatabase) {
    const TempDir tmp;
    auto reg = open_db(tmp);
    const auto created = reg.create_world("w1", "", {{"run_id", "r1"}}, "", kNow);
    const fs::path db_file = created.db_path;
    ASSERT_TRUE(fs::is_regular_file(db_file));
    // 建完即关：不留 WAL 旁车（没有连接还开着这个库）。在本测试自己打开库之前看。
    EXPECT_FALSE(fs::exists(db_file.string() + "-wal"));
    EXPECT_FALSE(fs::exists(db_file.string() + "-shm"));
    sqlite3* raw = nullptr;
    ASSERT_EQ(sqlite3_open_v2(("file:" + db_file.string() + "?mode=ro").c_str(), &raw,
                              SQLITE_OPEN_READONLY | SQLITE_OPEN_URI, nullptr), SQLITE_OK);
    sqlite3_stmt* stmt = nullptr;
    ASSERT_EQ(sqlite3_prepare_v2(raw, "SELECT COUNT(*) FROM statements", -1, &stmt, nullptr),
              SQLITE_OK) << sqlite3_errmsg(raw);
    ASSERT_EQ(sqlite3_step(stmt), SQLITE_ROW);
    EXPECT_EQ(sqlite3_column_int64(stmt, 0), 0);
    sqlite3_finalize(stmt);
    sqlite3_close(raw);
    EXPECT_EQ(reg.get("w1").labels.at("run_id"), "r1");

    const auto plain = reg.create("w2", "", {}, "{}", kNow);
    EXPECT_FALSE(fs::exists(plain.db_path));
}

TEST(RegistryDb, DefaultWorldCannotBeRemoved) {
    const TempDir tmp;
    auto reg = open_db(tmp);
    EXPECT_EQ(code_of([&] { reg.remove("default"); }), "world_protected");
    EXPECT_EQ(reg.default_world(), "default");
    EXPECT_EQ(code_of([&] { reg.remove("nope"); }), "world_not_found");
}

TEST(RegistryDb, LabelsAreImmutable) {
    const TempDir tmp;
    auto reg = open_db(tmp);
    reg.create("a", "", {{"run_id", "r1"}}, "{}", kNow);
    EXPECT_EQ(code_of([&] { reg.set_labels("a", {{"run_id", "r2"}}); }), "labels_immutable");
    EXPECT_EQ(reg.get("a").labels.at("run_id"), "r1");

    reg.create("b", "", {}, "{}", kNow);
    reg.set_labels("b", {{"k", "v"}});
    EXPECT_EQ(reg.get("b").labels.at("k"), "v");
    EXPECT_EQ(code_of([&] { reg.set_labels("b", {{"k", "w"}}); }), "labels_immutable");
}

TEST(RegistryDb, ListFiltersByLabelsAndPages) {
    const TempDir tmp;
    auto reg = open_db(tmp);
    for (int idx = 0; idx < 3; ++idx) {
        reg.create("r1-" + std::to_string(idx), "", {{"run_id", "r1"}}, "{}",
                   "2099-01-01T00:00:0" + std::to_string(idx) + "Z");
    }
    for (int idx = 0; idx < 4; ++idx) {
        reg.create("r2-" + std::to_string(idx), "", {{"run_id", "r2"}}, "{}",
                   "2099-01-01T00:01:0" + std::to_string(idx) + "Z");
    }
    WorldQuery by_label;
    by_label.labels = {{"run_id", "r1"}};
    const auto filtered = reg.list(by_label);
    EXPECT_EQ(filtered.total, 3);
    EXPECT_EQ(filtered.worlds.size(), 3U);

    WorldQuery paged;
    paged.limit = 2;
    paged.offset = 2;
    const auto page = reg.list(paged);
    EXPECT_EQ(page.total, 8);
    ASSERT_EQ(page.worlds.size(), 2U);

    const auto all = reg.list(WorldQuery{.limit = 500});
    for (std::size_t idx = 1; idx < all.worlds.size(); ++idx) {   // 没人写过：按建世界时刻降序
        EXPECT_GE(all.worlds[idx - 1].created_at, all.worlds[idx].created_at);
    }
    EXPECT_EQ(all.worlds.front().name, "r2-3");
    EXPECT_EQ(page.worlds.front().name, all.worlds[2].name);
    // spec §14（审查 M-1）：写过的世界按最近写入降序排在前面，没写过的（last_write_at 为空）在后。
    reg.update_stats("r1-0", 1, 1, "2099-02-01T00:00:00Z", kNow);
    reg.update_stats("r1-2", 1, 1, "2099-03-01T00:00:00Z", kNow);
    const auto by_write = reg.list(WorldQuery{.limit = 500});
    ASSERT_GE(by_write.worlds.size(), 3U);
    EXPECT_EQ(by_write.worlds[0].name, "r1-2");
    EXPECT_EQ(by_write.worlds[1].name, "r1-0");
    EXPECT_EQ(by_write.worlds[2].name, "r2-3");          // 其余照旧按建世界时刻
    EXPECT_EQ(code_of([&] { (void)reg.list(WorldQuery{.limit = 0}); }), "invalid_world_query");
    EXPECT_EQ(code_of([&] { (void)reg.list(WorldQuery{.limit = 501}); }), "invalid_world_query");
}

TEST(RegistryDb, RemoveDeletesWorldDirectoryOnlyUnderWorlds) {
    const TempDir tmp;
    auto reg = open_db(tmp);
    const auto world = reg.create("gone", "", {{"k", "v"}}, "{}", kNow);
    const fs::path dir = tmp.path() / "worlds" / world.dir_id;
    ASSERT_TRUE(fs::exists(dir));
    reg.remove("gone");
    EXPECT_FALSE(fs::exists(dir));
    EXPECT_EQ(code_of([&] { (void)reg.get("gone"); }), "world_not_found");

    // 手改库把 dir_id 改成 "../x"：remove 必须拒绝，且什么都不删。
    const auto victim = reg.create("victim", "", {}, "{}", kNow);
    const fs::path outside = tmp.path() / "x";
    fs::create_directories(outside);
    sql_exec(tmp.path() / "registry.db",
             "UPDATE worlds SET dir_id='../x' WHERE name='victim'");
    EXPECT_EQ(code_of([&] { reg.remove("victim"); }), "snapshot_failed");
    EXPECT_TRUE(fs::exists(outside));
    EXPECT_TRUE(fs::exists(tmp.path() / "worlds" / victim.dir_id));
    EXPECT_EQ(reg.get("victim").name, "victim");
}

TEST(RegistryDb, StatsAndSnapshotsRoundTrip) {
    const TempDir tmp;
    auto reg = open_db(tmp);
    reg.create("w", "", {}, "{}", kNow);
    reg.update_stats("w", 3, 42, "2026-11-03T01:00:00Z", "2026-11-03T02:00:00Z");
    const auto got = reg.get("w");
    EXPECT_EQ(got.holders, 3);
    EXPECT_EQ(got.statements, 42);
    EXPECT_EQ(got.last_write_at, "2026-11-03T01:00:00Z");
    EXPECT_EQ(got.stats_at, "2026-11-03T02:00:00Z");

    reg.update_settings("w", R"({"a":1})");
    EXPECT_EQ(reg.get("w").settings_json, R"({"a":1})");

    SnapshotEntry first;
    first.id = "20261103T000000Z-aaaa";
    first.path = (tmp.path() / "s1").string();
    first.kind = "manual";
    first.reason = "manual";
    first.note = "n1";
    first.created_at = "2026-11-03T00:00:00Z";
    first.size_bytes = 10;
    SnapshotEntry second = first;
    second.id = "20261103T010000Z-bbbb";
    second.kind = "auto";
    second.reason = "session";
    second.session_id = "s-1";
    second.created_at = "2026-11-03T01:00:00Z";
    second.sessions_complete = 7;
    second.content_mark = "mark";
    second.world_time = "2026-11-02T00:00:00Z";
    reg.put_snapshot("w", first);
    reg.put_snapshot("w", second);

    const auto snaps = reg.snapshots("w");
    ASSERT_EQ(snaps.size(), 2U);
    EXPECT_EQ(snaps[0].id, second.id);
    EXPECT_EQ(snaps[1].id, first.id);
    EXPECT_EQ(snaps[0].session_id, "s-1");
    ASSERT_TRUE(snaps[0].sessions_complete.has_value());
    EXPECT_EQ(*snaps[0].sessions_complete, 7);
    EXPECT_FALSE(snaps[1].sessions_complete.has_value());
    EXPECT_EQ(snaps[0].content_mark, "mark");
    EXPECT_EQ(snaps[0].world_time, "2026-11-02T00:00:00Z");
    EXPECT_EQ(snaps[1].size_bytes, 10);

    reg.drop_snapshot("w", first.id);
    EXPECT_EQ(reg.snapshots("w").size(), 1U);
    EXPECT_EQ(code_of([&] { reg.drop_snapshot("w", first.id); }), "snapshot_not_found");
}

// 快照条目的 digest 列往返；第 3 期之前建的注册表（snapshots 没有 digest 列，schema_version 仍是 1）
// 打开时就地加列，旧条目读出空串，重复打开不出错。
TEST(RegistryDb, SnapshotDigestColumnRoundTripsAndOldRegistryGainsIt) {
    const TempDir tmp;
    {
        auto reg = open_db(tmp);
        reg.create("w", "", {}, "{}", kNow);
        SnapshotEntry snap;
        snap.id = "20261103T000000Z-aaaa";
        snap.path = (tmp.path() / "s1").string();
        snap.kind = "held";
        snap.reason = "held";
        snap.created_at = kNow;
        snap.digest = std::string(64, 'd');
        reg.put_snapshot("w", snap);
        EXPECT_EQ(reg.snapshots("w").front().digest, std::string(64, 'd'));
    }
    // 模拟旧注册表：把 snapshots 改回没有 digest 列的形状（保留一条旧条目）。
    sql_exec(tmp.path() / "registry.db",
             "CREATE TABLE snap_old AS SELECT world, id, path, kind, reason, note, created_at, "
             "world_time, size_bytes, sessions_complete, session_id, content_mark, seq FROM snapshots;"
             "DROP TABLE snapshots;"
             "CREATE TABLE snapshots (world TEXT NOT NULL, id TEXT NOT NULL, path TEXT NOT NULL, "
             "kind TEXT NOT NULL, reason TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', "
             "created_at TEXT NOT NULL, world_time TEXT NOT NULL DEFAULT '', "
             "size_bytes INTEGER NOT NULL DEFAULT 0, sessions_complete INTEGER, "
             "session_id TEXT NOT NULL DEFAULT '', content_mark TEXT NOT NULL DEFAULT '', "
             "seq INTEGER NOT NULL, PRIMARY KEY (world, id));"
             "INSERT INTO snapshots SELECT * FROM snap_old; DROP TABLE snap_old;");
    for (int round = 0; round < 2; ++round) {   // 第二次打开时列已在：幂等
        auto reg = open_db(tmp);
        const auto snaps = reg.snapshots("w");
        ASSERT_EQ(snaps.size(), 1U);
        EXPECT_EQ(snaps.front().digest, "");
        EXPECT_EQ(snaps.front().kind, "held");
    }
    auto reg = open_db(tmp);
    SnapshotEntry fresh;
    fresh.id = "20261103T000001Z-bbbb";
    fresh.path = (tmp.path() / "s2").string();
    fresh.kind = "manual";
    fresh.reason = "manual";
    fresh.created_at = kNow;
    fresh.digest = "abc";
    reg.put_snapshot("w", fresh);
    EXPECT_EQ(reg.snapshots("w").front().digest, "abc");
}

// create / create_world 写入 WorldFlags；seed 蕴含 read_only，只读世界的后台强制关。
TEST(RegistryDb, CreateRecordsWorldFlags) {
    const TempDir tmp;
    auto reg = open_db(tmp);
    const auto plain = reg.create("plain", "", {}, "{}", kNow);
    EXPECT_FALSE(plain.seed);
    EXPECT_FALSE(plain.read_only);
    EXPECT_FALSE(plain.reclaimable);
    EXPECT_EQ(plain.retention_group, "");
    const auto rollout = reg.create("rollout", "", {}, "{}", kNow,
                                    WorldFlags{.reclaimable = true, .retention_group = "g1",
                                               .fork_of_world = "plain"});
    EXPECT_TRUE(rollout.reclaimable);
    EXPECT_FALSE(rollout.read_only);
    EXPECT_EQ(rollout.retention_group, "g1");
    EXPECT_EQ(rollout.fork_of_world, "plain");
    const auto seed = reg.create_world("seed", "", {}, "{}", kNow, std::nullopt,
                                       WorldFlags{.seed = true});
    EXPECT_TRUE(seed.seed);
    EXPECT_TRUE(seed.read_only);
    EXPECT_FALSE(settings_from_json(seed.settings_json).background.ingest);
    EXPECT_EQ(reg.get("seed").read_only, true);
}

// 打开方式（spec §6.4）：只读或可回收的世界看板一律只读打开；客户端写入（第 4 期）只对只读世界只读。
TEST(RegistryDb, OpenModeRule) {
    const TempDir tmp;
    auto reg = open_db(tmp);
    const auto plain = reg.create("plain", "", {}, "{}", kNow);
    const auto seed = reg.create("seed", "", {}, "{}", kNow, WorldFlags{.seed = true});
    const auto frozen = reg.create("frozen", "", {}, "{}", kNow, WorldFlags{.read_only = true});
    const auto rollout = reg.create("rollout", "", {}, "{}", kNow, WorldFlags{.reclaimable = true});

    EXPECT_EQ(decide_open_mode(plain, false), OpenMode::ReadWrite);
    EXPECT_EQ(decide_open_mode(seed, false), OpenMode::ReadOnly);
    EXPECT_EQ(decide_open_mode(frozen, false), OpenMode::ReadOnly);
    EXPECT_EQ(decide_open_mode(rollout, false), OpenMode::ReadOnly);
    EXPECT_EQ(decide_open_mode(reg.get("default"), false), OpenMode::ReadWrite);

    // 客户端写入：可回收世界（rollout）可写，种子与只读世界仍只读。
    EXPECT_EQ(decide_open_mode(plain, true), OpenMode::ReadWrite);
    EXPECT_EQ(decide_open_mode(seed, true), OpenMode::ReadOnly);
    EXPECT_EQ(decide_open_mode(frozen, true), OpenMode::ReadOnly);
    EXPECT_EQ(decide_open_mode(rollout, true), OpenMode::ReadWrite);
}

TEST(RegistryDb, CorruptRegistryFileIsReported) {
    const TempDir tmp;
    {
        std::ofstream out(tmp.path() / "registry.db", std::ios::binary);
        out << "this is not a sqlite database, just garbage bytes padding padding padding";
    }
    EXPECT_EQ(code_of([&] { (void)open_db(tmp); }), "registry_corrupt");
}

// ---- 设置组合（裁定 R2）：合并在 C++，读—合并—写在一次持锁内 ----

TEST(WorldSettingsCompose, NewWorldCopiesDefaultSettingsThenOverrides) {
    const TempDir tmp;
    auto reg = open_db(tmp);
    reg.update_world_settings("default", R"({"source_lane":{"k_source":60}})");
    const auto created = reg.create_world("w1", "", {}, R"({"source_lane":{"dim":512}})", kNow);
    const auto got = settings_from_json(created.settings_json);
    EXPECT_EQ(got.source_lane.k_source, 60);   // 默认世界的值被复制
    EXPECT_EQ(got.source_lane.dim, 512);       // overrides 覆盖
    reg.update_world_settings("default", R"({"source_lane":{"k_source":10}})");
    EXPECT_EQ(settings_from_json(reg.get("w1").settings_json).source_lane.k_source, 60);  // 值拷贝
    EXPECT_EQ(settings_from_json(reg.create_world("w2", "", {}, "", kNow).settings_json)
                  .source_lane.k_source, 10);
    EXPECT_EQ(code_of([&] { (void)reg.create_world("w3", "", {}, R"({"nope":1})", kNow); }),
              "invalid_settings");
    EXPECT_EQ(code_of([&] { (void)reg.get("w3"); }), "world_not_found");   // 非法 overrides 什么都不写
    EXPECT_EQ(code_of([&] { (void)reg.create_world("w1", "", {}, "{}", kNow); }), "world_exists");
    EXPECT_EQ(code_of([&] { (void)reg.create_world("Bad Name", "", {}, "{}", kNow); }),
              "invalid_world_name");
}

TEST(WorldSettingsCompose, UpdateMergesOntoCurrentAndPersists) {
    const TempDir tmp;
    auto reg = open_db(tmp);
    reg.create("w1", "", {}, "{}", kNow);
    reg.update_world_settings("w1", R"({"source_lane":{"k_source":7}})");
    const auto merged = reg.update_world_settings("w1", R"({"source_lane":{"dim":256}})");
    const auto got = settings_from_json(reg.get("w1").settings_json);
    EXPECT_EQ(got.source_lane.k_source, 7);    // 前一次的值保留
    EXPECT_EQ(got.source_lane.dim, 256);
    EXPECT_EQ(merged, reg.get("w1").settings_json);
    const auto before = reg.get("w1").settings_json;
    EXPECT_EQ(code_of([&] { (void)reg.update_world_settings("w1", R"({"nope":1})"); }),
              "invalid_settings");
    EXPECT_EQ(reg.get("w1").settings_json, before);   // 非法 overrides 什么都不写
    EXPECT_EQ(code_of([&] { (void)reg.update_world_settings("zz", "{}"); }), "world_not_found");
}

// 并发更新同一世界：每个线程合并一个不同的 gist_thresholds 键，结束后全部都在（不丢合并）。
TEST(WorldSettingsCompose, ConcurrentUpdatesOfOneWorldLoseNoMerge) {
    const TempDir tmp;
    auto reg = open_db(tmp);
    constexpr int kThreads = 8;
    std::vector<std::thread> workers;
    workers.reserve(kThreads);
    for (int idx = 0; idx < kThreads; ++idx) {
        workers.emplace_back([&reg, idx] {
            for (int round = 0; round < 5; ++round) {
                reg.update_world_settings(
                    "default", R"({"gist_thresholds":{"k)" + std::to_string(idx) + R"(":0.5}})");
            }
        });
    }
    for (auto& worker : workers) { worker.join(); }
    const auto got = settings_from_json(reg.get("default").settings_json);
    EXPECT_EQ(got.gist_thresholds.size(), static_cast<std::size_t>(kThreads));
}

// 裁定 R27：RegistryDb::replace_world_sections 在一次持锁内读—整段替换—写，返回新的 settings_json。
TEST(WorldSettingsCompose, ReplaceWorldSectionsReplacesUnderTheRegistryLock) {
    const TempDir tmp;
    auto reg = open_db(tmp);
    (void)reg.update_world_settings(
        "default", R"({"extraction":{"provider_name":"a","base_url":"http://a","model":"ma",)"
                   R"("max_tokens":8192,"thinking_type":"disabled"},)"
                   R"("background":{"auto_snapshot":{"on_session":true,"interval_hours":6}}})");
    const std::string out = reg.replace_world_sections(
        "default", R"({"extraction":{"provider_name":"b","model":"mb"}})");
    const auto got = settings_from_json(reg.get("default").settings_json);
    EXPECT_EQ(settings_to_json(got), out);
    ASSERT_TRUE(got.extraction.has_value());
    EXPECT_EQ(got.extraction->provider_name, "b");
    EXPECT_EQ(got.extraction->base_url, "");
    EXPECT_EQ(got.extraction->max_tokens, EndpointSettings{}.max_tokens);
    EXPECT_EQ(got.extraction->thinking_type, EndpointSettings{}.thinking_type);
    EXPECT_EQ(got.background.auto_snapshot,
              (AutoSnapshotConfig{.on_session = true, .interval_hours = 6}));
    EXPECT_EQ(code_of([&] { (void)reg.replace_world_sections("nope", "{}"); }), "world_not_found");
    const std::string before = reg.get("default").settings_json;
    EXPECT_EQ(code_of([&] { (void)reg.replace_world_sections("default", R"({"x":1})"); }),
              "invalid_settings");
    EXPECT_EQ(reg.get("default").settings_json, before);   // 非法输入什么都不写
}

// 建世界与更新默认世界并发：新世界的 extraction 一定是某一次完整写入的快照（model 与 dim 成对）。
TEST(WorldSettingsCompose, CreateWhileDefaultIsUpdatedSeesOneCompleteSnapshot) {
    const TempDir tmp;
    auto reg = open_db(tmp);
    const auto endpoint = [](int ver) {
        return R"({"extraction":{"provider":"openai","base_url":"http://h","model":"m)" +
               std::to_string(ver) + R"(","dim":)" + std::to_string(ver) +
               R"(,"max_tokens":10,"timeout_ms":1000}})";
    };
    reg.update_world_settings("default", endpoint(0));
    std::atomic<bool> stop{false};
    std::thread updater([&] {
        for (int ver = 1; !stop.load(); ++ver) { reg.update_world_settings("default", endpoint(ver)); }
    });
    for (int idx = 0; idx < 15; ++idx) {
        const auto rec = reg.create_world("w" + std::to_string(idx), "", {}, "", kNow);
        const auto got = settings_from_json(rec.settings_json);
        ASSERT_TRUE(got.extraction.has_value());
        EXPECT_EQ(got.extraction->model, "m" + std::to_string(got.extraction->dim));  // 不撕裂
    }
    stop.store(true);
    updater.join();
}

// 裁定 R40：给出 provider 地址表时，合并结果里继承来的 endpoint（provider_name 非空）在同一次持锁内核对，
// 地址与表不同（或表里没有这个 provider）就 endpoint_changed，什么都不写。
TEST(WorldSettingsCompose, InheritedEndpointMustMatchTheProviderAddress) {
    const TempDir tmp;
    auto reg = open_db(tmp);
    reg.update_world_settings(
        "default", R"({"embedding":{"provider":"openai","provider_name":"p","base_url":"http://h",)"
                   R"("model":"e1","dim":8,"max_tokens":1,"timeout_ms":1000}})");
    const ProviderEndpoints urls{{"p", ProviderEndpoint{.provider = "openai", .base_url = "http://h"}}};
    reg.create_world("w1", "", {}, "", kNow, urls);
    const auto before = reg.get("w1").settings_json;
    const std::string evil = R"({"embedding":{"base_url":"http://evil"}})";

    EXPECT_EQ(code_of([&] { (void)reg.update_world_settings("w1", evil, urls); }),
              "endpoint_changed");
    EXPECT_EQ(reg.get("w1").settings_json, before);                    // 什么都没写
    EXPECT_EQ(code_of([&] { (void)reg.create_world("w2", "", {}, evil, kNow, urls); }),
              "endpoint_changed");
    EXPECT_EQ(code_of([&] { (void)reg.get("w2"); }), "world_not_found");
    std::vector<std::string> dirs;                                     // 没留下 w2 的目录
    for (const auto& entry : fs::directory_iterator(tmp.path() / "worlds")) {
        dirs.push_back(entry.path().filename().string());
    }
    EXPECT_EQ(dirs, std::vector<std::string>{reg.get("w1").dir_id});    // 默认世界的库在原地
    // 表里没有这个 provider（已被删掉）：同样拒绝。
    EXPECT_EQ(code_of([&] { (void)reg.update_world_settings("w1", "{}", ProviderEndpoints{}); }),
              "endpoint_changed");
    // 地址不变的改动照常写；显式端点（provider_name 为空）不核对。
    const auto ok = settings_from_json(
        reg.update_world_settings("w1", R"({"embedding":{"model":"e2"}})", urls));
    EXPECT_EQ(ok.embedding->model, "e2");
    const auto mine = settings_from_json(reg.update_world_settings(
        "w1", R"({"embedding":{"provider_name":"","base_url":"http://mine"}})", urls));
    EXPECT_EQ(mine.embedding->base_url, "http://mine");
    EXPECT_TRUE(mine.embedding->provider_name.empty());
    // 不给地址表：不核对（内部调用方，例如自动快照配置）。
    (void)reg.update_world_settings("w1", R"({"embedding":{"provider_name":"p"}})");
    EXPECT_EQ(settings_from_json(reg.get("w1").settings_json).embedding->provider_name, "p");
}

// 裁定 R51：写设置时的核对比种类与地址两项。provider 的 base_url 为空（种类默认地址），只改了种类（openai →
// anthropic）也是另一家的地址：endpoint_changed，什么都不写。provider 被删掉同样拒绝。
TEST(WorldSettingsCompose, InheritedEndpointRefusedWhenOnlyTheProviderKindChanged) {
    const TempDir tmp;
    auto reg = open_db(tmp);
    reg.update_world_settings(
        "default", R"({"extraction":{"provider":"openai","provider_name":"p","base_url":"",)"
                   R"("model":"m","max_tokens":1,"timeout_ms":1000}})");
    const ProviderEndpoints openai{{"p", ProviderEndpoint{.provider = "openai", .base_url = ""}}};
    const ProviderEndpoints anthropic{
        {"p", ProviderEndpoint{.provider = "anthropic", .base_url = ""}}};
    reg.create_world("w1", "", {}, R"({"extraction":{"model":"m2"}})", kNow, openai);
    const auto before = reg.get("w1").settings_json;
    EXPECT_EQ(code_of([&] { (void)reg.update_world_settings("w1", "{}", anthropic); }),
              "endpoint_changed");
    EXPECT_EQ(reg.get("w1").settings_json, before);
    EXPECT_EQ(code_of([&] {
                  (void)reg.create_world("w2", "", {}, R"({"extraction":{"model":"m2"}})", kNow,
                                         anthropic);
              }),
              "endpoint_changed");
    EXPECT_EQ(code_of([&] { (void)reg.get("w2"); }), "world_not_found");
    EXPECT_EQ(code_of([&] { (void)reg.update_world_settings("w1", "{}", ProviderEndpoints{}); }),
              "endpoint_changed");
    (void)reg.update_world_settings("w1", R"({"extraction":{"model":"m3"}})", openai);
    EXPECT_EQ(settings_from_json(reg.get("w1").settings_json).extraction->model, "m3");
}

// 裁定 R53：重新确认端点。provider_name 非空的角色整段换成 provider 此刻的 endpoint（名字不变），provider 已被删掉的
// 角色变成 null；客户端显式给的端点与没设的角色不动；注册表里不会有密钥。
TEST(WorldSettingsCompose, ReconfirmEndpointsCopiesTheProvidersCurrentEndpoint) {
    const TempDir tmp;
    auto reg = open_db(tmp);
    reg.update_world_settings(
        "default",
        R"({"extraction":{"provider":"openai","provider_name":"p","base_url":"http://h",)"
        R"("model":"m1","max_tokens":1,"timeout_ms":1000},)"
        R"("embedding":{"provider":"openai","provider_name":"gone","base_url":"http://g",)"
        R"("model":"e1","dim":8,"max_tokens":1,"timeout_ms":1000},)"
        R"("chat":{"provider":"openai","provider_name":"","base_url":"http://mine",)"
        R"("model":"c1","max_tokens":1,"timeout_ms":1000}})");
    reg.create_world("w1", "", {}, "", kNow);

    EndpointSettings moved;
    moved.provider = "anthropic";
    moved.provider_name = "ignored";             // 名字以世界里记的为准
    moved.base_url = "";
    moved.model = "m2";
    const std::map<std::string, EndpointSettings> current{{"p", moved}};
    const auto out = settings_from_json(reg.reconfirm_world_endpoints("w1", current));
    ASSERT_TRUE(out.extraction.has_value());
    EXPECT_EQ(out.extraction->provider_name, "p");
    EXPECT_EQ(out.extraction->provider, "anthropic");
    EXPECT_EQ(out.extraction->base_url, "");
    EXPECT_EQ(out.extraction->model, "m2");
    EXPECT_FALSE(out.embedding.has_value());                    // provider 已被删掉 → null
    ASSERT_TRUE(out.chat.has_value());                          // 显式端点不动
    EXPECT_EQ(out.chat->base_url, "http://mine");
    EXPECT_EQ(out.chat->model, "c1");
    EXPECT_FALSE(out.consolidation.has_value());                // 没设的角色不动
    EXPECT_EQ(settings_from_json(reg.get("w1").settings_json), out);
    EXPECT_EQ(reg.get("w1").settings_json.find("api_key"), std::string::npos);
    // 之后按 provider 此刻的端点核对就过了。
    const ProviderEndpoints now_at{{"p", ProviderEndpoint{.provider = "anthropic", .base_url = ""}}};
    EXPECT_NO_THROW((void)reg.update_world_settings("w1", "{}", now_at));
    EXPECT_EQ(code_of([&] { (void)reg.reconfirm_world_endpoints("nope", current); }),
              "world_not_found");
}

// 裁定 R40：客户端显式给的 endpoint 建世界，同时另一个线程反复把默认世界的这个角色绑到 p 再解绑
// （replace_world_sections，即 sync_default_settings）。合并与核对在同一次持锁内：存下来的世界要么是
// 显式端点（provider_name 为空、客户端地址），要么建世界被拒（endpoint_changed）；绝不会出现
// provider_name=p、地址却是客户端给的。
// 两部分：(1) 按步交错（握手）：每一轮先在「已绑定」状态下建一次、再在「已解绑」状态下建一次，两种结果都
// 必然出现；(2) 真并发：绑定线程不停地绑、解绑，同时连续建世界，只断言不变式（结果不受调度影响）。
TEST(WorldSettingsCompose, CreateWithAnExplicitEndpointNeverInheritsAProviderNameAtTheClientUrl) {
    const TempDir tmp;
    auto reg = open_db(tmp);
    const ProviderEndpoints urls{{"p", ProviderEndpoint{.provider = "openai", .base_url = "http://h"}}};
    const std::string bound =
        R"({"embedding":{"provider":"openai","provider_name":"p","base_url":"http://h",)"
        R"("model":"e1","dim":8,"max_tokens":1,"timeout_ms":1000}})";
    const std::string unbound = R"({"embedding":null})";
    const std::string client = R"({"embedding":{"base_url":"http://evil","model":"e","dim":8}})";
    int created = 0;
    int refused = 0;
    int index = 0;
    const auto attempt = [&] {
        try {
            (void)reg.create_world("c" + std::to_string(index++), "", {}, client, kNow, urls);
            ++created;
        } catch (const WorldStoreError& err) {
            ASSERT_EQ(err.code(), "endpoint_changed") << err.what();
            ++refused;
        }
    };

    // (1) 按步交错：绑定线程每次换状态后交出控制权，建世界线程在那个状态下建一次。
    {
        std::mutex step_mutex;
        std::condition_variable step_cv;
        int phase = 0;   // 偶数：轮到绑定线程；奇数：轮到建世界线程
        constexpr int kRounds = 10;
        std::thread binder([&] {
            for (int round = 0; round < 2 * kRounds; ++round) {
                std::unique_lock lock(step_mutex);
                step_cv.wait(lock, [&] { return phase % 2 == 0; });
                (void)reg.replace_world_sections("default", round % 2 == 0 ? bound : unbound);
                ++phase;
                step_cv.notify_all();
            }
        });
        for (int round = 0; round < 2 * kRounds; ++round) {
            std::unique_lock lock(step_mutex);
            step_cv.wait(lock, [&] { return phase % 2 == 1; });
            attempt();
            ++phase;
            step_cv.notify_all();
        }
        binder.join();
        EXPECT_EQ(created, kRounds);    // 解绑状态下建：显式端点照常建
        EXPECT_EQ(refused, kRounds);    // 绑定状态下建：叶合并会叠出 provider_name=p，被拒
    }

    // (2) 真并发：只断言不变式。
    std::atomic<bool> stop{false};
    std::thread binder([&] {
        while (!stop.load()) {
            (void)reg.replace_world_sections("default", bound);
            (void)reg.replace_world_sections("default", unbound);
        }
    });
    for (int round = 0; round < 40; ++round) { attempt(); }
    stop.store(true);
    binder.join();

    std::int64_t seen = 0;
    for (std::int64_t offset = 0;; offset += 500) {
        const auto page = reg.list(WorldQuery{.labels = {}, .offset = offset, .limit = 500});
        for (const auto& rec : page.worlds) {
            ++seen;
            const auto got = settings_from_json(rec.settings_json);
            if (!got.embedding || got.embedding->provider_name != "p") { continue; }
            EXPECT_EQ(got.embedding->base_url, "http://h") << rec.name;   // p 的地址，从不是客户端的
        }
        if (page.worlds.empty() || offset + 500 >= page.total) { break; }
    }
    EXPECT_EQ(seen, created + 1);                                         // 建成的世界加默认世界
}
