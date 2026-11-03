// RegistryDb（SQLite 注册表 registry.db）：默认世界、命名规则、标签不可变、分页、删除边界、
// 统计与快照条目往返。
#include "starling/governance/registry_db.hpp"

#include <gtest/gtest.h>
#include <sqlite3.h>
#include <unistd.h>

#include <atomic>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <functional>
#include <map>
#include <string>
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
    for (std::size_t idx = 1; idx < all.worlds.size(); ++idx) {
        EXPECT_GE(all.worlds[idx - 1].created_at, all.worlds[idx].created_at);
    }
    EXPECT_EQ(all.worlds.front().name, "r2-3");
    EXPECT_EQ(page.worlds.front().name, all.worlds[2].name);
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

TEST(RegistryDb, CorruptRegistryFileIsReported) {
    const TempDir tmp;
    {
        std::ofstream out(tmp.path() / "registry.db", std::ios::binary);
        out << "this is not a sqlite database, just garbage bytes padding padding padding";
    }
    EXPECT_EQ(code_of([&] { (void)open_db(tmp); }), "registry_corrupt");
}
