// test_world_store.cpp — 世界注册表（W1）、快照（W2）、回滚/归档（W3）与自动快照（W4），
// 注册表为 registry.db（RegistryDb）。
// 设计：docs/superpowers/specs/2026-10-25-world-scene-management-design.md §3/§4/§5.1；
// docs/superpowers/specs/2026-11-03-memory-service-design.md §5、§7。
// 旧 worlds.json 的解析与校验用例在 test_registry_import.cpp（导入），RegistryDb 自身的用例在
// test_registry_db.cpp。

#include "starling/governance/world_store.hpp"

#include "starling/governance/registry_db.hpp"
#include "starling/governance/world_settings.hpp"

#include "starling/memory/memory_ops.hpp"
#include "starling/persistence/sqlite_adapter.hpp"
#include "starling/persistence/stmt.hpp"

#include <gtest/gtest.h>
#include <sqlite3.h>

#include <unistd.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iterator>
#include <limits>
#include <map>
#include <optional>
#include <random>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace starling::governance {
namespace {

namespace fs = std::filesystem;

// 每个用例一个独立的临时目录，析构时删除。
class TempDir {
public:
    TempDir() {
        static std::atomic<int> seq{0};
        path_ = fs::temp_directory_path() /
                ("starling-world-" + std::to_string(::getpid()) + "-" +
                 std::to_string(++seq));
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

std::string slurp(const fs::path& file) {
    std::ifstream input(file, std::ios::binary);
    return {std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>()};
}

std::string code_of(const std::function<void()>& action) {
    try { action(); } catch (const WorldStoreError& err) { return err.code(); }
    return "<no throw>";
}

// 抛出的 WorldStoreError 的完整消息（不抛时为空串）。
std::string what_of(const std::function<void()>& action) {
    try { action(); } catch (const WorldStoreError& err) { return err.what(); }
    return {};
}

TEST(WorldName, AcceptsAndRejects) {
    EXPECT_TRUE(is_valid_world_name("default"));
    EXPECT_TRUE(is_valid_world_name("sim-1"));
    EXPECT_TRUE(is_valid_world_name("a_b"));
    EXPECT_TRUE(is_valid_world_name("0abc"));
    EXPECT_FALSE(is_valid_world_name(""));
    EXPECT_FALSE(is_valid_world_name("-lead"));
    EXPECT_TRUE(is_valid_world_name("Upper"));
    EXPECT_TRUE(is_valid_world_name("rise.deploy-S1_a"));
    EXPECT_FALSE(is_valid_world_name(".hidden"));
    EXPECT_FALSE(is_valid_world_name("has space"));
    EXPECT_FALSE(is_valid_world_name("../escape"));
    EXPECT_FALSE(is_valid_world_name("a/b"));
    EXPECT_FALSE(is_valid_world_name(std::string(129, 'a')));
    EXPECT_TRUE(is_valid_world_name(std::string(128, 'a')));
}

// ---- 注册表（registry.db）助手 ------------------------------------------------

constexpr const char* kCreatedAt = "2026-10-25T01:00:00Z";

RegistryDb open_registry(const TempDir& tmp) {
    return RegistryDb::open(tmp.path(), tmp.path() / "dashboard.db", "default");
}

// 按会话开、按时间关的设置：旧 create_world 的默认值，也是从 worlds.json 导入的非默认世界的值。
// 下面的用例沿用这个配置（新设置的默认值是 {false, 24}，见 WorldAutoConfig 的用例）。
std::string session_settings() {
    WorldSettings settings;
    settings.background.auto_snapshot = AutoSnapshotConfig{.on_session = true, .interval_hours = 0};
    return settings_to_json(settings);
}

void add_world(RegistryDb& reg, const std::string& name) {
    (void)reg.create(name, "", {}, session_settings(), kCreatedAt);
}

RegistryDb registry_with_world(const TempDir& tmp, const std::string& name) {
    auto reg = open_registry(tmp);
    if (name != "default") { add_world(reg, name); }
    return reg;
}

// 世界目录 <data_dir>/worlds/<dir_id>。
fs::path world_dir_of(const RegistryDb& reg, const std::string& name) {
    return reg.data_dir() / "worlds" / reg.get(name).dir_id;
}

// 用一条独立的连接直接改 registry.db（模拟手改过的注册表）。args 依次绑定到 ?1、?2……
void registry_sql(const RegistryDb& reg, const std::string& sql,
                  const std::vector<std::string>& args = {}) {
    sqlite3* raw = nullptr;
    ASSERT_EQ(sqlite3_open((reg.data_dir() / "registry.db").string().c_str(), &raw), SQLITE_OK);
    const persistence::SqliteHandle handle(raw);
    sqlite3_busy_timeout(raw, 5000);
    persistence::Stmt stmt(raw, sql.c_str());
    int idx = 0;
    for (const auto& arg : args) { persistence::detail::bind_sv(stmt.raw(), ++idx, arg); }
    stmt.step_done();
}

void set_retention(const RegistryDb& reg, int retention) {
    registry_sql(reg, "UPDATE meta SET value=?1 WHERE key='snapshot_retention'",
                 {std::to_string(retention)});
}

// 改世界的一列（db_path、snapshot_dir、dir_id、settings_json……）。
void set_world_column(const RegistryDb& reg, const std::string& world, const std::string& column,
                      const std::string& value) {
    registry_sql(reg, "UPDATE worlds SET " + column + "=?1 WHERE name=?2", {value, world});
}

// 改一份快照条目的一列。
void set_snapshot_column(const RegistryDb& reg, const std::string& world, const std::string& snap_id,
                         const std::string& column, const std::string& value) {
    registry_sql(reg, "UPDATE snapshots SET " + column + "=?1 WHERE world=?2 AND id=?3",
                 {value, world, snap_id});
}

// 把该世界的快照条目整个换成 newest_first（最新在前），按登记顺序写回：最旧的先登记。
void set_snapshots(RegistryDb& reg, const std::string& world,
                   const std::vector<SnapshotEntry>& newest_first) {
    registry_sql(reg, "DELETE FROM snapshots WHERE world=?1", {world});
    for (auto iter = newest_first.rbegin(); iter != newest_first.rend(); ++iter) {
        reg.put_snapshot(world, *iter);
    }
}

// 在该世界现有快照之后追加一份更旧的条目。
void append_oldest(RegistryDb& reg, const std::string& world, const SnapshotEntry& snap) {
    auto list = reg.snapshots(world);
    list.push_back(snap);
    set_snapshots(reg, world, list);
}

// 注册表的全部内容（世界记录与快照条目），用来断言「注册表没变」。
std::string registry_dump(const RegistryDb& reg) {
    std::string out;
    for (const auto& rec : reg.list(WorldQuery{.limit = 500}).worlds) {
        out += rec.name + "|" + rec.dir_id + "|" + rec.tenant + "|" + rec.db_path + "|" +
               rec.snapshot_dir + "|" + rec.label + "|" + rec.created_at + "|" + rec.settings_json + "\n";
        for (const auto& snap : reg.snapshots(rec.name)) {
            out += "  " + snap.id + "|" + snap.path + "|" + snap.kind + "|" + snap.reason + "|" +
                   snap.note + "|" + snap.created_at + "|" + snap.world_time + "|" +
                   std::to_string(snap.size_bytes) + "|" +
                   (snap.sessions_complete ? std::to_string(*snap.sessions_complete) : "-") + "|" +
                   snap.session_id + "|" + snap.content_mark + "\n";
        }
    }
    return out;
}

// ---- 世界的删除边界（RegistryDb::remove 的布局检查；原 delete_world 的用例） -------------------

// 删除世界：default 受保护、不存在报 world_not_found；删掉之后世界目录与它的快照条目都不在了。
TEST(WorldRegistry, RemoveGuardsAndDropsSnapshots) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    EXPECT_EQ(code_of([&] { reg.remove("default"); }), "world_protected");
    EXPECT_EQ(code_of([&] { reg.remove("nope"); }), "world_not_found");
    // 先放一条快照记录，否则下面「快照列表随世界一起删除」的断言永远成立。
    SnapshotEntry snap;
    snap.id = "s1";
    snap.kind = "manual";
    snap.reason = "manual";
    reg.put_snapshot("sim-1", snap);
    ASSERT_EQ(reg.snapshots("sim-1").size(), 1U);
    const auto dir = world_dir_of(reg, "sim-1");
    ASSERT_TRUE(fs::exists(dir));
    reg.remove("sim-1");
    EXPECT_EQ(code_of([&] { (void)reg.get("sim-1"); }), "world_not_found");
    EXPECT_EQ(code_of([&] { (void)reg.snapshots("sim-1"); }), "world_not_found");
    // 同名世界重建后不会继承旧的快照条目。
    add_world(reg, "sim-1");
    EXPECT_TRUE(reg.snapshots("sim-1").empty());
    EXPECT_FALSE(fs::exists(dir));
}

TEST(WorldRegistry, DeleteRefusesPathOutsideRegistryDir) {
    const TempDir tmp;
    const TempDir outside;
    auto reg = registry_with_world(tmp, "sim-1");
    // 模拟被篡改的注册表：db_path 指到外部目录。删除目标固定为 worlds/<dir_id>，不读这两个路径。
    set_world_column(reg, "sim-1", "db_path", (outside.path() / "world.db").string());
    set_world_column(reg, "sim-1", "snapshot_dir", (outside.path() / "snapshots").string());
    { std::ofstream out(outside.path() / "keep.txt"); out << "x"; }
    reg.remove("sim-1");
    EXPECT_TRUE(fs::exists(outside.path() / "keep.txt"));   // 外部文件不受影响
    EXPECT_EQ(code_of([&] { (void)reg.get("sim-1"); }), "world_not_found");
}

// worlds/<dir_id> 若是指向外部的符号链接（worlds/ 本身是真目录），remove_within 解析后发现越界，
// 什么都不删；删除必须 fail closed——抛 snapshot_failed、注册项保留。
TEST(WorldRegistry, DeleteRefusesSymlinkEscapingRegistryDir) {
    const TempDir tmp;
    const TempDir outside;
    auto reg = registry_with_world(tmp, "sim-1");
    const fs::path home = world_dir_of(reg, "sim-1");
    fs::remove_all(home);
    fs::create_directory_symlink(outside.path(), home);
    { std::ofstream out(outside.path() / "keep.txt"); out << "x"; }
    EXPECT_EQ(code_of([&] { reg.remove("sim-1"); }), "snapshot_failed");
    EXPECT_TRUE(fs::exists(outside.path() / "keep.txt"));
    EXPECT_EQ(reg.get("sim-1").name, "sim-1");
}

// 路径解析本身出错（这里用自指符号链接触发 ELOOP）时无法判断是否越界：
// 必须 fail loud 并保留注册项，不能悄悄摘掉注册项而把数据遗弃在磁盘上。
TEST(WorldRegistry, DeleteFailsLoudWhenPathCannotBeResolved) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    SnapshotEntry snap;
    snap.id = "s1";
    snap.kind = "manual";
    snap.reason = "manual";
    reg.put_snapshot("sim-1", snap);
    const fs::path home = world_dir_of(reg, "sim-1");
    fs::remove_all(home);
    fs::create_symlink(home.filename(), home);   // 指向自己
    EXPECT_EQ(code_of([&] { reg.remove("sim-1"); }), "snapshot_failed");
    EXPECT_EQ(reg.get("sim-1").name, "sim-1");
    EXPECT_EQ(reg.snapshots("sim-1").size(), 1U);
}

// ---- W2：快照、保留策略与世界统计 ------------------------------------------

// 插入一行 engram eng-<idx>。
void insert_engram(sqlite3* dbh, const std::string& tenant, int idx) {
    // source_item_id 必须各不相同：idx_engrams_source_identity 是
    // (tenant_id, adapter_name, source_item_id, source_version, chunk_index) 唯一索引，
    // 只填必填列时后四列全是默认值 ''/0，第二行就会撞唯一约束（已实测）。
    persistence::Stmt ins(dbh,
        "INSERT INTO engrams(id,tenant_id,content_hash,source_kind,ingest_policy,"
        "ingest_mode,privacy_class,retention_mode,created_at,source_item_id) "
        "VALUES(?1,?2,?3,'conversation','default','inline','normal','audit_retain',?4,?1)");
    ins.bind("eng-" + std::to_string(idx), tenant, "h" + std::to_string(idx),
             "2026-10-2" + std::to_string(idx % 5) + "T00:00:00Z");
    ins.step_done();
}

// 在世界库里追加 engram 行 eng-<first> .. eng-<first+count-1>（库不存在时按真实迁移建库）。
void append_engrams(const fs::path& db_path, const std::string& tenant, int first, int count) {
    auto adapter = persistence::SqliteAdapter::open(db_path);
    for (int idx = first; idx < first + count; ++idx) {
        insert_engram(adapter->connection().raw(), tenant, idx);
    }
}

// 用真实迁移建一个有数据的世界库，保证快照测的是 Starling 真实的 schema。
void seed_world_db(const fs::path& db_path, const std::string& tenant, int engram_rows) {
    append_engrams(db_path, tenant, 0, engram_rows);
}

std::int64_t count_rows(const std::string& db_path, const char* sql) {
    sqlite3* raw = nullptr;
    EXPECT_EQ(sqlite3_open_v2(("file:" + db_path + "?mode=ro").c_str(), &raw,
                              SQLITE_OPEN_READONLY | SQLITE_OPEN_URI, nullptr), SQLITE_OK);
    persistence::SqliteHandle handle(raw);
    persistence::Stmt query(raw, sql);
    EXPECT_TRUE(query.step_row());
    return sqlite3_column_int64(query.raw(), 0);
}

TEST(WorldSnapshot, ProducesIntactCompactCopy) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto world = reg.get("sim-1");
    seed_world_db(world.db_path, "sim-1", 5);

    const auto snap = take_snapshot(reg, "sim-1", SnapshotOptions{});
    EXPECT_EQ(snap.kind, "manual");
    EXPECT_TRUE(fs::exists(snap.path));
    EXPECT_GT(snap.size_bytes, 0);
    EXPECT_EQ(snap.world_time, "2026-10-24T00:00:00Z");   // engrams MAX(created_at)
    EXPECT_EQ(fs::path(snap.path).parent_path(), fs::path(world.snapshot_dir));
    EXPECT_EQ(count_rows(snap.path, "SELECT COUNT(*) FROM engrams"), 5);
    EXPECT_EQ(count_rows(snap.path, "SELECT COUNT(*) FROM pragma_integrity_check "
                                    "WHERE integrity_check='ok'"), 1);
    // 不留 partial。
    for (const auto& entry : fs::directory_iterator(world.snapshot_dir)) {
        EXPECT_EQ(entry.path().extension(), ".db") << entry.path();
    }
    ASSERT_EQ(reg.snapshots("sim-1").size(), 1U);
    EXPECT_EQ(reg.snapshots("sim-1").front().id, snap.id);
}

TEST(WorldSnapshot, SnapshotIsIndependentOfLaterWrites) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto world = reg.get("sim-1");
    seed_world_db(world.db_path, "sim-1", 2);
    const auto snap = take_snapshot(reg, "sim-1", SnapshotOptions{});
    seed_world_db(world.db_path, "sim-1", 0);   // 重开库不改变行
    {
        auto adapter = persistence::SqliteAdapter::open(world.db_path);
        persistence::Stmt del(adapter->connection().raw(), "DELETE FROM engrams");
        del.step_done();
    }
    EXPECT_EQ(count_rows(world.db_path, "SELECT COUNT(*) FROM engrams"), 0);
    EXPECT_EQ(count_rows(snap.path, "SELECT COUNT(*) FROM engrams"), 2);
}

TEST(WorldSnapshot, TakenWhileWriterHoldsOpenTransaction) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto world = reg.get("sim-1");
    seed_world_db(world.db_path, "sim-1", 1);
    auto adapter = persistence::SqliteAdapter::open(world.db_path);
    adapter->connection().begin_immediate();
    persistence::Stmt ins(adapter->connection().raw(),
        "INSERT INTO engrams(id,tenant_id,content_hash,source_kind,ingest_policy,"
        "ingest_mode,privacy_class,retention_mode,created_at,source_item_id) "
        "VALUES('uncommitted','sim-1','hx','conversation','default','inline','normal',"
        "'audit_retain','2026-10-25T00:00:00Z','uncommitted')");
    ins.step_done();
    const auto snap = take_snapshot(reg, "sim-1", SnapshotOptions{});   // 不能报错
    adapter->connection().rollback();
    EXPECT_EQ(count_rows(snap.path, "SELECT COUNT(*) FROM engrams"), 1);   // 不含未提交行
}

TEST(WorldSnapshot, PathWithSpacesAndQuotes) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    set_world_column(reg, "sim-1", "snapshot_dir",
                     (world_dir_of(reg, "sim-1") / "it's a dir").string());
    const auto world = reg.get("sim-1");
    fs::create_directories(world.snapshot_dir);
    seed_world_db(world.db_path, "sim-1", 1);
    const auto snap = take_snapshot(reg, "sim-1", SnapshotOptions{});
    EXPECT_TRUE(fs::exists(snap.path));
}

TEST(WorldSnapshot, DiskGuardRefusesWithoutWriting) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto world = reg.get("sim-1");
    seed_world_db(world.db_path, "sim-1", 3);
    SnapshotOptions opts;
    opts.free_space = [](const fs::path&) -> std::int64_t { return 1; };
    EXPECT_EQ(code_of([&] { (void)take_snapshot(reg, "sim-1", opts); }), "insufficient_disk");
    EXPECT_TRUE(fs::is_empty(world.snapshot_dir));
    EXPECT_EQ(reg.snapshots("sim-1").empty(), true);
}

TEST(WorldSnapshot, UnknownWorldAndMissingDb) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    EXPECT_EQ(code_of([&] { (void)take_snapshot(reg, "nope", SnapshotOptions{}); }),
              "world_not_found");
    // 库文件还没创建（世界从未激活）：拒绝，并且不留 partial。
    EXPECT_EQ(code_of([&] { (void)take_snapshot(reg, "sim-1", SnapshotOptions{}); }),
              "snapshot_failed");
    EXPECT_TRUE(fs::is_empty(reg.get("sim-1").snapshot_dir));
}

// 保留策略不需要真实快照文件，构造注册项即可。reason 取与 kind 配对的值（auto 默认 interval），
// 否则 validate_world 把条目当成损坏（裁定 R12：删文件的入口先跑它）。
SnapshotEntry fake_snapshot(const fs::path& dir, const std::string& stamp,
                            const std::string& kind) {
    SnapshotEntry snap;
    snap.id = stamp;
    snap.path = (dir / (stamp + ".db")).string();
    snap.kind = kind;
    snap.reason = kind == "auto" ? "interval" : kind;
    snap.created_at = stamp;
    { std::ofstream out(snap.path); out << "x"; }
    return snap;
}

std::vector<std::string> ids_of(const std::vector<SnapshotEntry>& snaps) {
    std::vector<std::string> out;
    for (const auto& snap : snaps) { out.push_back(snap.id); }
    return out;
}

TEST(WorldRetention, PrefersDroppingAutoKeepsNewestAuto) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    set_retention(reg, 3);
    const fs::path dir = reg.get("sim-1").snapshot_dir;
    // 列表最新在前：t5..t1
    set_snapshots(reg, "sim-1", {
        fake_snapshot(dir, "t5", "manual"), fake_snapshot(dir, "t4", "auto"),
        fake_snapshot(dir, "t3", "manual"), fake_snapshot(dir, "t2", "auto"),
        fake_snapshot(dir, "t1", "manual"),
    });
    const auto removed = apply_retention(reg, "sim-1");
    // 超出 2 份：先删最旧的 auto t2；auto 只剩 t4，必须保留；再删最旧的 manual t1。
    EXPECT_EQ(removed, (std::vector<std::string>{"t2", "t1"}));
    EXPECT_EQ(ids_of(reg.snapshots("sim-1")), (std::vector<std::string>{"t5", "t4", "t3"}));
    EXPECT_FALSE(fs::exists(dir / "t2.db"));
    EXPECT_FALSE(fs::exists(dir / "t1.db"));
    EXPECT_TRUE(fs::exists(dir / "t4.db"));
}

TEST(WorldRetention, AllManualDropsOldest) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    set_retention(reg, 2);
    const fs::path dir = reg.get("sim-1").snapshot_dir;
    set_snapshots(reg, "sim-1", {fake_snapshot(dir, "t3", "manual"),
                              fake_snapshot(dir, "t2", "manual"),
                              fake_snapshot(dir, "t1", "manual")});
    EXPECT_EQ(apply_retention(reg, "sim-1"), (std::vector<std::string>{"t1"}));
    EXPECT_EQ(ids_of(reg.snapshots("sim-1")), (std::vector<std::string>{"t3", "t2"}));
}

// 未超限时一个都不能删。用 2 份 manual（保留 5）：若去掉「总数 <= N 不删」的守卫，
// 第一轮没有 auto 可删，第二轮会把 manual 全删掉，本用例就会失败。
// （只放一份 auto 是个空洞用例：它本身就是回滚锚点，守卫在不在都不会被删。）
TEST(WorldRetention, UnderLimitIsNoop) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const fs::path dir = reg.get("sim-1").snapshot_dir;
    set_snapshots(reg, "sim-1", {fake_snapshot(dir, "t2", "manual"), fake_snapshot(dir, "t1", "manual")});
    EXPECT_TRUE(apply_retention(reg, "sim-1").empty());
    EXPECT_EQ(ids_of(reg.snapshots("sim-1")), (std::vector<std::string>{"t2", "t1"}));
    EXPECT_TRUE(fs::exists(dir / "t1.db"));
    EXPECT_TRUE(fs::exists(dir / "t2.db"));
}

// 恰好等于保留份数：不删（边界是 <=，不是 <）。
TEST(WorldRetention, ExactlyAtLimitIsNoop) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    set_retention(reg, 3);
    const fs::path dir = reg.get("sim-1").snapshot_dir;
    set_snapshots(reg, "sim-1", {fake_snapshot(dir, "t3", "manual"), fake_snapshot(dir, "t2", "auto"),
                              fake_snapshot(dir, "t1", "manual")});
    EXPECT_TRUE(apply_retention(reg, "sim-1").empty());
    EXPECT_EQ(reg.snapshots("sim-1").size(), 3U);
    EXPECT_TRUE(fs::exists(dir / "t1.db"));
}

TEST(WorldRetention, TakeSnapshotAppliesRetention) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    set_retention(reg, 2);
    seed_world_db(reg.get("sim-1").db_path, "sim-1", 1);
    SnapshotEntry latest;
    for (int idx = 0; idx < 3; ++idx) { latest = take_snapshot(reg, "sim-1", SnapshotOptions{}); }
    EXPECT_EQ(reg.snapshots("sim-1").size(), 2U);
    // 新快照必须插在列表开头（最新在前）。若误用 push_back，它会成为「最旧」的那份，
    // 在修剪时被删掉：返回的条目指向已不存在的文件，且不在 front()。
    EXPECT_TRUE(fs::exists(latest.path));
    EXPECT_EQ(reg.snapshots("sim-1").front().id, latest.id);
    std::size_t files = 0;
    for (const auto& entry : fs::directory_iterator(reg.get("sim-1").snapshot_dir)) {
        (void)entry;
        ++files;
    }
    EXPECT_EQ(files, 2U);
}

TEST(WorldSnapshotDelete, RemovesEntryAndFile) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    seed_world_db(reg.get("sim-1").db_path, "sim-1", 1);
    const auto snap = take_snapshot(reg, "sim-1", SnapshotOptions{});
    EXPECT_EQ(code_of([&] { delete_snapshot(reg, "sim-1", "missing"); }), "snapshot_not_found");
    delete_snapshot(reg, "sim-1", snap.id);
    EXPECT_FALSE(fs::exists(snap.path));
    EXPECT_TRUE(reg.snapshots("sim-1").empty());
}

TEST(WorldStats, CountsTenantRowsAndHandlesMissingDb) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto world = reg.get("sim-1");
    const auto empty = world_stats(world.db_path, "sim-1");   // 文件不存在
    EXPECT_EQ(empty.db_bytes, 0);
    EXPECT_EQ(empty.engrams, 0);
    seed_world_db(world.db_path, "sim-1", 4);
    const auto stats = world_stats(world.db_path, "sim-1");
    EXPECT_EQ(stats.engrams, 4);
    EXPECT_EQ(stats.statements, 0);
    EXPECT_GT(stats.db_bytes, 0);
    EXPECT_GT(stats.live_bytes, 0);
    EXPECT_EQ(world_stats(world.db_path, "other").engrams, 0);   // 按租户过滤
}

// ---- W2 补充：简报之外的边界 ------------------------------------------------

void run_sql(sqlite3* handle, const char* sql) {
    char* msg = nullptr;
    const int code = sqlite3_exec(handle, sql, nullptr, nullptr, &msg);
    const std::string detail = msg != nullptr ? msg : "";
    sqlite3_free(msg);
    ASSERT_EQ(code, SQLITE_OK) << detail;
}

// 在已迁移的世界库上执行 SQL。
void exec_sql(const std::string& db_path, const char* sql) {
    auto adapter = persistence::SqliteAdapter::open(db_path);
    run_sql(adapter->connection().raw(), sql);
}

// 不经迁移，直接建一个只有手写表的「旧库」。
void exec_sql_unmigrated(const std::string& db_path, const char* sql) {
    sqlite3* raw = nullptr;
    ASSERT_EQ(sqlite3_open(db_path.c_str(), &raw), SQLITE_OK);
    const persistence::SqliteHandle handle(raw);
    run_sql(raw, sql);
}

// source_unit_meta 有非空 observed_at 时，水位取它而不是 engrams.created_at。
TEST(WorldSnapshot, WorldTimePrefersSourceUnitMetaObservedAt) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto world = reg.get("sim-1");
    seed_world_db(world.db_path, "sim-1", 2);   // engrams MAX(created_at) = 2026-10-21
    exec_sql(world.db_path,
             "INSERT INTO source_unit_meta(tenant_id,engram_ref,clause_id,speaker,session_id,"
             "audience_json,observed_at) VALUES('sim-1','eng-0','c0','a','s1','[]',"
             "'2031-01-02T03:04:05Z'),('sim-1','eng-1','c0','a','s1','[]',NULL),"
             "('sim-1','eng-1','c1','a','s1','[]','')");
    EXPECT_EQ(snapshot_world_time(world.db_path, "sim-1"), "2031-01-02T03:04:05Z");
    EXPECT_EQ(snapshot_world_time(world.db_path, "other"), "");   // 按租户过滤
    EXPECT_EQ(take_snapshot(reg, "sim-1", SnapshotOptions{}).world_time,
              "2031-01-02T03:04:05Z");
}

// 迁移停在 36 的库没有 source_unit_meta：不能靠 SQL 报错，要回落到 engrams。
TEST(WorldSnapshot, WorldTimeOnDbWithoutSourceUnitMetaTable) {
    const TempDir tmp;
    const std::string db_path = (tmp.path() / "legacy.db").string();
    exec_sql_unmigrated(db_path,
             "CREATE TABLE engrams(id TEXT, tenant_id TEXT, created_at TEXT);"
             "INSERT INTO engrams VALUES('a','t1','2026-01-01T00:00:00Z'),"
             "('b','t1','2026-03-01T00:00:00Z'),('c','t2','2027-01-01T00:00:00Z');");
    EXPECT_EQ(snapshot_world_time(db_path, "t1"), "2026-03-01T00:00:00Z");
    const auto stats = world_stats(db_path, "t1");   // 没有 statements 表
    EXPECT_EQ(stats.engrams, 2);
    EXPECT_EQ(stats.statements, 0);
    EXPECT_EQ(snapshot_world_time((tmp.path() / "absent.db").string(), "t1"), "");
}

// db_path 里的 % ? # 不能被当成 URI 语法（目录放在世界目录之内，布局校验照常通过）。
TEST(WorldSnapshot, DbPathWithUriSpecialCharacters) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const fs::path odd_dir = world_dir_of(reg, "sim-1") / "a#b?c%d";
    fs::create_directories(odd_dir);
    set_world_column(reg, "sim-1", "db_path", (odd_dir / "w%41.db").string());
    const auto world = reg.get("sim-1");
    seed_world_db(world.db_path, "sim-1", 2);
    EXPECT_EQ(world_stats(world.db_path, "sim-1").engrams, 2);
    const auto snap = take_snapshot(reg, "sim-1", SnapshotOptions{});
    EXPECT_EQ(count_rows(snap.path, "SELECT COUNT(*) FROM engrams"), 2);
}

// 磁盘守卫看活数据量，不看文件大小：删表后文件大、活数据小。
TEST(WorldSnapshot, DiskGuardUsesLiveBytesNotFileSize) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto world = reg.get("sim-1");
    seed_world_db(world.db_path, "sim-1", 1);
    exec_sql(world.db_path,
             "CREATE TABLE junk(blob BLOB);"
             "WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n<400) "
             "INSERT INTO junk SELECT zeroblob(4096) FROM seq;"
             "DROP TABLE junk;");
    const auto stats = world_stats(world.db_path, "sim-1");
    ASSERT_GT(stats.db_bytes, stats.live_bytes * 3) << "freelist 没有撑大文件";

    SnapshotOptions tight;   // 恰好 3 倍活数据量：够
    tight.free_space = [&](const fs::path&) { return 3 * stats.live_bytes; };
    const auto snap = take_snapshot(reg, "sim-1", tight);
    EXPECT_TRUE(fs::exists(snap.path));
    EXPECT_LT(snap.size_bytes, stats.db_bytes);   // 快照是紧凑的

    SnapshotOptions short_by_one;   // 少 1 字节：拒
    short_by_one.free_space = [&](const fs::path&) { return 3 * stats.live_bytes - 1; };
    EXPECT_EQ(code_of([&] { (void)take_snapshot(reg, "sim-1", short_by_one); }),
              "insufficient_disk");
    EXPECT_EQ(reg.snapshots("sim-1").size(), 1U);
}

// 不是 SQLite 的文件：snapshot_failed，不留 partial，不登记。
TEST(WorldSnapshot, CorruptDbFailsWithoutPartialOrEntry) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto world = reg.get("sim-1");
    { std::ofstream out(world.db_path); out << "this is not a sqlite database at all, nope"; }
    EXPECT_EQ(code_of([&] { (void)take_snapshot(reg, "sim-1", SnapshotOptions{}); }),
              "snapshot_failed");
    EXPECT_TRUE(fs::is_empty(world.snapshot_dir));
    EXPECT_TRUE(reg.snapshots("sim-1").empty());
}

// 真实的 VACUUM 失败：库的中间几页被写坏（第 1 页完好，所以 page_count 等头部
// 信息仍可读，守卫和打开都通过），VACUUM INTO 读到坏页时报 "malformed"，并且此时
// SQLite 已经创建了 0 字节的 .partial。必须删掉它，也不能登记。
// 这是唯一真正走到 catch 里 fs::remove(partial) 的用例；上面那个「不是 SQLite 的
// 文件」在 PRAGMA page_count 就失败，partial 还没赋值，删掉清理代码它也照样通过。
TEST(WorldSnapshot, VacuumFailureLeavesNoPartialAndNoEntry) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto world = reg.get("sim-1");
    seed_world_db(world.db_path, "sim-1", 50);
    ASSERT_GT(fs::file_size(world.db_path), 16U * 4096U);

    {   // 第 3~10 页（0 起算偏移 2*4096 起）写入确定性的随机字节。
        std::mt19937 gen(12345);
        std::fstream file(world.db_path, std::ios::in | std::ios::out | std::ios::binary);
        ASSERT_TRUE(file.is_open());
        file.seekp(2 * 4096);
        for (int idx = 0; idx < 8 * 4096; ++idx) {
            file.put(static_cast<char>(gen() & 0xFFU));
        }
    }

    EXPECT_EQ(code_of([&] { (void)take_snapshot(reg, "sim-1", SnapshotOptions{}); }),
              "snapshot_failed");
    EXPECT_TRUE(fs::is_empty(world.snapshot_dir))
        << "VACUUM 失败后残留了文件";
    EXPECT_TRUE(reg.snapshots("sim-1").empty());
}

// kind 拼错不能悄悄当成 manual：保留策略只认 "auto"，会丢掉回滚锚点保护。
TEST(WorldSnapshot, RejectsUnknownKindBeforeWriting) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    seed_world_db(reg.get("sim-1").db_path, "sim-1", 1);
    for (const char* bad : {"Auto", "AUTO", "manual ", "", "scene"}) {
        SnapshotOptions opts;
        opts.kind = bad;
        EXPECT_EQ(code_of([&] { (void)take_snapshot(reg, "sim-1", opts); }),
                  "invalid_snapshot_kind") << "kind='" << bad << "'";
    }
    EXPECT_TRUE(fs::is_empty(reg.get("sim-1").snapshot_dir));
    EXPECT_TRUE(reg.snapshots("sim-1").empty());
}

// R3：kind 与 reason 不配对时，take_snapshot 在任何 I/O 之前抛 invalid_snapshot_kind。以前要到
// 保存注册表时才报 registry_corrupt，那时快照文件已经写出。「之前」由三点证明：default 世界的库
// 文件不存在（否则报 snapshot_failed）、它的快照目录没被建出来、磁盘守卫一次都没被调用。
TEST(WorldSnapshot, RejectsMismatchedKindAndReasonBeforeAnyIo) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    seed_world_db(reg.get("sim-1").db_path, "sim-1", 1);
    int probed = 0;
    for (const auto& [kind, reason] : std::vector<std::pair<std::string, std::string>>{
             {"manual", "session"}, {"manual", "interval"}, {"manual", ""},
             {"auto", "manual"}, {"auto", "scene_boundary"}, {"auto", ""}, {"auto", "Session"}}) {
        SnapshotOptions opts;
        opts.kind = kind;
        opts.reason = reason;
        opts.free_space = [&probed](const fs::path&) -> std::int64_t {
            ++probed;
            return std::numeric_limits<std::int64_t>::max();
        };
        for (const char* world : {"sim-1", "default"}) {
            EXPECT_EQ(code_of([&] { (void)take_snapshot(reg, world, opts); }),
                      "invalid_snapshot_kind") << world << " " << kind << "/" << reason;
        }
    }
    EXPECT_EQ(probed, 0);
    EXPECT_TRUE(fs::is_empty(reg.get("sim-1").snapshot_dir));
    EXPECT_FALSE(fs::exists(reg.get("default").snapshot_dir));
    EXPECT_TRUE(reg.snapshots("sim-1").empty());
    EXPECT_TRUE(reg.snapshots("default").empty());
    // 三种合法配对照常拍。
    for (const auto& [kind, reason] : std::vector<std::pair<std::string, std::string>>{
             {"manual", "manual"}, {"auto", "session"}, {"auto", "interval"}}) {
        SnapshotOptions opts;
        opts.kind = kind;
        opts.reason = reason;
        const auto snap = take_snapshot(reg, "sim-1", opts);
        EXPECT_EQ(snap.kind, kind);
        EXPECT_EQ(snap.reason, reason);
    }
    EXPECT_EQ(reg.snapshots("sim-1").size(), 3U);
}

// 成功的快照一定返回它的条目：修剪失败（这里用自指符号链接让旧快照路径无法解析，
// 触发 ELOOP）不能把异常抛给调用方，否则新快照已落盘并登记，返回值却丢了。
TEST(WorldSnapshot, RetentionFailureStillReturnsTheNewSnapshot) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    set_retention(reg, 2);
    const fs::path dir = reg.get("sim-1").snapshot_dir;
    seed_world_db(reg.get("sim-1").db_path, "sim-1", 1);

    SnapshotEntry stuck = fake_snapshot(dir, "t1", "manual");
    fs::remove(stuck.path);
    fs::create_symlink("t1.db", stuck.path);   // 指向自己
    set_snapshots(reg, "sim-1", {fake_snapshot(dir, "t2", "manual"), stuck});

    SnapshotEntry snap;
    ASSERT_NO_THROW(snap = take_snapshot(reg, "sim-1", SnapshotOptions{}));
    EXPECT_TRUE(fs::exists(snap.path));
    ASSERT_EQ(reg.snapshots("sim-1").size(), 3U);   // 修剪没成功：暂时多一份
    EXPECT_EQ(reg.snapshots("sim-1").front().id, snap.id);
    // 列表与磁盘一致：卡住的那份仍登记着（没被摘掉），下次修剪会重试。
    EXPECT_EQ(reg.snapshots("sim-1").back().id, "t1");

    // 显式调用 apply_retention 仍然 fail loud，只是 take_snapshot 不再外抛它。
    EXPECT_EQ(code_of([&] { (void)apply_retention(reg, "sim-1"); }), "snapshot_failed");
}

// remove_within 的删除本身失败（目录不可写）时抛带 code 的 WorldStoreError，
// 不能是裸 std::filesystem::filesystem_error——HTTP 层按 code 映射。
// 注册项必须保留（文件还在盘上，不能悄悄遗弃）。
TEST(WorldSnapshotDelete, RemoveFailureIsTypedAndKeepsEntry) {
    if (::geteuid() == 0) { GTEST_SKIP() << "root 不受目录权限限制"; }
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const fs::path dir = reg.get("sim-1").snapshot_dir;
    const fs::path locked = dir / "locked-snap";
    fs::create_directories(locked);
    { std::ofstream out(locked / "inner.db"); out << "x"; }
    SnapshotEntry snap;
    snap.id = "locked";
    snap.path = locked.string();
    snap.kind = "manual";
    snap.reason = "manual";
    reg.put_snapshot("sim-1", snap);

    fs::permissions(locked, fs::perms::owner_read | fs::perms::owner_exec,
                    fs::perm_options::replace);   // 去掉写权限：unlink 内部文件会 EACCES
    const std::string code = code_of([&] { delete_snapshot(reg, "sim-1", "locked"); });
    fs::permissions(locked, fs::perms::owner_all, fs::perm_options::replace);   // 让 TempDir 能清理

    EXPECT_EQ(code, "snapshot_failed");
    ASSERT_EQ(reg.snapshots("sim-1").size(), 1U);   // 文件还在，注册项不能摘
    EXPECT_TRUE(fs::exists(locked / "inner.db"));
}

// default 世界的快照目录由 RegistryDb::open 登记但不创建，首次快照要补建。
TEST(WorldSnapshot, DefaultWorldSnapshotDirCreatedOnDemand) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "default");
    const auto world = reg.get("default");
    ASSERT_FALSE(fs::exists(world.snapshot_dir));
    seed_world_db(world.db_path, "default", 1);
    const auto snap = take_snapshot(reg, "default", SnapshotOptions{});
    EXPECT_TRUE(fs::exists(snap.path));
}

// 保留份数非法时在写任何文件之前拒绝，不能先写出快照再在修剪时才发现。
TEST(WorldSnapshot, InvalidRetentionRefusedBeforeWriting) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    seed_world_db(reg.get("sim-1").db_path, "sim-1", 1);
    set_retention(reg, 1);
    EXPECT_EQ(code_of([&] { (void)take_snapshot(reg, "sim-1", SnapshotOptions{}); }),
              "invalid_retention");
    EXPECT_TRUE(fs::is_empty(reg.get("sim-1").snapshot_dir));
    EXPECT_EQ(code_of([&] { (void)apply_retention(reg, "sim-1"); }), "invalid_retention");
}

TEST(WorldSnapshot, ExplicitKindReasonAndNoteAreRecorded) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    seed_world_db(reg.get("sim-1").db_path, "sim-1", 1);
    SnapshotOptions opts;
    opts.kind = "auto";
    opts.reason = "session";   // W4 起 auto 的 reason 只能是 session / interval（注册表校验）
    opts.note = "进入第二幕";
    const auto snap = take_snapshot(reg, "sim-1", opts);
    EXPECT_EQ(snap.kind, "auto");
    EXPECT_EQ(snap.reason, "session");
    EXPECT_EQ(snap.note, "进入第二幕");
    EXPECT_EQ(snap.created_at.size(), 20U);   // 2026-10-25T01:12:00Z
    EXPECT_EQ(snap.id.size(), 21U);           // 20261025T011200Z-a1b2
}

// 全是 auto 时：只保最新的 N 份，被删的是最旧的。
TEST(WorldRetention, AllAutoDropsOldestKeepsNewest) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    set_retention(reg, 2);
    const fs::path dir = reg.get("sim-1").snapshot_dir;
    set_snapshots(reg, "sim-1", {fake_snapshot(dir, "t4", "auto"), fake_snapshot(dir, "t3", "auto"),
                              fake_snapshot(dir, "t2", "auto"), fake_snapshot(dir, "t1", "auto")});
    EXPECT_EQ(apply_retention(reg, "sim-1"), (std::vector<std::string>{"t1", "t2"}));
    EXPECT_EQ(ids_of(reg.snapshots("sim-1")), (std::vector<std::string>{"t4", "t3"}));
}

// 唯一一份 auto 是回滚锚点：manual 再多也只删 manual，宁可只剩锚点 + 最新 manual。
TEST(WorldRetention, SoleAutoSurvivesManualFlood) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    set_retention(reg, 2);
    const fs::path dir = reg.get("sim-1").snapshot_dir;
    set_snapshots(reg, "sim-1", {fake_snapshot(dir, "t5", "manual"), fake_snapshot(dir, "t4", "manual"),
                              fake_snapshot(dir, "t3", "manual"), fake_snapshot(dir, "t2", "manual"),
                              fake_snapshot(dir, "t1", "auto")});
    EXPECT_EQ(apply_retention(reg, "sim-1"), (std::vector<std::string>{"t2", "t3", "t4"}));
    EXPECT_EQ(ids_of(reg.snapshots("sim-1")), (std::vector<std::string>{"t5", "t1"}));
    EXPECT_TRUE(fs::exists(dir / "t1.db"));
}

// 被篡改的注册项指向 snapshot_dir 之外：外部文件不能被删，而且必须 fail closed——
// 摘掉注册项会让这份文件再也无法经注册表清理，页面上的快照空间统计也随之失真。
// 字面越界的路径由开头的 validate_world 拒（registry_corrupt）；字面在 snapshot_dir 里、却是指向
// 外部的符号链接，validate_world 看不出来（快照路径只比字面），由 remove_within 解析后拒（snapshot_failed）。
TEST(WorldSnapshotDelete, RefusesPathOutsideSnapshotDir) {
    const TempDir tmp;
    const TempDir outside;
    auto reg = registry_with_world(tmp, "sim-1");
    const fs::path victim = outside.path() / "precious.db";
    { std::ofstream out(victim); out << "x"; }
    SnapshotEntry snap;
    snap.id = "evil";
    snap.path = victim.string();
    snap.kind = "manual";
    snap.reason = "manual";
    reg.put_snapshot("sim-1", snap);
    EXPECT_EQ(code_of([&] { delete_snapshot(reg, "sim-1", "evil"); }), "registry_corrupt");
    EXPECT_TRUE(fs::exists(victim));

    const fs::path link = fs::path(reg.get("sim-1").snapshot_dir) / "evil.db";
    fs::create_symlink(victim, link);
    set_snapshot_column(reg, "sim-1", "evil", "path", link.string());
    EXPECT_EQ(code_of([&] { validate_world(reg, "sim-1"); }), "<no throw>");
    EXPECT_EQ(code_of([&] { delete_snapshot(reg, "sim-1", "evil"); }), "snapshot_failed");
    EXPECT_TRUE(fs::exists(victim));
    EXPECT_TRUE(fs::is_symlink(link));
    ASSERT_EQ(reg.snapshots("sim-1").size(), 1U);
    EXPECT_EQ(reg.snapshots("sim-1").front().id, "evil");
}

TEST(WorldSnapshotDelete, UnknownWorldAndNoSnapshotList) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    EXPECT_EQ(code_of([&] { delete_snapshot(reg, "nope", "x"); }), "world_not_found");
    EXPECT_EQ(code_of([&] { delete_snapshot(reg, "sim-1", "x"); }), "snapshot_not_found");
}


// ---- Codex 审查修复：租户不变式、注册表校验、fail closed、zvec、残留清扫 ----------

TEST(WorldTenant, ForeignTenantRowIsRefused) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto world = reg.get("sim-1");
    seed_world_db(world.db_path, "sim-1", 2);
    EXPECT_EQ(code_of([&] { verify_world_tenant(world.db_path, "sim-1"); }), "<no throw>");

    // 另一个租户的一行混进来（例如库被复用、注册表被改）：拒开，并点名违规的表。
    seed_world_db(world.db_path, "intruder", 0);
    exec_sql(world.db_path,
             "INSERT INTO engrams(id,tenant_id,content_hash,source_kind,ingest_policy,"
             "ingest_mode,privacy_class,retention_mode,created_at,source_item_id) "
             "VALUES('x-1','intruder','hx','conversation','default','inline','normal',"
             "'audit_retain','2026-10-25T00:00:00Z','x-1')");
    try {
        verify_world_tenant(world.db_path, "sim-1");
        ADD_FAILURE() << "foreign tenant row was accepted";
    } catch (const WorldStoreError& err) {
        EXPECT_EQ(err.code(), "world_tenant_mismatch");
        EXPECT_NE(std::string(err.what()).find("engrams"), std::string::npos) << err.what();
    }
}

TEST(WorldTenant, MissingDbPassesAndNullTenantIsIgnored) {
    const TempDir tmp;
    // 新世界首次激活：库文件还不存在，不能拒。
    EXPECT_EQ(code_of([&] {
        verify_world_tenant((tmp.path() / "absent.db").string(), "sim-1");
    }), "<no throw>");
    // tenant_id 为 NULL 的行不属于任何租户，不算违规；没有 tenant_id 列的表不参与检查。
    const std::string db_path = (tmp.path() / "legacy.db").string();
    exec_sql_unmigrated(db_path,
             "CREATE TABLE things(id TEXT, tenant_id TEXT);"
             "INSERT INTO things VALUES('a','sim-1'),('b',NULL);"
             "CREATE TABLE plain(id TEXT); INSERT INTO plain VALUES('z');");
    EXPECT_EQ(code_of([&] { verify_world_tenant(db_path, "sim-1"); }), "<no throw>");
    EXPECT_EQ(code_of([&] { verify_world_tenant(db_path, "other"); }), "world_tenant_mismatch");
}

// ---- 注册表不变式（validate_world / validate_registry；原 RegistryValidation 对 load_registry
// 的用例）。worlds.json 的同一套规则在 test_registry_import.cpp 里对导入再测一遍。----------------

// 一个世界的注册表行被手改：validate_world 报 registry_corrupt，消息里点名 field。
void expect_world_corrupt(const RegistryDb& reg, const std::string& world, const std::string& label,
                          const std::string& field) {
    const std::string what = what_of([&] { validate_world(reg, world); });
    EXPECT_EQ(code_of([&] { validate_world(reg, world); }), "registry_corrupt") << label;
    EXPECT_NE(what.find(field), std::string::npos) << label << ": " << what;
    EXPECT_EQ(code_of([&] { validate_registry(reg); }), "registry_corrupt") << label;
}

TEST(WorldValidation, FreshRegistryAndCreatedWorldsPass) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    (void)reg.create("Run.A_1", "", {{"k", "v"}}, "{}", kCreatedAt);
    EXPECT_EQ(code_of([&] { validate_registry(reg); }), "<no throw>");
    EXPECT_EQ(code_of([&] { validate_world(reg, "sim-1"); }), "<no throw>");
    EXPECT_EQ(code_of([&] { validate_world(reg, "default"); }), "<no throw>");
    EXPECT_EQ(code_of([&] { validate_world(reg, "nope"); }), "world_not_found");
}

TEST(WorldValidation, RejectsBadNameAndDirId) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    registry_sql(reg, "UPDATE worlds SET name='Bad Name' WHERE name='sim-1'");
    expect_world_corrupt(reg, "Bad Name", "bad name", "world name 'Bad Name'");
    registry_sql(reg, "UPDATE worlds SET name='sim-1' WHERE name='Bad Name'");
    for (const char* dir_id : {"", ".", "..", "../x", "a/b"}) {
        set_world_column(reg, "sim-1", "dir_id", dir_id);
        expect_world_corrupt(reg, "sim-1", std::string("dir_id '") + dir_id + "'", "invalid dir_id");
    }
}

TEST(WorldValidation, RejectsRelativePaths) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto rec = reg.get("sim-1");
    set_world_column(reg, "sim-1", "db_path", "worlds/x/world.db");
    expect_world_corrupt(reg, "sim-1", "relative db_path", "db_path of 'sim-1' is not absolute");
    set_world_column(reg, "sim-1", "db_path", rec.db_path);
    set_world_column(reg, "sim-1", "snapshot_dir", "snapshots");
    expect_world_corrupt(reg, "sim-1", "relative snapshot_dir",
                         "snapshot_dir of 'sim-1' is not absolute");
    // default 的库路径同样必须是绝对路径（它不受 worlds/ 布局约束，但也不能是相对的）。
    set_world_column(reg, "default", "db_path", "dashboard.db");
    expect_world_corrupt(reg, "default", "relative default db_path", "db_path of 'default'");
}

TEST(WorldValidation, RejectsWorldPathsOutsideItsHome) {
    const TempDir tmp;
    const TempDir outside;
    auto reg = registry_with_world(tmp, "sim-1");
    add_world(reg, "sim-2");
    const auto rec = reg.get("sim-1");
    const fs::path home = world_dir_of(reg, "sim-1");
    for (const auto& bad : {(outside.path() / "world.db").string(),
                            (home / ".." / "other" / "world.db").string(),   // .. 绕出去
                            reg.get("sim-2").db_path}) {                     // 兄弟世界的目录
        set_world_column(reg, "sim-1", "db_path", bad);
        expect_world_corrupt(reg, "sim-1", "db_path " + bad, "db_path of 'sim-1'");
    }
    set_world_column(reg, "sim-1", "db_path", rec.db_path);
    set_world_column(reg, "sim-1", "snapshot_dir", (outside.path() / "snaps").string());
    expect_world_corrupt(reg, "sim-1", "snapshot_dir outside", "snapshot_dir of 'sim-1'");
}

// default 的库保持原地，但它的快照目录同样固定在 worlds/default/ 下：否则把它改成 "/"，
// 删除快照 / 保留策略 / 残留清扫就能删到任意位置。
TEST(WorldValidation, RejectsDefaultSnapshotDirOutsideItsHome) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    set_world_column(reg, "default", "snapshot_dir", "/");
    expect_world_corrupt(reg, "default", "default snapshot_dir /", "snapshot_dir of 'default'");
}

TEST(WorldValidation, RejectsBadSnapshotEntries) {
    const TempDir tmp;
    const TempDir outside;
    auto reg = registry_with_world(tmp, "sim-1");
    const fs::path dir = reg.get("sim-1").snapshot_dir;
    reg.put_snapshot("sim-1", fake_snapshot(dir, "s1", "manual"));
    EXPECT_EQ(code_of([&] { validate_world(reg, "sim-1"); }), "<no throw>");
    set_snapshot_column(reg, "sim-1", "s1", "kind", "Auto");
    expect_world_corrupt(reg, "sim-1", "kind Auto", "has kind 'Auto'");
    for (const auto& [kind, reason] : std::vector<std::pair<std::string, std::string>>{
             {"manual", "session"}, {"auto", "manual"}, {"auto", "scene_boundary"}, {"auto", ""}}) {
        set_snapshot_column(reg, "sim-1", "s1", "kind", kind);
        set_snapshot_column(reg, "sim-1", "s1", "reason", reason);
        expect_world_corrupt(reg, "sim-1", kind + "/" + reason, "with reason");
    }
    set_snapshot_column(reg, "sim-1", "s1", "kind", "manual");
    set_snapshot_column(reg, "sim-1", "s1", "reason", "manual");
    // 快照路径越出所属世界的 snapshot_dir（外部、别的世界的快照目录）。
    for (const auto& bad : {(outside.path() / "precious.db").string(),
                            (fs::path(reg.get("default").snapshot_dir) / "s1.db").string()}) {
        set_snapshot_column(reg, "sim-1", "s1", "path", bad);
        expect_world_corrupt(reg, "sim-1", "path " + bad, "lies outside");
    }
}

TEST(WorldValidation, RejectsLowRetentionAndOutOfRangeAutoSnapshot) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    set_retention(reg, 1);
    EXPECT_EQ(code_of([&] { validate_world(reg, "sim-1"); }), "invalid_retention");
    set_retention(reg, 5);
    // 存着的设置解析不了（只有手改 registry.db 才写得进去）是注册表损坏：registry_corrupt，原码写在消息里。
    for (const std::int64_t bad : {std::int64_t{-1}, std::int64_t{8761}}) {
        set_world_column(reg, "sim-1", "settings_json",
                         R"({"background":{"auto_snapshot":{"on_session":true,"interval_hours":)" +
                             std::to_string(bad) + "}}}");
        expect_world_corrupt(reg, "sim-1", "interval " + std::to_string(bad),
                             "settings_json of 'sim-1': invalid_auto_snapshot");
    }
    set_world_column(reg, "sim-1", "settings_json", R"({"foo":1})");
    expect_world_corrupt(reg, "sim-1", "unknown key", "settings_json of 'sim-1': invalid_settings");
}

// worlds/ 本身指向外部：注册表校验即拒（registry_corrupt）。只靠逐项比较两侧都解析
// 符号链接的话，「snapshot_dir 在 worlds/<dir_id> 之下」会在外部成立，快照清扫、保留策略、
// 删快照就以外部目录为根（Codex 第二轮 Important 2）。
TEST(WorldRegistry, ValidationRefusesWorldsDirSymlinkedOutside) {
    const TempDir tmp;
    const TempDir outside;
    auto reg = registry_with_world(tmp, "sim-1");
    const std::string dir_id = reg.get("sim-1").dir_id;
    fs::rename(tmp.path() / "worlds", outside.path() / "worlds");
    fs::create_directory_symlink(outside.path() / "worlds", tmp.path() / "worlds");
    EXPECT_EQ(code_of([&] { validate_registry(reg); }), "registry_corrupt");
    EXPECT_EQ(code_of([&] { validate_world(reg, "sim-1"); }), "registry_corrupt");
    EXPECT_TRUE(fs::exists(outside.path() / "worlds" / dir_id));   // 外部一个字节不动
}

// worlds/ 本身是指向外部的符号链接：解析后 worlds/<dir_id> 落在数据目录之外，
// 删除必须拒绝，外部一个字节都不能动，注册项保留。
TEST(WorldRegistry, DeleteRefusesWhenWorldsDirIsSymlinkOutside) {
    const TempDir tmp;
    const TempDir outside;
    auto reg = registry_with_world(tmp, "sim-1");
    const std::string dir_id = reg.get("sim-1").dir_id;
    fs::remove_all(tmp.path() / "worlds");
    fs::create_directories(outside.path() / dir_id);
    { std::ofstream out(outside.path() / dir_id / "keep.txt"); out << "x"; }
    fs::create_directory_symlink(outside.path(), tmp.path() / "worlds");
    EXPECT_EQ(code_of([&] { reg.remove("sim-1"); }), "snapshot_failed");
    EXPECT_TRUE(fs::exists(outside.path() / dir_id / "keep.txt"));
    EXPECT_EQ(reg.get("sim-1").name, "sim-1");
}

// 同一份注册表换一种路径写法打开（经指向数据目录的符号链接）：布局校验必须认它合法。
// 只做字面比较的话，注册表里写的是真实路径、数据目录却是链接路径，于是判成越界，
// 服务启动即失败（/tmp 与 /private/tmp 就是这种情形）。
TEST(WorldRegistry, ValidatesThroughSymlinkedDataDir) {
    const TempDir tmp;
    const TempDir links;
    { auto reg = registry_with_world(tmp, "sim-1"); }
    const fs::path alias = links.path() / "alias";
    fs::create_directory_symlink(tmp.path(), alias);
    auto aliased = RegistryDb::open(alias, alias / "dashboard.db", "default");
    EXPECT_EQ(code_of([&] { validate_registry(aliased); }), "<no throw>");
    // 解析符号链接之后仍越界的路径照样拒绝：只放行「同一处、换了写法」。
    set_world_column(aliased, "sim-1", "snapshot_dir", (links.path() / "elsewhere").string());
    EXPECT_EQ(code_of([&] { validate_registry(aliased); }), "registry_corrupt");
}

// 保留策略遇到越界的注册项：fail closed，外部文件不动，注册项保留。条目字面在 snapshot_dir 里
// （validate_world 放行），磁盘上却是指向外部的符号链接：由 remove_within 解析后拒。
TEST(WorldRetention, OutOfBoundsVictimFailsClosed) {
    const TempDir tmp;
    const TempDir outside;
    auto reg = registry_with_world(tmp, "sim-1");
    set_retention(reg, 2);
    const fs::path dir = reg.get("sim-1").snapshot_dir;
    const fs::path victim = outside.path() / "precious.db";
    { std::ofstream out(victim); out << "x"; }
    SnapshotEntry evil;
    evil.id = "t1";
    evil.path = (dir / "t1.db").string();
    fs::create_symlink(victim, evil.path);
    evil.kind = "manual";
    evil.reason = "manual";
    set_snapshots(reg, "sim-1", {fake_snapshot(dir, "t3", "manual"),
                              fake_snapshot(dir, "t2", "manual"), evil});
    EXPECT_EQ(code_of([&] { (void)apply_retention(reg, "sim-1"); }), "snapshot_failed");
    EXPECT_TRUE(fs::exists(victim));
    EXPECT_EQ(ids_of(reg.snapshots("sim-1")), (std::vector<std::string>{"t3", "t2", "t1"}));
}

// 注册项还在、文件已经没了（保存失败后的补偿、手工删除）：仍能删掉注册项。
TEST(WorldSnapshotDelete, EntryWithMissingFileCanBeDeleted) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    seed_world_db(reg.get("sim-1").db_path, "sim-1", 1);
    const auto snap = take_snapshot(reg, "sim-1", SnapshotOptions{});
    fs::remove(snap.path);
    delete_snapshot(reg, "sim-1", snap.id);
    EXPECT_TRUE(reg.snapshots("sim-1").empty());
}

// 向量不在库里的后端（zvec）：在任何文件 I/O 之前拒绝，不留文件，不登记。
TEST(WorldSnapshot, RefusesNonSqliteVectorBackendBeforeWriting) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    seed_world_db(reg.get("sim-1").db_path, "sim-1", 1);
    for (const char* backend : {"zvec", "", "SQLITE"}) {
        SnapshotOptions opts;
        opts.vector_backend = backend;
        EXPECT_EQ(code_of([&] { (void)take_snapshot(reg, "sim-1", opts); }),
                  "snapshot_unsupported_backend") << "backend='" << backend << "'";
    }
    EXPECT_TRUE(fs::is_empty(reg.get("sim-1").snapshot_dir));
    EXPECT_TRUE(reg.snapshots("sim-1").empty());
    // 默认就是 sqlite：照常拍。
    EXPECT_NO_THROW((void)take_snapshot(reg, "sim-1", SnapshotOptions{}));
}

// 上次崩溃的残留：VACUUM 写到一半的 .partial，以及改名之后、存注册表之前崩溃留下的
// 未登记 .db。下一次快照先清掉它们；登记过的快照和不像快照 id 的文件一概不碰。
TEST(WorldSnapshot, SweepsCrashLeftoversBeforeSnapshotting) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    seed_world_db(reg.get("sim-1").db_path, "sim-1", 1);
    const fs::path dir = reg.get("sim-1").snapshot_dir;
    const auto kept = take_snapshot(reg, "sim-1", SnapshotOptions{});
    const fs::path partial = dir / "20260101T000000Z-dead.db.partial";
    const fs::path orphan = dir / "20260101T000000Z-beef.db";
    const fs::path foreign_db = dir / "keep.db";
    const fs::path note = dir / "notes.txt";
    for (const auto& path : {partial, orphan, foreign_db, note}) {
        std::ofstream out(path);
        out << "x";
    }
    const auto snap = take_snapshot(reg, "sim-1", SnapshotOptions{});
    EXPECT_FALSE(fs::exists(partial));
    EXPECT_FALSE(fs::exists(orphan));
    EXPECT_TRUE(fs::exists(kept.path));
    EXPECT_TRUE(fs::exists(snap.path));
    EXPECT_TRUE(fs::exists(foreign_db));
    EXPECT_TRUE(fs::exists(note));
}

// 快照文件已改好名、登记进 registry.db 却失败（这里用触发器让 INSERT 失败）：take_snapshot 自己删掉
// 那个文件、抛 snapshot_failed，注册表不变，快照目录里不留没有登记的文件。原来这一步由调用方存注册表
// 失败后调 discard_snapshot_file 补偿。
TEST(WorldSnapshot, RegistrationFailureRemovesTheNewFile) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    seed_world_db(reg.get("sim-1").db_path, "sim-1", 1);
    const auto kept = take_snapshot(reg, "sim-1", SnapshotOptions{});
    const fs::path dir = reg.get("sim-1").snapshot_dir;
    registry_sql(reg, "CREATE TRIGGER no_insert BEFORE INSERT ON snapshots "
                      "BEGIN SELECT RAISE(ABORT, 'injected insert failure'); END");
    const std::string what = what_of([&] { (void)take_snapshot(reg, "sim-1", SnapshotOptions{}); });
    EXPECT_NE(what.find("snapshot_failed: cannot record snapshot"), std::string::npos) << what;
    EXPECT_NE(what.find("injected insert failure"), std::string::npos) << what;
    EXPECT_EQ(ids_of(reg.snapshots("sim-1")), (std::vector<std::string>{kept.id}));
    std::vector<std::string> files;
    for (const auto& item : fs::directory_iterator(dir)) {
        files.push_back(item.path().filename().string());
    }
    EXPECT_EQ(files, (std::vector<std::string>{fs::path(kept.path).filename().string()}));
    registry_sql(reg, "DROP TRIGGER no_insert");
    EXPECT_EQ(code_of([&] { (void)take_snapshot(reg, "sim-1", SnapshotOptions{}); }), "<no throw>");
    EXPECT_EQ(reg.snapshots("sim-1").size(), 2U);
}

// 文件已删、摘注册项失败（触发器让 DELETE 失败）：抛 snapshot_failed（不是裸 SqliteError），注册项
// 还在；修好之后再修剪，对已经不在的文件照常删、再摘一次。
TEST(WorldRetention, DropFailureAfterTheFileIsGoneIsRetriedNextTime) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    set_retention(reg, 2);
    const fs::path dir = reg.get("sim-1").snapshot_dir;
    set_snapshots(reg, "sim-1", {fake_snapshot(dir, "t3", "manual"),
                                 fake_snapshot(dir, "t2", "manual"),
                                 fake_snapshot(dir, "t1", "manual")});
    registry_sql(reg, "CREATE TRIGGER no_delete BEFORE DELETE ON snapshots "
                      "BEGIN SELECT RAISE(ABORT, 'injected delete failure'); END");
    const std::string what = what_of([&] { (void)apply_retention(reg, "sim-1"); });
    EXPECT_NE(what.find("snapshot_failed:"), std::string::npos) << what;
    EXPECT_NE(what.find("could not be dropped"), std::string::npos) << what;
    EXPECT_FALSE(fs::exists(dir / "t1.db"));
    EXPECT_EQ(ids_of(reg.snapshots("sim-1")), (std::vector<std::string>{"t3", "t2", "t1"}));
    registry_sql(reg, "DROP TRIGGER no_delete");
    EXPECT_EQ(apply_retention(reg, "sim-1"), (std::vector<std::string>{"t1"}));
    EXPECT_EQ(ids_of(reg.snapshots("sim-1")), (std::vector<std::string>{"t3", "t2"}));
}

// ---- W3：回滚（语义 A，丢掉后续）与归档 -------------------------------------
// 设计：spec §5.1 / §11；计划 docs/superpowers/plans/2026-10-26-world-w3-rollback.md Task 1。

fs::path home_of(const WorldRecord& world) { return fs::path(world.snapshot_dir).parent_path(); }
fs::path archive_dir_of(const WorldRecord& world) { return home_of(world) / "archive"; }

bool archive_is_empty(const WorldRecord& world) {
    const fs::path dir = archive_dir_of(world);
    return !fs::exists(dir) || fs::is_empty(dir);
}

// 库所在目录里回滚留下的 staging 文件（.<库文件名>.rollback-<hex>.partial 及其 -wal/-shm）。
std::vector<std::string> rollback_partials(const WorldRecord& world) {
    std::vector<std::string> out;
    for (const auto& item : fs::directory_iterator(fs::path(world.db_path).parent_path())) {
        const std::string name = item.path().filename().string();
        if (name.find(".rollback-") != std::string::npos) { out.push_back(name); }
    }
    return out;
}

// 拍一份 rows 行的快照，再追加 extra 行，返回快照。
SnapshotEntry snapshot_then_write(RegistryDb& reg, const std::string& world, int rows, int extra) {
    const auto entry = reg.get(world);
    seed_world_db(entry.db_path, world, rows);
    auto snap = take_snapshot(reg, world, SnapshotOptions{});
    append_engrams(entry.db_path, world, rows, extra);
    return snap;
}

std::int64_t engram_count(const std::string& db_path) {
    return count_rows(db_path, "SELECT COUNT(*) FROM engrams");
}

// 新开一条读写连接（与引擎同样按路径打开、不带 URI 参数），返回 engrams 行数和
// PRAGMA integrity_check 是否为 "ok"。
std::pair<std::int64_t, bool> open_fresh_and_check(const std::string& db_path) {
    sqlite3* raw = nullptr;
    EXPECT_EQ(sqlite3_open_v2(db_path.c_str(), &raw, SQLITE_OPEN_READWRITE, nullptr), SQLITE_OK);
    const persistence::SqliteHandle handle(raw);
    persistence::Stmt rows(raw, "SELECT COUNT(*) FROM engrams");
    const std::int64_t count = rows.step_row() ? sqlite3_column_int64(rows.raw(), 0) : -1;
    persistence::Stmt check(raw, "SELECT COUNT(*) FROM pragma_integrity_check "
                                 "WHERE integrity_check='ok'");
    const bool intact = check.step_row() && sqlite3_column_int64(check.raw(), 0) == 1;
    return {count, intact};
}

// 让快照「打得开、quick_check 通过、integrity_check 不通过」：改写一个索引的定义，使索引内容
// 与表对不上（quick_check 不比对索引内容）。只有 integrity_check 拦得住它：把校验换成
// quick_check，或者干脆不校验，用例都会失败。
void corrupt_index_definition(const std::string& db_path) {
    sqlite3* raw = nullptr;
    ASSERT_EQ(sqlite3_open(db_path.c_str(), &raw), SQLITE_OK);
    const persistence::SqliteHandle handle(raw);
    sqlite3_db_config(raw, SQLITE_DBCONFIG_DEFENSIVE, 0, nullptr);
    run_sql(raw,
            "CREATE TABLE side(aaa, bbb); CREATE INDEX side_aaa ON side(aaa);"
            "INSERT INTO side VALUES(1,10),(2,20),(3,30);"
            "PRAGMA writable_schema=ON;"
            "UPDATE sqlite_master SET sql='CREATE INDEX side_aaa ON side(bbb)' "
            "WHERE name='side_aaa';");
}

// 把目录设成只读，析构时恢复原权限：断言失败提前返回也会恢复，否则 TempDir 删不掉它。
class ReadOnlyDir {
public:
    explicit ReadOnlyDir(fs::path dir)
        : dir_(std::move(dir)), saved_(fs::status(dir_).permissions()) {
        fs::permissions(dir_, fs::perms::owner_read | fs::perms::owner_exec,
                        fs::perm_options::replace);
    }
    ~ReadOnlyDir() {
        std::error_code err;
        fs::permissions(dir_, saved_, fs::perm_options::replace, err);
    }
    ReadOnlyDir(const ReadOnlyDir&) = delete;
    ReadOnlyDir& operator=(const ReadOnlyDir&) = delete;
    ReadOnlyDir(ReadOnlyDir&&) = delete;
    ReadOnlyDir& operator=(ReadOnlyDir&&) = delete;
private:
    fs::path dir_;
    fs::perms saved_;
};

TEST(WorldRollback, RestoresSnapshotContentAndArchivesThePreviousDb) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto world = reg.get("sim-1");
    const auto snap = snapshot_then_write(reg, "sim-1", 2, 3);
    ASSERT_EQ(engram_count(world.db_path), 5);
    // 实测表：只读读者打开过之后会留下空的 -wal 和 32 KB 的 -shm，回滚要能处理并清掉。
    ASSERT_TRUE(fs::exists(world.db_path + "-shm"));
    ASSERT_EQ(fs::file_size(world.db_path + "-wal"), 0U);
    // 现役库收紧过权限：恢复出来的库沿用它，不随快照文件放宽。
    const auto tight = fs::perms::owner_read | fs::perms::owner_write;
    fs::permissions(world.db_path, tight, fs::perm_options::replace);
    { std::ofstream out(world.db_path + "-journal"); }   // 空的 -journal 同样属于旧库

    const auto result = rollback_world(reg, "sim-1", snap.id, RollbackOptions{});
    EXPECT_EQ(fs::status(world.db_path).permissions() & fs::perms::all, tight);
    EXPECT_FALSE(fs::exists(world.db_path + "-wal"));
    EXPECT_FALSE(fs::exists(world.db_path + "-shm"));
    EXPECT_FALSE(fs::exists(world.db_path + "-journal"));
    EXPECT_EQ(engram_count(world.db_path), 2);
    EXPECT_EQ(result.restored_bytes, static_cast<std::int64_t>(fs::file_size(snap.path)));

    ASSERT_FALSE(result.archive_path.empty());
    const fs::path archived = result.archive_path;
    EXPECT_TRUE(fs::is_regular_file(archived));
    EXPECT_EQ(archived.parent_path(), archive_dir_of(world));
    EXPECT_EQ(archived.filename().string().rfind("world.db.pre-rollback-", 0), 0U) << archived;
    EXPECT_EQ(engram_count(result.archive_path), 5);   // 被替换的库原样保留
    // 归档是旧 inode 唯一剩下的名字（硬链接 + 原子替换之后），新库是另一个 inode。
    EXPECT_EQ(fs::hard_link_count(archived), 1U);
    EXPECT_EQ(fs::hard_link_count(world.db_path), 1U);

    EXPECT_TRUE(fs::exists(snap.path));   // 快照是复制出来的，不是挪走的
    EXPECT_TRUE(rollback_partials(world).empty());
    // 恢复出来的库能被引擎照常打开并写入（journal_mode 自动回到 wal）。
    append_engrams(world.db_path, "sim-1", 10, 1);
    EXPECT_EQ(engram_count(world.db_path), 3);
}

TEST(WorldRollback, RefusesCorruptSnapshotAndLeavesTargetUntouched) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto world = reg.get("sim-1");
    const auto snap = snapshot_then_write(reg, "sim-1", 2, 3);
    corrupt_index_definition(snap.path);
    ASSERT_EQ(count_rows(snap.path, "SELECT COUNT(*) FROM pragma_quick_check "
                                    "WHERE quick_check='ok'"), 1)
        << "要的是只有 integrity_check 拦得住的损坏";
    ASSERT_EQ(count_rows(snap.path, "SELECT COUNT(*) FROM pragma_integrity_check "
                                    "WHERE integrity_check='ok'"), 0);
    const std::string before = slurp(world.db_path);

    EXPECT_EQ(code_of([&] { (void)rollback_world(reg, "sim-1", snap.id, RollbackOptions{}); }),
              "snapshot_corrupt");
    EXPECT_EQ(slurp(world.db_path), before);
    EXPECT_EQ(engram_count(world.db_path), 5);
    EXPECT_TRUE(archive_is_empty(world));
    EXPECT_TRUE(rollback_partials(world).empty());
}

TEST(WorldRollback, RefusesSnapshotThatIsNotADatabase) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto world = reg.get("sim-1");
    const auto snap = snapshot_then_write(reg, "sim-1", 2, 1);
    { std::ofstream out(snap.path, std::ios::trunc); out << "this is not a sqlite database, nope"; }
    const std::string before = slurp(world.db_path);
    EXPECT_EQ(code_of([&] { (void)rollback_world(reg, "sim-1", snap.id, RollbackOptions{}); }),
              "snapshot_corrupt");
    EXPECT_EQ(slurp(world.db_path), before);
    EXPECT_TRUE(archive_is_empty(world));
    EXPECT_TRUE(rollback_partials(world).empty());
}

TEST(WorldRollback, RefusesMissingSnapshotFile) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto world = reg.get("sim-1");
    const auto snap = snapshot_then_write(reg, "sim-1", 2, 1);
    fs::remove(snap.path);
    const std::string before = slurp(world.db_path);
    EXPECT_EQ(code_of([&] { (void)rollback_world(reg, "sim-1", snap.id, RollbackOptions{}); }),
              "snapshot_corrupt");
    EXPECT_EQ(slurp(world.db_path), before);
    EXPECT_TRUE(archive_is_empty(world));
    EXPECT_TRUE(rollback_partials(world).empty());
}

// 快照文件被换成指向 snapshot_dir 之外的符号链接：解析后越界，snapshot_failed，不读它。
TEST(WorldRollback, RefusesSnapshotPathEscapingSnapshotDir) {
    const TempDir tmp;
    const TempDir outside;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto world = reg.get("sim-1");
    const auto snap = snapshot_then_write(reg, "sim-1", 2, 1);
    const fs::path foreign = outside.path() / "foreign.db";
    fs::rename(snap.path, foreign);   // 内容完好，只是挪到了外面
    fs::create_symlink(foreign, snap.path);
    const std::string before = slurp(world.db_path);
    EXPECT_EQ(code_of([&] { (void)rollback_world(reg, "sim-1", snap.id, RollbackOptions{}); }),
              "snapshot_failed");
    EXPECT_EQ(slurp(world.db_path), before);
    EXPECT_TRUE(archive_is_empty(world));
    EXPECT_TRUE(rollback_partials(world).empty());
}

TEST(WorldRollback, RefusesWhenDbDirectoryIsReadOnly) {
    if (::geteuid() == 0) { GTEST_SKIP() << "root 不受目录权限限制"; }
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto world = reg.get("sim-1");
    const auto snap = snapshot_then_write(reg, "sim-1", 2, 3);
    const std::string before = slurp(world.db_path);
    std::string code;
    {
        const ReadOnlyDir locked(fs::path(world.db_path).parent_path());
        code = code_of([&] { (void)rollback_world(reg, "sim-1", snap.id, RollbackOptions{}); });
    }
    EXPECT_EQ(code, "snapshot_failed");
    EXPECT_EQ(slurp(world.db_path), before);
    EXPECT_EQ(engram_count(world.db_path), 5);
    EXPECT_TRUE(archive_is_empty(world));
    EXPECT_TRUE(rollback_partials(world).empty());
}

TEST(WorldRollback, RefusesForeignTenantSnapshot) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto world = reg.get("sim-1");
    const auto snap = snapshot_then_write(reg, "sim-1", 2, 1);
    exec_sql_unmigrated(snap.path,
             "INSERT INTO engrams(id,tenant_id,content_hash,source_kind,ingest_policy,"
             "ingest_mode,privacy_class,retention_mode,created_at,source_item_id) "
             "VALUES('x-1','intruder','hx','conversation','default','inline','normal',"
             "'audit_retain','2026-10-25T00:00:00Z','x-1')");
    const std::string before = slurp(world.db_path);
    EXPECT_EQ(code_of([&] { (void)rollback_world(reg, "sim-1", snap.id, RollbackOptions{}); }),
              "world_tenant_mismatch");
    EXPECT_EQ(slurp(world.db_path), before);
    EXPECT_TRUE(archive_is_empty(world));
    EXPECT_TRUE(rollback_partials(world).empty());
}

TEST(WorldRollback, RefusesInsufficientDisk) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto world = reg.get("sim-1");
    const auto snap = snapshot_then_write(reg, "sim-1", 2, 1);
    const std::string before = slurp(world.db_path);
    const auto snap_bytes = static_cast<std::int64_t>(fs::file_size(snap.path));

    RollbackOptions starved;
    fs::path asked;
    starved.free_space = [&asked](const fs::path& dir) -> std::int64_t { asked = dir; return 1; };
    EXPECT_EQ(code_of([&] { (void)rollback_world(reg, "sim-1", snap.id, starved); }),
              "insufficient_disk");
    EXPECT_EQ(asked, fs::path(world.db_path).parent_path());   // 问的是库所在目录
    EXPECT_EQ(slurp(world.db_path), before);
    EXPECT_TRUE(archive_is_empty(world));
    EXPECT_TRUE(rollback_partials(world).empty());

    RollbackOptions short_by_one;   // 少 1 字节：拒
    short_by_one.free_space = [snap_bytes](const fs::path&) { return 2 * snap_bytes - 1; };
    EXPECT_EQ(code_of([&] { (void)rollback_world(reg, "sim-1", snap.id, short_by_one); }),
              "insufficient_disk");
    RollbackOptions exact;          // 恰好 2 倍：放行
    exact.free_space = [snap_bytes](const fs::path&) { return 2 * snap_bytes; };
    EXPECT_EQ(code_of([&] { (void)rollback_world(reg, "sim-1", snap.id, exact); }), "<no throw>");
    EXPECT_EQ(engram_count(world.db_path), 2);
}

TEST(WorldRollback, RefusesNonEmptyWal) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto world = reg.get("sim-1");
    const auto snap = snapshot_then_write(reg, "sim-1", 2, 1);
    { std::ofstream out(world.db_path + "-wal", std::ios::trunc); out << "pending frames"; }
    const std::string before = slurp(world.db_path);
    EXPECT_EQ(code_of([&] { (void)rollback_world(reg, "sim-1", snap.id, RollbackOptions{}); }),
              "snapshot_failed");
    EXPECT_EQ(slurp(world.db_path), before);
    EXPECT_EQ(slurp(world.db_path + "-wal"), "pending frames");
    EXPECT_TRUE(archive_is_empty(world));
    EXPECT_TRUE(rollback_partials(world).empty());

    // 库不在、旁边却有非空 -wal：同样拒绝，否则 SQLite 打开恢复出来的库时会重放这份 WAL。
    fs::remove(world.db_path);
    EXPECT_EQ(code_of([&] { (void)rollback_world(reg, "sim-1", snap.id, RollbackOptions{}); }),
              "snapshot_failed");
    EXPECT_FALSE(fs::exists(world.db_path));
    EXPECT_TRUE(rollback_partials(world).empty());
}

// 库路径是符号链接：拒绝。对符号链接建硬链接得到什么因平台而异（macOS 的 link() 跟随链接、
// 链到目标文件；Linux 链的是符号链接本身），两种结果都不是要的归档：前者让链接目标多出一个
// 名字，后者 list / purge 认不出（跳过符号链接）。链接指向的真实库也不该被悄悄换掉。
TEST(WorldRollback, RefusesWhenDbPathIsASymlink) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto world = reg.get("sim-1");
    const auto snap = snapshot_then_write(reg, "sim-1", 2, 1);
    const fs::path real_db = home_of(world) / "real.db";
    for (const char* suffix : {"-wal", "-shm"}) { fs::remove(world.db_path + suffix); }
    fs::rename(world.db_path, real_db);
    fs::create_symlink(real_db, world.db_path);
    const std::string before = slurp(real_db);
    EXPECT_EQ(code_of([&] { (void)rollback_world(reg, "sim-1", snap.id, RollbackOptions{}); }),
              "snapshot_failed");
    EXPECT_TRUE(fs::is_symlink(world.db_path));
    EXPECT_EQ(slurp(real_db), before);
    EXPECT_TRUE(archive_is_empty(world));
    EXPECT_TRUE(rollback_partials(world).empty());
}

TEST(WorldRollback, UnknownWorldAndSnapshot) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    EXPECT_EQ(code_of([&] { (void)rollback_world(reg, "nope", "x", RollbackOptions{}); }),
              "world_not_found");
    EXPECT_EQ(code_of([&] { (void)rollback_world(reg, "sim-1", "x", RollbackOptions{}); }),
              "snapshot_not_found");
    const auto snap = snapshot_then_write(reg, "sim-1", 1, 0);
    EXPECT_EQ(code_of([&] { (void)rollback_world(reg, "sim-1", "x", RollbackOptions{}); }),
              "snapshot_not_found");
    // 别的世界的快照 id 不能拿来回滚这个世界。
    add_world(reg, "sim-2");
    EXPECT_EQ(code_of([&] { (void)rollback_world(reg, "sim-2", snap.id, RollbackOptions{}); }),
              "snapshot_not_found");
}

TEST(WorldRollback, RestoresIntoAWorldWhoseDbDoesNotExistYet) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto world = reg.get("sim-1");
    const auto snap = snapshot_then_write(reg, "sim-1", 2, 0);
    for (const char* suffix : {"", "-wal", "-shm"}) { fs::remove(world.db_path + suffix); }
    ASSERT_FALSE(fs::exists(world.db_path));

    const auto result = rollback_world(reg, "sim-1", snap.id, RollbackOptions{});
    EXPECT_TRUE(result.archive_path.empty());
    EXPECT_FALSE(fs::exists(archive_dir_of(world)));
    EXPECT_EQ(engram_count(world.db_path), 2);
    EXPECT_TRUE(rollback_partials(world).empty());
}

TEST(WorldRollback, RollbackDoesNotChangeTheRegistry) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto snap = snapshot_then_write(reg, "sim-1", 2, 3);
    const std::string before = registry_dump(reg);

    (void)rollback_world(reg, "sim-1", snap.id, RollbackOptions{});
    EXPECT_EQ(engram_count(reg.get("sim-1").db_path), 2);   // 回滚确实发生了
    EXPECT_EQ(registry_dump(reg), before);
}

// 归档名建好之后、原子替换失败：库路径上一直是完整的旧库（硬链接，不是改名挪走），
// 失败后只需摘掉归档名。两种注入：钩子直接抛异常；钩子删掉 partial，让真实的 fs::rename 失败。
TEST(WorldRollback, DbStaysInPlaceWhenReplaceFails) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto world = reg.get("sim-1");
    const auto snap = snapshot_then_write(reg, "sim-1", 2, 3);
    const std::string before = slurp(world.db_path);

    std::vector<std::function<void(const fs::path&)>> faults{
        [](const fs::path&) { throw std::runtime_error("injected failure before replace"); },
        [](const fs::path& staged) { fs::remove(staged); },
    };
    const std::string wal = world.db_path + "-wal";
    const std::string shm = world.db_path + "-shm";
    for (std::size_t idx = 0; idx < faults.size(); ++idx) {
        // 只读读者读一次，留下 -shm：旧库带着它进回滚（上一轮的读写连接关闭时删掉了旁车）。
        ASSERT_EQ(engram_count(world.db_path), 5) << "fault " << idx;
        ASSERT_TRUE(fs::exists(shm)) << "fault " << idx;
        RollbackOptions opts;
        bool linked = false;
        bool sidecars_gone = false;
        std::int64_t rows_at_hook = -1;
        opts.before_replace = [&](const fs::path& staged) {
            // 钩子触发时归档名已经建好（否则测不到「摘掉归档名」），库仍在原处且是旧内容。
            linked = !archive_is_empty(world) && fs::hard_link_count(world.db_path) == 2U;
            // 旁车在替换之前已删掉（C2：顺序不变）。先看这个，再读库：读库会重建它们。
            sidecars_gone = !fs::exists(wal) && !fs::exists(shm);
            rows_at_hook = fs::exists(world.db_path) ? engram_count(world.db_path) : -1;
            faults.at(idx)(staged);
        };
        EXPECT_EQ(code_of([&] { (void)rollback_world(reg, "sim-1", snap.id, opts); }),
                  "snapshot_failed") << "fault " << idx;
        EXPECT_TRUE(linked) << "fault " << idx;
        EXPECT_TRUE(sidecars_gone) << "fault " << idx;
        EXPECT_EQ(rows_at_hook, 5) << "fault " << idx;
        EXPECT_EQ(slurp(world.db_path), before) << "fault " << idx;
        EXPECT_EQ(fs::hard_link_count(world.db_path), 1U) << "fault " << idx;
        EXPECT_TRUE(archive_is_empty(world)) << "fault " << idx;
        EXPECT_TRUE(rollback_partials(world).empty()) << "fault " << idx;
        // 旧库没有了原来的旁车也照常可用：新开一条读写连接，行数与 integrity_check 都对。
        const auto [rows, intact] = open_fresh_and_check(world.db_path);
        EXPECT_EQ(rows, 5) << "fault " << idx;
        EXPECT_TRUE(intact) << "fault " << idx;
    }
    EXPECT_EQ(engram_count(world.db_path), 5);
}

// 替换失败、归档名也摘不掉（archive/ 在钩子里被设成只读）：库仍完好；错误信息写明那个归档名
// 是现役库的第二个名字、不能用写连接打开。
TEST(WorldRollback, ReportsArchiveNameThatCouldNotBeRemoved) {
    if (::geteuid() == 0) { GTEST_SKIP() << "root 不受目录权限限制"; }
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto world = reg.get("sim-1");
    const auto snap = snapshot_then_write(reg, "sim-1", 2, 3);
    const std::string before = slurp(world.db_path);

    std::optional<ReadOnlyDir> locked;
    RollbackOptions opts;
    opts.before_replace = [&](const fs::path&) {
        locked.emplace(archive_dir_of(world));
        throw std::runtime_error("injected failure before replace");
    };
    std::string message;
    try {
        (void)rollback_world(reg, "sim-1", snap.id, opts);
        ADD_FAILURE() << "rollback did not fail";
    } catch (const WorldStoreError& err) {
        EXPECT_EQ(err.code(), "snapshot_failed");
        message = err.what();
    }
    locked.reset();
    EXPECT_NE(message.find("second name of the live database"), std::string::npos) << message;
    EXPECT_EQ(slurp(world.db_path), before);
    EXPECT_EQ(fs::hard_link_count(world.db_path), 2U);   // 留下的第二个名字
    EXPECT_TRUE(rollback_partials(world).empty());
    // 它不是归档：list 不列、purge 不删（A2）；下一次回滚开头摘掉它，现役库不受影响。
    EXPECT_TRUE(list_rollback_archives(reg, "sim-1").empty());
    EXPECT_EQ(purge_rollback_archives(reg, "sim-1"), 0U);
    (void)rollback_world(reg, "sim-1", snap.id, RollbackOptions{});
    EXPECT_EQ(engram_count(world.db_path), 2);
    EXPECT_EQ(fs::hard_link_count(world.db_path), 1U);
    EXPECT_EQ(list_rollback_archives(reg, "sim-1").size(), 1U);
}

// 校验的是装上去的那份字节：快照文件在「是否存在」检查之后、复制之前被换掉（这里借第 4 步
// 的 free_space 回调换，它恰好落在两者之间），换进来的坏内容也必须被拦下，目标不动。
// 只校验快照文件本身的实现在这里会把坏内容装上去。
TEST(WorldRollback, VerifiesTheCopyNotTheSnapshotFile) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto world = reg.get("sim-1");
    const auto snap = snapshot_then_write(reg, "sim-1", 2, 3);
    const fs::path corrupt = tmp.path() / "corrupt.db";
    fs::copy_file(snap.path, corrupt);
    corrupt_index_definition(corrupt.string());
    const fs::path foreign = tmp.path() / "foreign.db";
    fs::copy_file(snap.path, foreign);
    exec_sql_unmigrated(foreign.string(),
             "INSERT INTO engrams(id,tenant_id,content_hash,source_kind,ingest_policy,"
             "ingest_mode,privacy_class,retention_mode,created_at,source_item_id) "
             "VALUES('x-1','intruder','hx','conversation','default','inline','normal',"
             "'audit_retain','2026-10-25T00:00:00Z','x-1')");
    const fs::path pristine = tmp.path() / "pristine.db";
    fs::copy_file(snap.path, pristine);
    const std::string before = slurp(world.db_path);

    const std::vector<std::pair<fs::path, std::string>> swaps{
        {corrupt, "snapshot_corrupt"}, {foreign, "world_tenant_mismatch"}};
    for (const auto& [replacement, expected] : swaps) {
        fs::copy_file(pristine, snap.path, fs::copy_options::overwrite_existing);
        bool swapped = false;
        RollbackOptions opts;
        opts.free_space = [&](const fs::path&) -> std::int64_t {
            fs::copy_file(replacement, snap.path, fs::copy_options::overwrite_existing);
            swapped = true;
            return std::numeric_limits<std::int64_t>::max();
        };
        EXPECT_EQ(code_of([&] { (void)rollback_world(reg, "sim-1", snap.id, opts); }), expected)
            << replacement;
        EXPECT_TRUE(swapped) << replacement;
        EXPECT_EQ(slurp(world.db_path), before) << replacement;
        EXPECT_TRUE(archive_is_empty(world)) << replacement;
        EXPECT_TRUE(rollback_partials(world).empty()) << replacement;
    }
}

// 上次回滚崩溃留下的 partial：下一次回滚先清掉。只认名字严格符合
// .<库文件名>.rollback-<16 位小写 hex>.partial 的普通文件，长得像的和符号链接都不碰。
TEST(WorldRollback, SweepsStaleRollbackPartials) {
    const TempDir tmp;
    const TempDir outside;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto world = reg.get("sim-1");
    const auto snap = snapshot_then_write(reg, "sim-1", 2, 1);
    const fs::path dir = fs::path(world.db_path).parent_path();
    const fs::path stale = dir / ".world.db.rollback-0123456789abcdef.partial";
    const std::vector<fs::path> keep{
        dir / ".world.db.rollback-0123.partial",               // hex 太短
        // 非 hex。不用大写变体：APFS 默认不分大小写，它会和 stale 是同一个文件。
        dir / ".world.db.rollback-0123456789abcdeg.partial",
        dir / ".other.db.rollback-0123456789abcdef.partial",   // 别的库的
        dir / ".world.db.rollback-0123456789abcdef.partial.keep",
    };
    for (const auto& path : keep) { std::ofstream out(path); out << "x"; }
    { std::ofstream out(stale); out << "x"; }
    const fs::path precious = outside.path() / "precious";
    { std::ofstream out(precious); out << "x"; }
    // 名字合规，但是符号链接：不跟随、不删。
    const fs::path link = dir / ".world.db.rollback-fedcba9876543210.partial";
    fs::create_symlink(precious, link);

    (void)rollback_world(reg, "sim-1", snap.id, RollbackOptions{});
    EXPECT_FALSE(fs::exists(stale));
    for (const auto& path : keep) { EXPECT_TRUE(fs::exists(path)) << path; }
    EXPECT_TRUE(fs::is_symlink(link));
    EXPECT_TRUE(fs::exists(precious));
    EXPECT_EQ(engram_count(world.db_path), 2);
}

// 热日志（-journal 非空或不是普通文件）同 -wal：SQLite 首次打开恢复出来的库时会把旧库的页
// 回滚进去，写坏新库。库不存在时同样检查。
TEST(WorldRollback, RefusesNonEmptyJournal) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto world = reg.get("sim-1");
    const auto snap = snapshot_then_write(reg, "sim-1", 2, 1);
    { std::ofstream out(world.db_path + "-journal", std::ios::trunc); out << "hot journal"; }
    const std::string before = slurp(world.db_path);
    EXPECT_EQ(code_of([&] { (void)rollback_world(reg, "sim-1", snap.id, RollbackOptions{}); }),
              "snapshot_failed");
    EXPECT_EQ(slurp(world.db_path), before);
    EXPECT_EQ(slurp(world.db_path + "-journal"), "hot journal");
    EXPECT_TRUE(archive_is_empty(world));
    EXPECT_TRUE(rollback_partials(world).empty());

    for (const char* suffix : {"", "-wal", "-shm"}) { fs::remove(world.db_path + suffix); }
    EXPECT_EQ(code_of([&] { (void)rollback_world(reg, "sim-1", snap.id, RollbackOptions{}); }),
              "snapshot_failed");
    EXPECT_FALSE(fs::exists(world.db_path));
    EXPECT_TRUE(rollback_partials(world).empty());
}

// 向量不在库里的后端（zvec）：在任何文件 I/O 之前拒绝（spec §12），与 take_snapshot 一致。
TEST(WorldRollback, RefusesNonSqliteVectorBackend) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto world = reg.get("sim-1");
    const auto snap = snapshot_then_write(reg, "sim-1", 2, 1);
    const std::string before = slurp(world.db_path);
    for (const char* backend : {"zvec", "", "SQLITE"}) {
        RollbackOptions opts;
        opts.vector_backend = backend;
        EXPECT_EQ(code_of([&] { (void)rollback_world(reg, "sim-1", snap.id, opts); }),
                  "snapshot_unsupported_backend") << "backend='" << backend << "'";
    }
    EXPECT_EQ(slurp(world.db_path), before);
    EXPECT_TRUE(archive_is_empty(world));
    EXPECT_TRUE(rollback_partials(world).empty());
}

TEST(WorldArchives, ListsAndPurgesArchives) {
    const TempDir tmp;
    const TempDir outside;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto world = reg.get("sim-1");
    EXPECT_TRUE(list_rollback_archives(reg, "sim-1").empty());   // archive/ 还不存在
    EXPECT_EQ(purge_rollback_archives(reg, "sim-1"), 0U);

    const auto snap = snapshot_then_write(reg, "sim-1", 2, 1);
    const auto first = rollback_world(reg, "sim-1", snap.id, RollbackOptions{});
    append_engrams(world.db_path, "sim-1", 2, 1);
    const auto second = rollback_world(reg, "sim-1", snap.id, RollbackOptions{});
    ASSERT_NE(first.archive_path, second.archive_path);

    const fs::path dir = archive_dir_of(world);
    { std::ofstream out(dir / "keep.txt"); out << "x"; }
    const fs::path precious = outside.path() / "precious.db";
    { std::ofstream out(precious); out << "x"; }
    // 名字合规，但是符号链接：不列、不删。
    const fs::path link = dir / "world.db.pre-rollback-20260101T000000Z-beef";
    fs::create_symlink(precious, link);

    const auto listed = list_rollback_archives(reg, "sim-1");
    ASSERT_EQ(listed.size(), 2U);
    EXPECT_GT(listed.at(0).name, listed.at(1).name);   // 名字降序
    std::vector<std::string> expected{fs::path(first.archive_path).filename().string(),
                                      fs::path(second.archive_path).filename().string()};
    std::ranges::sort(expected, std::greater<>());
    EXPECT_EQ(listed.at(0).name, expected.at(0));
    EXPECT_EQ(listed.at(1).name, expected.at(1));
    for (const auto& item : listed) {
        EXPECT_EQ(item.size_bytes, static_cast<std::int64_t>(fs::file_size(dir / item.name)));
    }

    EXPECT_EQ(purge_rollback_archives(reg, "sim-1"), 2U);
    EXPECT_FALSE(fs::exists(first.archive_path));
    EXPECT_FALSE(fs::exists(second.archive_path));
    EXPECT_TRUE(fs::exists(dir / "keep.txt"));
    EXPECT_TRUE(fs::is_symlink(link));
    EXPECT_TRUE(fs::exists(precious));
    EXPECT_TRUE(list_rollback_archives(reg, "sim-1").empty());
    EXPECT_EQ(engram_count(world.db_path), 2);   // 现役库不受清理影响
}

TEST(WorldArchives, UnknownWorld) {
    const TempDir tmp;
    const auto reg = registry_with_world(tmp, "sim-1");
    EXPECT_EQ(code_of([&] { (void)list_rollback_archives(reg, "nope"); }), "world_not_found");
    EXPECT_EQ(code_of([&] { (void)purge_rollback_archives(reg, "nope"); }), "world_not_found");
}

// archive/ 本身被换成指向外部的符号链接：解析后不在世界 home 之下，列出与清理都拒绝，
// 外部同名模式的文件一个字节不动（同 worlds/ 外链的 Codex 第二轮 Important 2）。
TEST(WorldArchives, RefusesArchiveDirSymlinkedOutside) {
    const TempDir tmp;
    const TempDir outside;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto world = reg.get("sim-1");
    const fs::path precious = outside.path() / "world.db.pre-rollback-20260101T000000Z-beef";
    { std::ofstream out(precious); out << "x"; }
    fs::create_directory_symlink(outside.path(), archive_dir_of(world));
    EXPECT_EQ(code_of([&] { (void)list_rollback_archives(reg, "sim-1"); }), "snapshot_failed");
    EXPECT_EQ(code_of([&] { (void)purge_rollback_archives(reg, "sim-1"); }), "snapshot_failed");
    EXPECT_TRUE(fs::exists(precious));

    // 回滚也不往外部目录里挪库：什么都不改。
    const auto snap = snapshot_then_write(reg, "sim-1", 2, 1);
    const std::string before = slurp(world.db_path);
    EXPECT_EQ(code_of([&] { (void)rollback_world(reg, "sim-1", snap.id, RollbackOptions{}); }),
              "snapshot_failed");
    EXPECT_EQ(slurp(world.db_path), before);
    EXPECT_TRUE(rollback_partials(world).empty());
    std::size_t outside_files = 0;
    for (const auto& item : fs::directory_iterator(outside.path())) { (void)item; ++outside_files; }
    EXPECT_EQ(outside_files, 1U);
}

// ---- W3 最终修复轮：世界 home 只从数据目录与 dir_id 推出（B1）--------------------------

// 目录树里的全部条目（相对路径，不跟随符号链接），用来断言「外部目录一个字节没多、没少」。
std::vector<std::string> tree_of(const fs::path& root) {
    std::vector<std::string> out;
    for (auto it = fs::recursive_directory_iterator(root); it != fs::recursive_directory_iterator();
         ++it) {
        out.push_back(fs::relative(it->path(), root).string());
    }
    std::ranges::sort(out);
    return out;
}

// 审查者的构造：注册表里 snapshot_dir 写成 <外部>/snapshots，它是指回 worlds/sim-1/snapshots 的
// 符号链接。旧实现取 snapshot_dir 的字面父目录当世界 home，归档建在 <外部>/archive，purge 删掉
// 外部预先放好的同名模式文件。世界 home 现在只由注册表目录推出：归档落在 worlds/sim-1/archive，
// 外部什么都不多、什么都不少。
TEST(WorldRollback, ArchiveStaysInWorldDirWhenSnapshotDirIsASymlinkFromOutside) {
    const TempDir tmp;
    const TempDir outside;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto snap = snapshot_then_write(reg, "sim-1", 2, 3);
    const fs::path real_snaps = world_dir_of(reg, "sim-1") / "snapshots";
    fs::create_directory_symlink(real_snaps, outside.path() / "snapshots");
    set_world_column(reg, "sim-1", "snapshot_dir", (outside.path() / "snapshots").string());
    set_snapshot_column(reg, "sim-1", snap.id, "path",
                        (outside.path() / "snapshots" / fs::path(snap.path).filename()).string());
    const auto world = reg.get("sim-1");
    const fs::path planted =
        outside.path() / "archive" / "notes.txt.pre-rollback-20250101T000000Z-abcd";
    fs::create_directories(planted.parent_path());
    { std::ofstream out(planted); out << "precious"; }
    const auto outside_before = tree_of(outside.path());

    const auto result = rollback_world(reg, "sim-1", snap.id, RollbackOptions{});
    EXPECT_EQ(engram_count(world.db_path), 2);
    EXPECT_EQ(fs::path(result.archive_path).parent_path(), world_dir_of(reg, "sim-1") / "archive");
    EXPECT_EQ(tree_of(outside.path()), outside_before);

    const auto listed = list_rollback_archives(reg, "sim-1");
    ASSERT_EQ(listed.size(), 1U);
    EXPECT_EQ(listed.front().name, fs::path(result.archive_path).filename().string());
    EXPECT_EQ(purge_rollback_archives(reg, "sim-1"), 1U);
    EXPECT_FALSE(fs::exists(result.archive_path));
    EXPECT_EQ(slurp(planted), "precious");
    EXPECT_EQ(tree_of(outside.path()), outside_before);
}

// 注册表不变，worlds/<sim-1>/snapshots 是指到 <外部>/snaps 的符号链接。这种布局已不再支持
// （用户裁定 2026-10-27）：validate_world 报 registry_corrupt。裁定 R12 起回滚、列出、清理自己先跑
// validate_world，同样拒绝：库不动，不建归档，外部只有原来那份快照文件。
TEST(WorldRollback, RefusesWhenSnapshotsDirPointsOutside) {
    const TempDir tmp;
    const TempDir outside;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto world = reg.get("sim-1");
    const auto snap = snapshot_then_write(reg, "sim-1", 2, 3);
    const fs::path real_snaps = world_dir_of(reg, "sim-1") / "snapshots";
    fs::rename(real_snaps, outside.path() / "snaps");
    fs::create_directory_symlink(outside.path() / "snaps", real_snaps);
    EXPECT_EQ(code_of([&] { validate_world(reg, "sim-1"); }), "registry_corrupt");
    const auto outside_before = tree_of(outside.path());
    const std::string db_before = slurp(world.db_path);

    EXPECT_EQ(code_of([&] { (void)rollback_world(reg, "sim-1", snap.id, RollbackOptions{}); }),
              "registry_corrupt");
    EXPECT_EQ(code_of([&] { (void)list_rollback_archives(reg, "sim-1"); }), "registry_corrupt");
    EXPECT_EQ(code_of([&] { (void)purge_rollback_archives(reg, "sim-1"); }), "registry_corrupt");
    EXPECT_EQ(engram_count(world.db_path), 5);
    EXPECT_EQ(slurp(world.db_path), db_before);
    EXPECT_TRUE(archive_is_empty(world));
    EXPECT_TRUE(rollback_partials(world).empty());
    EXPECT_EQ(tree_of(outside.path()), outside_before);
}

// worlds/<sim-1> 本身是指到外部的符号链接：回滚、列出、清理都 fail closed。开头的 validate_world
// 按布局规则 1 拒（registry_corrupt，裁定 R12）；checked_world_home 的同一检查（snapshot_failed，与
// RegistryDb::remove 同一边界）留作纵深防御。外部的库、快照和预先放好的归档名文件一个字节都不动。
TEST(WorldArchives, RefuseWhenWorldDirIsASymlinkOutside) {
    const TempDir tmp;
    const TempDir outside;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto snap = snapshot_then_write(reg, "sim-1", 2, 3);
    const fs::path home = world_dir_of(reg, "sim-1");
    fs::rename(home, outside.path() / "sim-1");
    fs::create_directory_symlink(outside.path() / "sim-1", home);
    const fs::path planted =
        outside.path() / "sim-1" / "archive" / "world.db.pre-rollback-20250101T000000Z-abcd";
    fs::create_directories(planted.parent_path());
    { std::ofstream out(planted); out << "precious"; }
    const auto outside_before = tree_of(outside.path());
    const std::string db_before = slurp(outside.path() / "sim-1" / "world.db");

    EXPECT_EQ(code_of([&] {
                  (void)rollback_world(reg, "sim-1", snap.id, RollbackOptions{});
              }), "registry_corrupt");
    EXPECT_EQ(code_of([&] { (void)list_rollback_archives(reg, "sim-1"); }),
              "registry_corrupt");
    EXPECT_EQ(code_of([&] { (void)purge_rollback_archives(reg, "sim-1"); }),
              "registry_corrupt");
    EXPECT_EQ(tree_of(outside.path()), outside_before);
    EXPECT_EQ(slurp(outside.path() / "sim-1" / "world.db"), db_before);
    EXPECT_EQ(slurp(planted), "precious");
}

// ---- W3 最终修复轮：现役库的第二个名字不是归档（A2）---------------------------

// 回滚死在建硬链接之后、原子替换之前：archive 里留下一个名字合规、却和现役库是同一个 inode
// 的条目。它不是归档（内容随现役库一起变），list 不列、purge 不删；下一次成功的回滚开头摘掉它。
TEST(WorldArchives, LiveDbAliasIsNotAnArchiveAndTheNextRollbackRemovesIt) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto world = reg.get("sim-1");
    const auto snap = snapshot_then_write(reg, "sim-1", 2, 3);
    const fs::path archive = archive_dir_of(world);
    fs::create_directories(archive);
    const fs::path alias = archive / "world.db.pre-rollback-20260101T000000Z-abcd";
    fs::create_hard_link(world.db_path, alias);
    ASSERT_EQ(fs::hard_link_count(world.db_path), 2U);

    EXPECT_TRUE(list_rollback_archives(reg, "sim-1").empty());
    EXPECT_EQ(purge_rollback_archives(reg, "sim-1"), 0U);
    EXPECT_TRUE(fs::exists(alias));

    const auto result = rollback_world(reg, "sim-1", snap.id, RollbackOptions{});
    EXPECT_FALSE(fs::exists(alias));
    EXPECT_EQ(engram_count(world.db_path), 2);
    EXPECT_EQ(fs::hard_link_count(world.db_path), 1U);
    EXPECT_EQ(fs::hard_link_count(result.archive_path), 1U);   // 旧库只剩真正的归档这一个名字
    const auto listed = list_rollback_archives(reg, "sim-1");
    ASSERT_EQ(listed.size(), 1U);
    EXPECT_EQ(listed.front().name, fs::path(result.archive_path).filename().string());
}

// ---- W3 最终修复轮：C++ 探测别的连接（B3）与库文件头（B6）---------------------

// 打开 db_path 并读一行，连接一直开着，直到析构。mode=ro 与读写两种都要被探测到。
class OpenReader {
public:
    OpenReader(const std::string& db_path, bool read_only) {
        const std::string uri = "file:" + db_path + (read_only ? "?mode=ro" : "");
        const int flags = SQLITE_OPEN_URI | (read_only ? SQLITE_OPEN_READONLY
                                                       : SQLITE_OPEN_READWRITE);
        sqlite3* raw = nullptr;
        EXPECT_EQ(sqlite3_open_v2(uri.c_str(), &raw, flags, nullptr), SQLITE_OK);
        handle_.reset(raw);
        persistence::Stmt query(raw, "SELECT COUNT(*) FROM engrams");
        EXPECT_TRUE(query.step_row());
    }
private:
    persistence::SqliteHandle handle_;
};

TEST(WorldRollback, RefusesWhileAnotherConnectionHasTheDbOpen) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto world = reg.get("sim-1");
    const auto snap = snapshot_then_write(reg, "sim-1", 2, 3);
    for (const bool read_only : {true, false}) {
        const std::string before = slurp(world.db_path);
        {
            const OpenReader reader(world.db_path, read_only);
            EXPECT_EQ(code_of([&] {
                          (void)rollback_world(reg, "sim-1", snap.id,
                                               RollbackOptions{});
                      }), "world_in_use") << "read_only=" << read_only;
            EXPECT_EQ(slurp(world.db_path), before) << "read_only=" << read_only;
            EXPECT_EQ(engram_count(world.db_path), 5) << "read_only=" << read_only;
            EXPECT_EQ(fs::hard_link_count(world.db_path), 1U) << "read_only=" << read_only;
            EXPECT_TRUE(archive_is_empty(world)) << "read_only=" << read_only;
            EXPECT_TRUE(rollback_partials(world).empty()) << "read_only=" << read_only;
        }
        // 连接关掉之后照常回滚。
        (void)rollback_world(reg, "sim-1", snap.id, RollbackOptions{});
        EXPECT_EQ(engram_count(world.db_path), 2) << "read_only=" << read_only;
        append_engrams(world.db_path, "sim-1", 2, 3);
        ASSERT_EQ(engram_count(world.db_path), 5);
        EXPECT_EQ(purge_rollback_archives(reg, "sim-1"), 1U);
    }
}

// 要被替换的不是 SQLite 库（default 世界的 db_path 不受布局约束）：拒绝，文件原样不动。空文件
// SQLite 自己会当成空库打开，只有文件头检查拦得住它。
TEST(WorldRollback, RefusesToReplaceAFileThatIsNotASqliteDatabase) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto world = reg.get("sim-1");
    const auto snap = snapshot_then_write(reg, "sim-1", 2, 1);
    for (const char* suffix : {"-wal", "-shm"}) { fs::remove(world.db_path + suffix); }
    for (const std::string content : {std::string("notes, not a database"), std::string()}) {
        { std::ofstream out(world.db_path, std::ios::trunc | std::ios::binary); out << content; }
        EXPECT_EQ(code_of([&] {
                      (void)rollback_world(reg, "sim-1", snap.id, RollbackOptions{});
                  }), "snapshot_failed") << "content='" << content << "'";
        EXPECT_EQ(slurp(world.db_path), content);
        EXPECT_EQ(fs::hard_link_count(world.db_path), 1U);
        EXPECT_TRUE(archive_is_empty(world));
        EXPECT_TRUE(rollback_partials(world).empty());
    }
}

// ---- W3 Codex 修复轮：回滚预检（C1）与 -shm 类型（C2）-------------------------

// 预检的一个用例：一份新的注册表与世界库，break_it 把它弄坏成某一种情况，然后断言
// check_rollback 抛 expected，且整个注册表目录一个条目都没多、没少，库字节不变。
// 预检之前先放一个上次崩溃留下的 partial：预检不清扫，它必须还在。
struct PreflightCase {
    std::string label{};
    std::string world = "sim-1";
    std::string snapshot_id{};                       // 空 = 用拍好的那份快照的 id
    RollbackOptions opts{};
    // (注册表, 外部目录, 快照)
    std::function<void(const RegistryDb&, const TempDir&, const SnapshotEntry&)> break_it{};
    std::string expected{};
};

void expect_preflight(const PreflightCase& tcase) {
    const TempDir tmp;
    const TempDir outside;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto snap = snapshot_then_write(reg, "sim-1", 2, 3);
    const auto world = reg.get("sim-1");
    const fs::path stale = fs::path(world.db_path).parent_path() /
                           ".world.db.rollback-0123456789abcdef.partial";
    { std::ofstream out(stale); out << "x"; }
    if (tcase.break_it) { tcase.break_it(reg, outside, snap); }
    // 库可能已被挪到外部（世界目录换成符号链接）：按库路径读，slurp 跟随符号链接。
    const std::string db_before = slurp(world.db_path);
    const auto tree_before = tree_of(tmp.path());
    const auto outside_before = tree_of(outside.path());
    const std::string snapshot_id = tcase.snapshot_id.empty() ? snap.id : tcase.snapshot_id;

    EXPECT_EQ(code_of([&] { check_rollback(reg, tcase.world, snapshot_id, tcase.opts); }),
              tcase.expected) << tcase.label;
    EXPECT_EQ(tree_of(tmp.path()), tree_before) << tcase.label;
    EXPECT_EQ(tree_of(outside.path()), outside_before) << tcase.label;
    EXPECT_EQ(slurp(world.db_path), db_before) << tcase.label;
    EXPECT_FALSE(fs::exists(fs::symlink_status(archive_dir_of(world)))) << tcase.label;
}

TEST(WorldRollbackCheck, RefusesEachReadOnlyCaseWithRollbackCodesAndTouchesNothing) {
    RollbackOptions zvec;
    zvec.vector_backend = "zvec";
    RollbackOptions starved;
    starved.free_space = [](const fs::path&) -> std::int64_t { return 1; };
    const std::vector<PreflightCase> cases{
        {.label = "1 unknown world", .world = "nope", .expected = "world_not_found"},
        {.label = "2 unknown snapshot", .snapshot_id = "x", .expected = "snapshot_not_found"},
        {.label = "3 backend", .opts = zvec, .expected = "snapshot_unsupported_backend"},
        {.label = "4 snapshot path escapes snapshot_dir",
         .break_it = [](const RegistryDb&, const TempDir& outside, const SnapshotEntry& snap) {
             const fs::path foreign = outside.path() / "foreign.db";
             fs::rename(snap.path, foreign);
             fs::create_symlink(foreign, snap.path);
         },
         .expected = "snapshot_failed"},
        {.label = "5a snapshot file missing",
         .break_it = [](const RegistryDb&, const TempDir&, const SnapshotEntry& snap) {
             fs::remove(snap.path);
         },
         .expected = "snapshot_corrupt"},
        {.label = "5b snapshot file is a directory",
         .break_it = [](const RegistryDb&, const TempDir&, const SnapshotEntry& snap) {
             fs::remove(snap.path);
             fs::create_directory(snap.path);
         },
         .expected = "snapshot_corrupt"},
        {.label = "6 world home resolves outside worlds/",
         .break_it = [](const RegistryDb& reg, const TempDir& outside, const SnapshotEntry&) {
             const fs::path home = world_dir_of(reg, "sim-1");
             fs::rename(home, outside.path() / "sim-1");
             fs::create_directory_symlink(outside.path() / "sim-1", home);
         },
         .expected = "registry_corrupt"},   // 开头的 validate_world（裁定 R12）
        {.label = "7 insufficient disk", .opts = starved, .expected = "insufficient_disk"},
    };
    for (const auto& tcase : cases) { expect_preflight(tcase); }
}

// 预检与回滚的检查是同一份代码：顺序相同，同时有两种毛病时报的是同一个码。
TEST(WorldRollbackCheck, ReportsTheSameFirstFailureAsRollback) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto snap = snapshot_then_write(reg, "sim-1", 2, 1);
    fs::remove(snap.path);                           // 第 3 步：snapshot_corrupt
    RollbackOptions both;
    both.free_space = [](const fs::path&) -> std::int64_t { return 1; };   // 第 4 步
    EXPECT_EQ(code_of([&] { check_rollback(reg, "sim-1", snap.id, both); }),
              "snapshot_corrupt");
    EXPECT_EQ(code_of([&] { (void)rollback_world(reg, "sim-1", snap.id, both); }),
              "snapshot_corrupt");
    both.vector_backend = "zvec";                    // 第 1 步排在最前
    EXPECT_EQ(code_of([&] { check_rollback(reg, "sim-1", snap.id, both); }),
              "snapshot_unsupported_backend");
    EXPECT_EQ(code_of([&] { (void)rollback_world(reg, "sim-1", snap.id, both); }),
              "snapshot_unsupported_backend");
}

// 有效快照：预检通过，什么都不动（不清扫上次的 partial、不建 archive/、库字节不变）；之后的
// 回滚照常成功，并且由它来清扫。
TEST(WorldRollbackCheck, PassesForAValidSnapshotAndLeavesTheSweepToRollback) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto world = reg.get("sim-1");
    const auto snap = snapshot_then_write(reg, "sim-1", 2, 3);
    const fs::path stale = fs::path(world.db_path).parent_path() /
                           ".world.db.rollback-0123456789abcdef.partial";
    { std::ofstream out(stale); out << "x"; }
    const std::string db_before = slurp(world.db_path);
    const auto tree_before = tree_of(tmp.path());

    EXPECT_EQ(code_of([&] { check_rollback(reg, "sim-1", snap.id, RollbackOptions{}); }),
              "<no throw>");
    EXPECT_EQ(tree_of(tmp.path()), tree_before);
    EXPECT_TRUE(fs::exists(stale));
    EXPECT_FALSE(fs::exists(archive_dir_of(world)));
    EXPECT_EQ(slurp(world.db_path), db_before);

    (void)rollback_world(reg, "sim-1", snap.id, RollbackOptions{});
    EXPECT_FALSE(fs::exists(stale));
    EXPECT_EQ(engram_count(world.db_path), 2);
}

// -shm 存在但不是普通文件（目录、符号链接）：在动任何东西之前拒绝（snapshot_failed），目标库、
// 它旁边的 -shm 原样不动，不建归档名，不留 partial。
TEST(WorldRollback, RefusesWhenShmIsNotARegularFile) {
    const TempDir tmp;
    const TempDir outside;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto world = reg.get("sim-1");
    const auto snap = snapshot_then_write(reg, "sim-1", 2, 3);
    const fs::path shm = world.db_path + "-shm";
    const fs::path target = outside.path() / "elsewhere-shm";
    { std::ofstream out(target); out << "not ours"; }
    const std::vector<std::pair<std::string, std::function<void()>>> shapes{
        {"directory", [&] { fs::create_directory(shm); }},
        {"symlink", [&] { fs::create_symlink(target, shm); }},
    };
    for (const auto& [label, plant] : shapes) {
        fs::remove_all(shm);
        plant();
        const std::string before = slurp(world.db_path);
        const auto status_before = fs::symlink_status(shm).type();
        EXPECT_EQ(code_of([&] {
                      (void)rollback_world(reg, "sim-1", snap.id, RollbackOptions{});
                  }), "snapshot_failed") << label;
        EXPECT_EQ(slurp(world.db_path), before) << label;
        EXPECT_EQ(fs::symlink_status(shm).type(), status_before) << label;
        EXPECT_EQ(fs::hard_link_count(world.db_path), 1U) << label;
        EXPECT_TRUE(archive_is_empty(world)) << label;
        EXPECT_TRUE(rollback_partials(world).empty()) << label;
    }
    EXPECT_EQ(slurp(target), "not ours");
    fs::remove_all(shm);
    EXPECT_EQ(engram_count(world.db_path), 5);
}

// ---- 世界管理加固 T1：布局约束在解析符号链接之后判断 -------------------------------
// 计划 docs/superpowers/plans/2026-10-27-world-hardening.md Task 1；spec §5.1 归档段、§12。
// 用户裁定（2026-10-27）：世界目录本身、非默认世界的库文件、snapshots/ 解析符号链接后都必须
// 严格位于 <数据目录>/worlds/<dir_id> 之下，否则 validate_world 报 registry_corrupt（导入 worlds.json
// 时同一套规则，见 test_registry_import.cpp）。数据目录本身可以是符号链接
// （ValidatesThroughSymlinkedDataDir）。

// 把 inside 挪到 outside_dir 下同名处，原处换成指向它的符号链接。
void move_out_and_link(const fs::path& inside, const fs::path& outside_dir) {
    const fs::path moved = outside_dir / inside.filename();
    fs::rename(inside, moved);
    if (fs::is_directory(moved)) {
        fs::create_directory_symlink(moved, inside);
    } else {
        fs::create_symlink(moved, inside);
    }
}

// 注册表（default + sim-1）和一个放了一个文件的外部目录。
struct LayoutCase {
    TempDir tmp;
    TempDir outside;
    RegistryDb reg = registry_with_world(tmp, "sim-1");
    fs::path home = world_dir_of(reg, "sim-1");

    LayoutCase() {
        std::ofstream out(outside.path() / "keep.txt");
        out << "x";
    }
};


// 越界布局：validate_world 与 validate_registry 都报 registry_corrupt，消息里同时有 field 与 reason，
// 确认是预期的那条规则拒的，而不是更早的某个校验恰好抛了同一个码；注册表内容不变；外部目录一个
// 条目都没多、没少。field 是消息里点名的字段标签（"db_path of 'sim-1'"）；越界时 reason 是
// "resolves to"。解析出错时消息不带字段标签，field 传原路径、reason 传 "cannot resolve"。
// NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
void expect_layout_refused(const LayoutCase& lcase, const std::string& label,
                           const std::string& field, const std::string& reason = "resolves to") {
    const std::string before = registry_dump(lcase.reg);
    const auto outside_before = tree_of(lcase.outside.path());
    EXPECT_EQ(code_of([&] { validate_world(lcase.reg, "sim-1"); }), "registry_corrupt") << label;
    EXPECT_EQ(code_of([&] { validate_registry(lcase.reg); }), "registry_corrupt") << label;
    for (const std::string& what : {what_of([&] { validate_world(lcase.reg, "sim-1"); }),
                                    what_of([&] { validate_registry(lcase.reg); })}) {
        EXPECT_NE(what.find(field), std::string::npos) << label << ": " << what;
        EXPECT_NE(what.find(reason), std::string::npos) << label << ": " << what;
    }
    EXPECT_EQ(registry_dump(lcase.reg), before) << label;
    EXPECT_EQ(tree_of(lcase.outside.path()), outside_before) << label;
}

void expect_layout_allowed(const LayoutCase& lcase) {
    EXPECT_EQ(code_of([&] { validate_world(lcase.reg, "sim-1"); }), "<no throw>");
    EXPECT_EQ(code_of([&] { validate_registry(lcase.reg); }), "<no throw>");
}

TEST(WorldLayout, RefusesSnapshotsDirResolvingOutsideTheWorldDir) {
    const LayoutCase lcase;
    move_out_and_link(lcase.home / "snapshots", lcase.outside.path());
    expect_layout_refused(lcase, "snapshots/ -> outside", "snapshot_dir of 'sim-1'");
}

TEST(WorldLayout, RefusesWorldDirResolvingOutside) {
    const LayoutCase lcase;
    move_out_and_link(lcase.home, lcase.outside.path());
    expect_layout_refused(lcase, "worlds/<sim-1> -> outside", "home of 'sim-1'");
}

TEST(WorldLayout, RefusesWorldDbResolvingOutside) {
    const LayoutCase lcase;
    const fs::path foreign = lcase.outside.path() / "world.db";
    { std::ofstream out(foreign); out << "x"; }
    fs::create_symlink(foreign, lcase.reg.get("sim-1").db_path);
    expect_layout_refused(lcase, "world.db -> outside", "db_path of 'sim-1'");
}

// weakly_canonical 不解析悬空的符号链接（目标不存在时当成普通的不存在路径），而 SQLite 以
// O_CREAT 打开它会在链接目标处建库：世界库就会建到世界目录之外。
TEST(WorldLayout, RefusesDanglingWorldDbLink) {
    const LayoutCase lcase;
    fs::create_symlink(lcase.outside.path() / "later.db", lcase.reg.get("sim-1").db_path);
    expect_layout_refused(lcase, "world.db -> missing file outside", "db_path of 'sim-1'");
}

// 同上，悬空的 snapshots/ 链接，但注册表里的 snapshot_dir 以 "/" 或 "/." 结尾：lstat("链接/") 会跟随
// 链接，把链接本身藏起来，字面结果就放行了；所以规则先去掉末尾的分隔符再判断，不取决于注册表里的
// 写法。（mkdir("链接/") 在 macOS 上会在链接目标处建目录，但创建快照目录时走不走得到这一步取决于
// 实现，这里不以它为依据。）
TEST(WorldLayout, RefusesDanglingSnapshotsLinkBehindTrailingSlash) {
    for (const std::string suffix : {"/", "/."}) {
        LayoutCase lcase;
        const std::string snapshot_dir = lcase.reg.get("sim-1").snapshot_dir + suffix;
        set_world_column(lcase.reg, "sim-1", "snapshot_dir", snapshot_dir);
        const fs::path snaps = lcase.home / "snapshots";
        fs::remove(snaps);
        fs::create_directory_symlink(lcase.outside.path() / "later", snaps);
        expect_layout_refused(lcase, "snapshot_dir '" + snapshot_dir + "'",
                              "snapshot_dir of 'sim-1'");
    }
}

// 解析本身出错（自指符号链接，ELOOP）：证明不了在世界目录里，按越界拒绝，消息带上原因。
TEST(WorldLayout, RefusesPathThatCannotBeResolved) {
    const LayoutCase lcase;
    const fs::path snaps = lcase.home / "snapshots";
    fs::remove(snaps);
    fs::create_symlink("snapshots", snaps);   // 指向自己
    expect_layout_refused(lcase, "snapshots/ -> itself", snaps.string(), "cannot resolve");
    try {
        validate_world(lcase.reg, "sim-1");
        ADD_FAILURE() << "validate_world did not throw";
    } catch (const WorldStoreError& err) {
        const std::string what = err.what();
        EXPECT_NE(what.find("cannot resolve " + snaps.string()), std::string::npos) << what;
    }
}

// snapshots/ 指向同一世界目录里的另一个子目录：解析后仍在世界目录之下，放行。
TEST(WorldLayout, AllowsSnapshotsDirLinkedWithinTheWorldDir) {
    const LayoutCase lcase;
    const fs::path snaps = lcase.home / "snapshots";
    fs::rename(snaps, lcase.home / "store");
    fs::create_directory_symlink(lcase.home / "store", snaps);
    expect_layout_allowed(lcase);
}

// 世界目录还不存在（未建目录的世界）：照常放行，不特判。
TEST(WorldLayout, AllowsWorldDirThatDoesNotExistYet) {
    const LayoutCase lcase;
    fs::remove_all(lcase.home);
    expect_layout_allowed(lcase);
}

// 同上，但 snapshots/ 是相对链接（ln -s store snapshots）：相对于链接所在的世界目录解析，放行。
TEST(WorldLayout, AllowsSnapshotsDirRelativeLinkWithinTheWorldDir) {
    const LayoutCase lcase;
    const fs::path snaps = lcase.home / "snapshots";
    fs::rename(snaps, lcase.home / "store");
    fs::create_directory_symlink("store", snaps);
    expect_layout_allowed(lcase);
}

// ---- 最终修复轮 A1：世界目录解析后必须恰好是 worlds/<它自己的 dir_id> --------------------------
// worlds/<sim-1> -> worlds/<sim-2> 时 sim-1 的目录解析后仍严格在 worlds/ 之下，旧规则放行；随后
// 删除 sim-1 经 remove_within 解析到 sim-2，把兄弟世界的整个目录和库删掉。

// 目录树里每个条目的相对路径和内容（普通文件取字节，其余记类型），用来断言「一个字节都没变」。
std::vector<std::pair<std::string, std::string>> contents_of(const fs::path& root) {
    std::vector<std::pair<std::string, std::string>> out;
    for (const auto& rel : tree_of(root)) {
        const fs::path entry = root / rel;
        const auto type = fs::symlink_status(entry).type();
        std::string body = "<type " + std::to_string(static_cast<int>(type)) + ">";
        if (type == fs::file_type::regular) { body = slurp(entry); }
        out.emplace_back(rel, std::move(body));
    }
    return out;
}

// LayoutCase 再登记一个 sim-2，库里放上数据、快照目录里放一个文件。
fs::path add_populated_sibling(LayoutCase& lcase) {
    add_world(lcase.reg, "sim-2");
    const auto sibling = lcase.reg.get("sim-2");
    seed_world_db(sibling.db_path, "sim-2", 3);
    { std::ofstream out(fs::path(sibling.snapshot_dir) / "keep.db"); out << "snap"; }
    return world_dir_of(lcase.reg, "sim-2");
}

TEST(WorldLayout, RefusesWorldDirAliasedToASiblingWorldDir) {
    LayoutCase lcase;
    const fs::path sibling = add_populated_sibling(lcase);
    fs::remove_all(lcase.home);
    fs::create_directory_symlink(sibling, lcase.home);
    const auto sibling_before = contents_of(sibling);
    expect_layout_refused(lcase, "worlds/<sim-1> -> worlds/<sim-2>", "home of 'sim-1'");
    EXPECT_EQ(contents_of(sibling), sibling_before);
    EXPECT_EQ(engram_count(lcase.reg.get("sim-2").db_path), 3);
}

TEST(WorldLayout, RefusesWorldDirAliasedIntoASiblingWorldDir) {
    LayoutCase lcase;
    const fs::path sibling = add_populated_sibling(lcase);
    fs::create_directories(sibling / "sub");
    fs::remove_all(lcase.home);
    fs::create_directory_symlink(sibling / "sub", lcase.home);
    const auto sibling_before = contents_of(sibling);
    expect_layout_refused(lcase, "worlds/<sim-1> -> worlds/<sim-2>/sub", "home of 'sim-1'");
    EXPECT_EQ(contents_of(sibling), sibling_before);
}

// 全新数据目录：RegistryDb::open 时 worlds/ 还不存在，create 再建出世界目录，校验都放行。
TEST(WorldLayout, AllowsFreshRegistryThenCreateWorld) {
    const TempDir tmp;
    auto reg = open_registry(tmp);
    EXPECT_FALSE(fs::exists(tmp.path() / "worlds"));
    EXPECT_EQ(code_of([&] { validate_registry(reg); }), "<no throw>");
    add_world(reg, "sim-1");
    EXPECT_EQ(code_of([&] { validate_registry(reg); }), "<no throw>");
}

// worlds/ 整体是指向数据目录内另一处的符号链接：两侧都解析，世界目录解析后仍是
// 「解析后的 worlds/」/<dir_id>，放行。
TEST(WorldLayout, AllowsWorldsRootLinkedWithinTheRegistryDir) {
    const TempDir tmp;
    fs::create_directories(tmp.path() / "store");
    fs::create_directory_symlink(tmp.path() / "store", tmp.path() / "worlds");
    auto reg = registry_with_world(tmp, "sim-1");
    EXPECT_EQ(code_of([&] { validate_registry(reg); }), "<no throw>");
    EXPECT_TRUE(fs::is_directory(tmp.path() / "store" / reg.get("sim-1").dir_id));
}


// ---- 整支终审修复轮 M1：不经校验的破坏性入口自己查世界目录恰好是 worlds/<dir_id> -------------
// RegistryDb::remove 与 checked_world_home（回滚、列出、清理归档）可以不先跑 validate_world 直接
// 调用，校验的布局规则（A1）拦不到它们。worlds/<sim-1> -> worlds/<sim-2> 时，只查「严格在 worlds/
// 之下」会放行：删除 sim-1 删掉 sim-2 的整个目录，purge_rollback_archives("sim-1") 删掉 sim-2 的
// 回滚归档。两处都必须抛 snapshot_failed，sim-2 一个字节都不动。

// 注册表里有 sim-1 与 sim-2，sim-2 的库有 3 行、回滚过一次（archive/ 里有一份归档）、快照目录里多放
// 一个文件；然后把 worlds/<sim-1> 换成指向 worlds/<sim-2> 的符号链接。
struct SiblingAliasCase {
    TempDir tmp;
    RegistryDb reg = registry_with_world(tmp, "sim-1");
    fs::path alias = world_dir_of(reg, "sim-1");
    fs::path sibling;
    std::string archive;    // sim-2 那份归档的完整路径

    SiblingAliasCase() {
        add_world(reg, "sim-2");
        sibling = world_dir_of(reg, "sim-2");
        const auto snap = snapshot_then_write(reg, "sim-2", 3, 2);
        archive = rollback_world(reg, "sim-2", snap.id, RollbackOptions{}).archive_path;
        { std::ofstream out(fs::path(reg.get("sim-2").snapshot_dir) / "keep.db"); out << "x"; }
        fs::remove_all(alias);
        fs::create_directory_symlink(sibling, alias);
    }
};

TEST(WorldLayout, DeleteWorldRefusesWorldDirAliasedToASiblingWorldDir) {
    SiblingAliasCase tcase;
    ASSERT_TRUE(fs::is_regular_file(tcase.archive));
    const auto sibling_before = contents_of(tcase.sibling);
    const std::string what = what_of([&] { tcase.reg.remove("sim-1"); });
    EXPECT_EQ(code_of([&] { tcase.reg.remove("sim-1"); }), "snapshot_failed");
    EXPECT_NE(what.find("home of 'sim-1'"), std::string::npos) << what;
    EXPECT_NE(what.find("resolves to"), std::string::npos) << what;
    EXPECT_EQ(contents_of(tcase.sibling), sibling_before);
    EXPECT_EQ(engram_count(tcase.reg.get("sim-2").db_path), 3);
    EXPECT_TRUE(fs::is_symlink(tcase.alias));
    EXPECT_EQ(tcase.reg.get("sim-1").name, "sim-1");      // 注册项保留
    EXPECT_EQ(tcase.reg.get("sim-2").name, "sim-2");
}

TEST(WorldLayout, PurgeArchivesRefusesWorldDirAliasedToASiblingWorldDir) {
    SiblingAliasCase tcase;
    ASSERT_TRUE(fs::is_regular_file(tcase.archive));
    const auto sibling_before = contents_of(tcase.sibling);
    // 开头的 validate_world 按布局规则 1 拒（registry_corrupt，裁定 R12）：消息与校验相同。
    const std::string what =
        what_of([&] { (void)purge_rollback_archives(tcase.reg, "sim-1"); });
    EXPECT_EQ(code_of([&] { (void)purge_rollback_archives(tcase.reg, "sim-1"); }),
              "registry_corrupt");
    EXPECT_NE(what.find("home of 'sim-1'"), std::string::npos) << what;
    EXPECT_NE(what.find("resolves to"), std::string::npos) << what;
    // 同一个检查也挡住列出与回滚：sim-1 的操作不会落到 sim-2 上。
    EXPECT_EQ(code_of([&] { (void)list_rollback_archives(tcase.reg, "sim-1"); }),
              "registry_corrupt");
    EXPECT_EQ(code_of([&] {
                  (void)rollback_world(tcase.reg, "sim-1", "x", RollbackOptions{});
              }), "registry_corrupt");
    EXPECT_EQ(contents_of(tcase.sibling), sibling_before);
    EXPECT_TRUE(fs::is_regular_file(tcase.archive));
    EXPECT_EQ(engram_count(tcase.reg.get("sim-2").db_path), 3);
}

// 注册表里的 dir_id 被手改成越出 worlds/ 的路径：回滚、列出、清理开头的 validate_world 先验 dir_id
// 的形状（registry_corrupt，裁定 R12），什么都不建、不删。checked_world_home 的同一检查
// （snapshot_failed）是纵深防御。
TEST(WorldLayout, ArchiveOperationsRefuseATamperedDirId) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto snap = snapshot_then_write(reg, "sim-1", 2, 1);
    fs::create_directories(tmp.path() / "x" / "archive");
    const fs::path planted = tmp.path() / "x" / "archive" / "world.db.pre-rollback-20250101T000000Z-abcd";
    { std::ofstream out(planted); out << "precious"; }
    const std::string db_before = slurp(reg.get("sim-1").db_path);
    for (const char* dir_id : {"../x", "..", "a/b", ""}) {
        set_world_column(reg, "sim-1", "dir_id", dir_id);
        EXPECT_EQ(code_of([&] { (void)rollback_world(reg, "sim-1", snap.id, RollbackOptions{}); }),
                  "registry_corrupt") << dir_id;
        EXPECT_EQ(code_of([&] { check_rollback(reg, "sim-1", snap.id, RollbackOptions{}); }),
                  "registry_corrupt") << dir_id;
        EXPECT_EQ(code_of([&] { (void)list_rollback_archives(reg, "sim-1"); }), "registry_corrupt")
            << dir_id;
        EXPECT_EQ(code_of([&] { (void)purge_rollback_archives(reg, "sim-1"); }), "registry_corrupt")
            << dir_id;
    }
    EXPECT_EQ(slurp(planted), "precious");
    EXPECT_EQ(slurp(reg.get("sim-1").db_path), db_before);
}

// 别名之外的合法布局不受影响：删除与归档操作照常工作（worlds/ 整体外链到数据目录内）。
TEST(WorldLayout, DeleteAndArchivesStillWorkWhenWorldsRootIsLinkedWithinTheRegistryDir) {
    const TempDir tmp;
    fs::create_directories(tmp.path() / "store");
    fs::create_directory_symlink(tmp.path() / "store", tmp.path() / "worlds");
    auto reg = registry_with_world(tmp, "sim-1");
    add_world(reg, "sim-2");
    const std::string sim1_dir = reg.get("sim-1").dir_id;
    const std::string sim2_dir = reg.get("sim-2").dir_id;
    const auto snap = snapshot_then_write(reg, "sim-1", 2, 1);
    (void)rollback_world(reg, "sim-1", snap.id, RollbackOptions{});
    EXPECT_EQ(list_rollback_archives(reg, "sim-1").size(), 1U);
    EXPECT_EQ(purge_rollback_archives(reg, "sim-1"), 1U);
    reg.remove("sim-2");
    EXPECT_FALSE(fs::exists(tmp.path() / "store" / sim2_dir));
    EXPECT_TRUE(fs::is_directory(tmp.path() / "store" / sim1_dir));
}


// ---- 整支终审修复轮 M2：worlds/ 是悬空链接时，报错点名 worlds/ 解析到了哪里 -------------------
// weakly_canonical 不跟随末端悬空的链接：worlds/ 字面放行，随后由世界目录那条规则以
// 「home of 'default' <reg>/worlds/default resolves to <reg>/worlds/default, not …」拒绝，像是说它
// 解析到了自己。全新安装（第一次打开数据目录、建第一个世界）同样命中。拒绝本身不变。
TEST(WorldLayout, DanglingWorldsRootIsRefusedByTheWorldsRootRule) {
    const TempDir tmp;
    const TempDir outside;
    const fs::path worlds = tmp.path() / "worlds";
    const fs::path unmounted = outside.path() / "unmounted" / "worlds";
    fs::create_directory_symlink(unmounted, worlds);
    const auto check = [&](const std::string& label, const std::function<void()>& action) {
        EXPECT_EQ(code_of(action), "registry_corrupt") << label;
        const std::string what = what_of(action);
        EXPECT_NE(what.find(worlds.string() + " resolves to "), std::string::npos)
            << label << ": " << what;
        EXPECT_NE(what.find("unmounted"), std::string::npos) << label << ": " << what;
        EXPECT_NE(what.find(", outside "), std::string::npos) << label << ": " << what;
        EXPECT_EQ(what.find("home of"), std::string::npos) << label << ": " << what;
    };
    {
        auto reg = open_registry(tmp);
        check("fresh install", [&] { validate_registry(reg); });
        check("create", [&] { add_world(reg, "sim-1"); });
    }
    fs::remove(tmp.path() / "registry.db");
    for (const char* sidecar : {"registry.db-wal", "registry.db-shm"}) {
        fs::remove(tmp.path() / sidecar);
    }
    { std::ofstream out(tmp.path() / "worlds.json"); out << R"({"version":1,"default":"default","active":"default","snapshot_retention":5,"worlds":{"default":{"tenant":"default","db_path":")" << (tmp.path() / "dashboard.db").string() << R"(","snapshot_dir":")" << (worlds / "default" / "snapshots").string() << R"(","label":"","created_at":""}},"snapshots":{}})"; }
    check("import", [&] { import_worlds_json(tmp.path()); });
    EXPECT_FALSE(fs::exists(outside.path() / "unmounted"));   // 外部什么都没建出来
}

// ---- 整支终审修复轮 M3：布局拒绝的消息末尾带补救提示 -----------------------------------------
// 违反布局的注册表让服务在启动时（validate_registry）就拒绝启动，消息要说清怎么改：世界目录内的
// 路径不能外链，换盘要整个挪数据目录。
TEST(WorldLayout, LayoutRefusalsCarryARemedyHint) {
    const std::string hint = "move the whole registry directory";
    {
        const LayoutCase lcase;
        move_out_and_link(lcase.home / "snapshots", lcase.outside.path());
        const std::string what = what_of([&] { validate_registry(lcase.reg); });
        EXPECT_NE(what.find("snapshot_dir of 'sim-1'"), std::string::npos) << what;
        EXPECT_NE(what.find(hint), std::string::npos) << what;
    }
    {
        LayoutCase lcase;
        const fs::path sibling = add_populated_sibling(lcase);
        fs::remove_all(lcase.home);
        fs::create_directory_symlink(sibling, lcase.home);
        const std::string what = what_of([&] { validate_world(lcase.reg, "sim-1"); });
        EXPECT_NE(what.find("home of 'sim-1'"), std::string::npos) << what;
        EXPECT_NE(what.find(hint), std::string::npos) << what;
    }
}


// ---- 裁定 R12：删除或替换文件的入口自己先跑 validate_world --------------------------------
// 以前每次操作之前都经 load_registry 校验整份注册表；现在 take_snapshot（含残留清扫）、
// apply_retention、delete_snapshot、take_auto_snapshot（含修剪）、check_rollback、rollback_world、
// list / purge_rollback_archives 开头都调 validate_world。snapshot_dir 被手改到世界目录之外
// （registry.db 上直接 UPDATE），或 snapshots/ 被换成指到外部的符号链接时，它们抛与 validate_world
// 相同的码，外部的文件一个字节都不动。

// sim-1：库有 2 行，登记了 3 份快照（保留份数 2，修剪会删一份），按时间 24 小时开着。然后把快照目录
// 挪到外部 <outside>/snaps：via_symlink 时 worlds/<sim-1>/snapshots 换成指向它的符号链接（注册表不变）；
// 否则 snapshot_dir 与快照路径在 registry.db 里直接改成外部路径。外部目录里还放了残留清扫会删的
// 诱饵（快照 id 形状的 .db.partial 与未登记的 .db）和一个哨兵文件。
struct EscapedSnapshotDir {
    TempDir tmp;
    TempDir outside;
    RegistryDb reg = registry_with_world(tmp, "sim-1");
    std::vector<std::string> ids;   // 最新在前

    explicit EscapedSnapshotDir(bool via_symlink) {
        set_auto_snapshot(reg, "sim-1", AutoSnapshotConfig{true, 24});
        seed_world_db(reg.get("sim-1").db_path, "sim-1", 2);
        set_retention(reg, 10);
        for (int idx = 0; idx < 3; ++idx) { (void)take_snapshot(reg, "sim-1", SnapshotOptions{}); }
        set_retention(reg, 2);
        ids = ids_of(reg.snapshots("sim-1"));
        EXPECT_EQ(code_of([&] { validate_world(reg, "sim-1"); }), "<no throw>");

        const fs::path real = world_dir_of(reg, "sim-1") / "snapshots";
        const fs::path moved = outside.path() / "snaps";
        fs::rename(real, moved);
        if (via_symlink) {
            fs::create_directory_symlink(moved, real);
        } else {
            set_world_column(reg, "sim-1", "snapshot_dir", moved.string());
            for (const auto& snap : reg.snapshots("sim-1")) {
                set_snapshot_column(reg, "sim-1", snap.id, "path",
                                    (moved / fs::path(snap.path).filename()).string());
            }
        }
        for (const char* bait : {"20260101T000000Z-dead.db.partial", "20260101T000000Z-beef.db",
                                 "sentinel.txt"}) {
            std::ofstream out(moved / bait);
            out << "precious";
        }
    }
};

// 外部目录的指纹：每个条目的相对路径、类型与大小，普通文件再带上内容的 std::hash（失败时消息可读，
// 不必打出整份 SQLite 文件）。
std::vector<std::string> fingerprint_of(const fs::path& root) {
    std::vector<std::string> out;
    for (const auto& rel : tree_of(root)) {
        const fs::path entry = root / rel;
        const auto type = fs::symlink_status(entry).type();
        std::string line = rel + " type=" + std::to_string(static_cast<int>(type));
        if (type == fs::file_type::regular) {
            line += " size=" + std::to_string(fs::file_size(entry)) +
                    " hash=" + std::to_string(std::hash<std::string>{}(slurp(entry)));
        }
        out.push_back(std::move(line));
    }
    return out;
}

TEST(WorldGuard, DestructiveEntryPointsRefuseASnapshotDirOutsideTheWorld) {
    const std::vector<std::pair<std::string, std::function<void(RegistryDb&, const std::string&)>>> ops{
        {"take_snapshot",
         [](RegistryDb& reg, const std::string&) { (void)take_snapshot(reg, "sim-1", SnapshotOptions{}); }},
        {"apply_retention", [](RegistryDb& reg, const std::string&) { (void)apply_retention(reg, "sim-1"); }},
        {"delete_snapshot",
         [](RegistryDb& reg, const std::string& snap_id) { delete_snapshot(reg, "sim-1", snap_id); }},
        {"take_auto_snapshot interval",
         [](RegistryDb& reg, const std::string&) {
             (void)take_auto_snapshot(reg, "sim-1", AutoTrigger{"interval", "", "2031-01-01T00:00:00Z"});
         }},
        {"take_auto_snapshot session",
         [](RegistryDb& reg, const std::string&) {
             (void)take_auto_snapshot(reg, "sim-1", AutoTrigger{"session", "s-new", "2031-01-01T00:00:00Z"});
         }},
        {"check_rollback",
         [](RegistryDb& reg, const std::string& snap_id) {
             check_rollback(reg, "sim-1", snap_id, RollbackOptions{});
         }},
        {"rollback_world",
         [](RegistryDb& reg, const std::string& snap_id) {
             (void)rollback_world(reg, "sim-1", snap_id, RollbackOptions{});
         }},
        {"list_rollback_archives",
         [](RegistryDb& reg, const std::string&) { (void)list_rollback_archives(reg, "sim-1"); }},
        {"purge_rollback_archives",
         [](RegistryDb& reg, const std::string&) { (void)purge_rollback_archives(reg, "sim-1"); }},
    };
    for (const bool via_symlink : {false, true}) {
        for (const auto& [name, op] : ops) {
            const std::string label = name + (via_symlink ? " (snapshots/ symlinked out)"
                                                          : " (snapshot_dir edited in registry.db)");
            EscapedSnapshotDir tcase(via_symlink);
            const std::string expected = code_of([&] { validate_world(tcase.reg, "sim-1"); });
            ASSERT_EQ(expected, "registry_corrupt") << label;
            const auto outside_before = fingerprint_of(tcase.outside.path());
            const std::string registry_before = registry_dump(tcase.reg);
            const std::string db_before = slurp(tcase.reg.get("sim-1").db_path);
            // 要删的那份是最旧的：修剪的第一个 victim，也是回滚的目标。
            const std::string target = tcase.ids.back();

            const std::string what = what_of([&] { op(tcase.reg, target); });
            EXPECT_EQ(code_of([&] { op(tcase.reg, target); }), expected) << label;
            EXPECT_NE(what.find("snapshot_dir of 'sim-1'"), std::string::npos) << label << ": " << what;
            EXPECT_EQ(fingerprint_of(tcase.outside.path()), outside_before) << label;
            EXPECT_EQ(slurp(tcase.outside.path() / "snaps" / "sentinel.txt"), "precious") << label;
            EXPECT_EQ(registry_dump(tcase.reg), registry_before) << label;
            EXPECT_EQ(slurp(tcase.reg.get("sim-1").db_path), db_before) << label;
            EXPECT_FALSE(fs::exists(fs::symlink_status(world_dir_of(tcase.reg, "sim-1") / "archive")))
                << label;
        }
    }
}

// ---- W4：自动快照配置、快照元数据、按触发拍快照、会话清单 ---------------------------
// 计划：docs/superpowers/plans/2026-10-28-world-w4-sessions.md Task 1。

// 旧注册表里 auto_snapshot 的读法（缺字段补默认值、显式值照写、形状不对是损坏、kind/reason 配对）
// 现在只在导入 worlds.json 时用到，用例在 test_registry_import.cpp。

AutoSnapshotConfig auto_of(const RegistryDb& reg, const std::string& world) {
    return settings_from_json(reg.get(world).settings_json).background.auto_snapshot;
}

// 自动快照配置在世界设置的 background.auto_snapshot 里：默认世界与新建世界的设置是 "{}" 时取
// WorldSettings 的默认值 {false, 24}；用例里 add_world 写明按会话 {true, 0}（旧 create_world 的默认）。
TEST(WorldAutoConfig, DefaultsComeFromTheWorldSettings) {
    const TempDir tmp;
    auto reg = open_registry(tmp);
    EXPECT_EQ(auto_of(reg, "default"), (AutoSnapshotConfig{.on_session = false, .interval_hours = 24}));
    (void)reg.create("plain", "", {}, "{}", kCreatedAt);
    EXPECT_EQ(auto_of(reg, "plain"), (AutoSnapshotConfig{.on_session = false, .interval_hours = 24}));
    add_world(reg, "sim-1");
    EXPECT_EQ(auto_of(reg, "sim-1"), (AutoSnapshotConfig{.on_session = true, .interval_hours = 0}));
}

TEST(WorldAutoConfig, SetAutoSnapshotWritesAndValidates) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    // 其余设置原样保留：只改 background.auto_snapshot。
    WorldSettings custom;
    custom.background.tick_interval_s = 5.0;
    custom.source_lane.k_source = 7;
    custom.background.auto_snapshot = AutoSnapshotConfig{.on_session = true, .interval_hours = 0};
    reg.update_settings("sim-1", settings_to_json(custom));

    set_auto_snapshot(reg, "sim-1", AutoSnapshotConfig{false, 12});
    EXPECT_EQ(auto_of(reg, "sim-1"), (AutoSnapshotConfig{.on_session = false, .interval_hours = 12}));
    const WorldSettings after = settings_from_json(reg.get("sim-1").settings_json);
    EXPECT_EQ(after.background.tick_interval_s, 5.0);
    EXPECT_EQ(after.source_lane.k_source, 7);
    set_auto_snapshot(reg, "sim-1", AutoSnapshotConfig{true, 8760});   // 上界放行
    EXPECT_EQ(auto_of(reg, "sim-1").interval_hours, 8760);
    set_auto_snapshot(reg, "sim-1", AutoSnapshotConfig{true, 0});      // 下界放行
    EXPECT_EQ(auto_of(reg, "sim-1").interval_hours, 0);

    EXPECT_EQ(code_of([&] { set_auto_snapshot(reg, "nope", AutoSnapshotConfig{true, 1}); }),
              "world_not_found");
    const std::string before = reg.get("sim-1").settings_json;
    for (const std::int64_t bad : {std::int64_t{-1}, std::int64_t{8761}}) {
        EXPECT_EQ(code_of([&] { set_auto_snapshot(reg, "sim-1", AutoSnapshotConfig{false, bad}); }),
                  "invalid_auto_snapshot") << bad;
    }
    // 出错时注册表不变。
    EXPECT_EQ(reg.get("sim-1").settings_json, before);
    EXPECT_EQ(auto_of(reg, "sim-1"), (AutoSnapshotConfig{.on_session = true, .interval_hours = 0}));
}

// 引擎池里改自动快照（PUT …/auto_snapshot）与同步默认世界设置（保存 config 之后）可能同时打到同一个
// 世界：set_auto_snapshot 必须与 update_world_settings 一样在注册表锁内读—改—写，谁都不丢谁的改动。
TEST(WorldAutoConfig, SetAutoSnapshotDoesNotLoseConcurrentSettingsMerge) {
    const TempDir tmp;
    auto reg = open_registry(tmp);
    constexpr int kRounds = 200;
    std::atomic<bool> stop{false};
    std::thread toggler([&] {
        for (int round = 0; !stop.load(); ++round) {
            set_auto_snapshot(reg, "default", AutoSnapshotConfig{round % 2 == 0, 24});
        }
    });
    for (int round = 0; round < kRounds; ++round) {
        (void)reg.update_world_settings(
            "default", R"({"gist_thresholds":{"k)" + std::to_string(round) + R"(":0.5}})");
    }
    stop.store(true);
    toggler.join();
    const auto got = settings_from_json(reg.get("default").settings_json);
    EXPECT_EQ(got.gist_thresholds.size(), static_cast<std::size_t>(kRounds));
}

// 手改 registry.db 写进越界的 interval_hours（写路径都经 merge_settings，存不进这样的值，裁定 R11）：
// 自动快照开头的 validate_world 按注册表损坏拒绝（registry_corrupt），什么都不拍。
TEST(WorldAutoConfig, OutOfRangeStoredIntervalIsRefusedByTheTrigger) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    seed_world_db(reg.get("sim-1").db_path, "sim-1", 1);
    set_world_column(reg, "sim-1", "settings_json",
                     R"({"background":{"auto_snapshot":{"on_session":true,"interval_hours":9000}}})");
    EXPECT_EQ(code_of([&] {
                  (void)take_auto_snapshot(reg, "sim-1",
                                           AutoTrigger{"interval", "", "2030-01-01T00:00:00Z"});
              }), "registry_corrupt");
    EXPECT_TRUE(reg.snapshots("sim-1").empty());
}

// 裁定 R11（恢复 SaveRefusesWhatLoadWouldReject 守的不变式）：写路径都经 merge_settings，它拒绝的值存
// 不进 registry.db；它接受的值存进去之后 validate_world 照常通过，服务启动时的 validate_registry 不会
// 因为一次设置更新而失败。边界：0、8760 放行，-1、8761 在写之前拒绝，注册表不变。
TEST(WorldAutoConfig, EveryValueMergeSettingsAcceptsPassesValidateWorld) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    seed_world_db(reg.get("sim-1").db_path, "sim-1", 1);
    for (const bool read_only : {false, true}) {
        for (const std::int64_t hours : {std::int64_t{0}, std::int64_t{8760}, std::int64_t{-1},
                                         std::int64_t{8761}}) {
            const std::string label = std::to_string(hours) + (read_only ? " read_only" : "");
            const std::string overrides =
                R"({"background":{"auto_snapshot":{"on_session":false,"interval_hours":)" +
                std::to_string(hours) + "}}}";
            const std::string before = reg.get("sim-1").settings_json;
            const std::string code = code_of([&] {
                const WorldSettings merged =
                    merge_settings(settings_from_json(before), overrides, read_only);
                reg.update_settings("sim-1", settings_to_json(merged));
            });
            if (hours == 0 || hours == 8760) {
                EXPECT_EQ(code, "<no throw>") << label;
            } else {
                EXPECT_EQ(code, "invalid_auto_snapshot") << label;
                EXPECT_EQ(reg.get("sim-1").settings_json, before) << label;
            }
            EXPECT_EQ(code_of([&] { validate_world(reg, "sim-1"); }), "<no throw>") << label;
            EXPECT_EQ(code_of([&] { validate_registry(reg); }), "<no throw>") << label;
            EXPECT_EQ(code_of([&] {
                          (void)take_auto_snapshot(reg, "sim-1",
                                                   AutoTrigger{"interval", "", "2030-01-01T00:00:00Z"});
                      }), "<no throw>") << label;
        }
    }
}

// 一个 source unit 的写入内容。observed 为空 optional 时写 NULL。
struct UnitRow {
    int engram = 0;
    std::string clause;
    std::string speaker;
    std::string session;
    std::optional<std::string> observed;
};

// 往已迁移的世界库写 source unit（engram eng-<n> 必须已存在于该租户：外键）。
void insert_units(const std::string& db_path, const std::string& tenant,
                  const std::vector<UnitRow>& rows) {
    auto adapter = persistence::SqliteAdapter::open(db_path);
    for (const auto& row : rows) {
        persistence::Stmt ins(adapter->connection().raw(),
            "INSERT INTO source_unit_meta(tenant_id,engram_ref,clause_id,speaker,session_id,"
            "audience_json,observed_at) VALUES(?1,?2,?3,?4,?5,'[]',?6)");
        const std::string ref = "eng-" + std::to_string(row.engram);
        if (row.observed) {
            ins.bind(tenant, ref, row.clause, row.speaker, row.session, *row.observed);
        } else {
            ins.bind(tenant, ref, row.clause, row.speaker, row.session, nullptr);
        }
        ins.step_done();
    }
}

// 往已迁移的世界库写一条最小的 statement（holder_id 不可原地改，所以由参数给出）。
// NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
void insert_statement(const std::string& db_path, const std::string& tenant,
                      const std::string& stmt_id, const std::string& holder = "self",
                      const std::string& created_at = "2026-10-28T00:00:00Z") {
    auto adapter = persistence::SqliteAdapter::open(db_path);
    persistence::Stmt ins(adapter->connection().raw(),
        "INSERT INTO statements(id,tenant_id,holder_id,holder_perspective,subject_kind,"
        "subject_id,predicate,object_kind,object_value,canonical_object_hash,"
        "canonical_object_hash_version,modality,polarity,confidence,observed_at,salience,"
        "affect_json,activation,last_accessed,provenance,created_at,updated_at) "
        "VALUES(?1,?2,?4,'first_person','entity','thing','likes','str','x',?3,'v1',"
        "'believes','pos',0.5,'2026-10-28T00:00:00Z',0.5,'{}',0.0,'2026-10-28T00:00:00Z',"
        "'user_input',?5,'2026-10-28T00:00:00Z')");
    ins.bind(stmt_id, tenant, std::string(64, 'a'), holder, created_at);
    ins.step_done();
}

// sim-1：3 行 engram、2 条 statement、2 个会话 3 个 unit；另一个租户各有一行（rowid 更大），
// 用来证明标记与会话数都按租户过滤。新库里 rowid 从 1 顺序分配。
void seed_marked_world(const std::string& db_path) {
    seed_world_db(db_path, "sim-1", 3);   // eng-0..2，rowid 1..3
    {
        auto adapter = persistence::SqliteAdapter::open(db_path);
        insert_engram(adapter->connection().raw(), "other", 100);   // rowid 4
    }
    insert_statement(db_path, "sim-1", "st-1");
    insert_statement(db_path, "sim-1", "st-2");
    insert_statement(db_path, "other", "st-x");
    insert_units(db_path, "sim-1", {{0, "c0", "spk-1", "s-a", "2030-01-01T00:00:00Z"},
                                    {0, "c1", "spk-2", "s-a", "2030-01-01T00:01:00Z"},
                                    {1, "c0", "spk-1", "s-b", "2030-01-01T00:02:00Z"}});
    insert_units(db_path, "other", {{100, "c0", "spk-9", "s-z", "2030-01-01T00:00:00Z"}});
}

// 引擎池的统计缓存（裁定 R18）：holders = 该租户 statements 的不同 holder 数，last_write_at =
// 该租户 statements 的 MAX(created_at)；没有 statements（或没有表、没有库文件）时 0 与空串。
TEST(WorldStats, HoldersAndLastWriteAtByTenant) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto world = reg.get("sim-1");
    const auto missing = world_stats(world.db_path, "sim-1");
    EXPECT_EQ(missing.holders, 0);
    EXPECT_EQ(missing.last_write_at, "");
    seed_world_db(world.db_path, "sim-1", 1);   // 有库、没有 statements
    const auto bare = world_stats(world.db_path, "sim-1");
    EXPECT_EQ(bare.holders, 0);
    EXPECT_EQ(bare.last_write_at, "");
    insert_statement(world.db_path, "sim-1", "st-1");
    insert_statement(world.db_path, "sim-1", "st-2", "bob", "2026-10-29T00:00:00Z");
    insert_statement(world.db_path, "sim-1", "st-3", "bob", "2026-10-27T00:00:00Z");
    insert_statement(world.db_path, "other", "st-x", "zed", "2031-01-01T00:00:00Z");
    const auto stats = world_stats(world.db_path, "sim-1");
    EXPECT_EQ(stats.statements, 3);
    EXPECT_EQ(stats.holders, 2);                             // self、bob；别的租户的 zed 不算
    EXPECT_EQ(stats.last_write_at, "2026-10-29T00:00:00Z");  // 别的租户更晚的那条不算
    const auto legacy_db = (tmp.path() / "legacy.db").string();
    exec_sql_unmigrated(legacy_db, "CREATE TABLE engrams(id TEXT, tenant_id TEXT, created_at TEXT);");
    const auto legacy = world_stats(legacy_db, "t1");       // 没有 statements 表
    EXPECT_EQ(legacy.holders, 0);
    EXPECT_EQ(legacy.last_write_at, "");
}

TEST(WorldSnapshotMeta, TakeSnapshotRecordsSessionsCompleteAndContentMark) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto world = reg.get("sim-1");
    seed_marked_world(world.db_path);
    SnapshotOptions opts;
    opts.session_id = "s-c";
    const auto snap = take_snapshot(reg, "sim-1", opts);
    ASSERT_TRUE(snap.sessions_complete.has_value());
    EXPECT_EQ(*snap.sessions_complete, 2);
    EXPECT_EQ(snap.content_mark, "e3:3:eng-2|s2:2:st-2|u3:3:eng-1:c0");
    EXPECT_EQ(snap.session_id, "s-c");   // 原样写进条目
    // 与库内容一致：快照文件里按租户数出来的就是这些。
    EXPECT_EQ(count_rows(snap.path, "SELECT COUNT(*) FROM engrams WHERE tenant_id='sim-1'"), 3);
    EXPECT_EQ(count_rows(snap.path,
                         "SELECT COUNT(DISTINCT session_id) FROM source_unit_meta "
                         "WHERE tenant_id='sim-1'"), 2);
    // 三个新字段写进注册表，重开之后读回来不变。
    const auto back = RegistryDb::open(tmp.path(), tmp.path() / "dashboard.db", "default");
    const auto got = back.snapshots("sim-1").front();
    EXPECT_EQ(got.sessions_complete, std::optional<std::int64_t>{2});
    EXPECT_EQ(got.content_mark, "e3:3:eng-2|s2:2:st-2|u3:3:eng-1:c0");
    EXPECT_EQ(got.session_id, "s-c");
}

// sessions_complete 为 0 也要写出来（与「W4 之前的条目」的 NULL 区分开）。没有内容的库标记为空串。
TEST(WorldSnapshotMeta, ZeroSessionsIsWrittenAndEmptyStringsAreOmitted) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    { auto adapter = persistence::SqliteAdapter::open(reg.get("sim-1").db_path); }
    const auto snap = take_snapshot(reg, "sim-1", SnapshotOptions{});
    EXPECT_EQ(snap.sessions_complete, std::optional<std::int64_t>{0});
    EXPECT_EQ(snap.content_mark, "");
    const auto back = RegistryDb::open(tmp.path(), tmp.path() / "dashboard.db", "default");
    const auto got = back.snapshots("sim-1").front();
    EXPECT_EQ(got.sessions_complete, std::optional<std::int64_t>{0});
    EXPECT_EQ(got.content_mark, "");
    EXPECT_EQ(got.session_id, "");
}

// 迁移停在 36 的库：没有的表那一段写 "-"，会话数记 0。
TEST(WorldSnapshotMeta, MissingTablesBecomeDashSegments) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    exec_sql_unmigrated(reg.get("sim-1").db_path,
             "CREATE TABLE engrams(id TEXT, tenant_id TEXT, created_at TEXT);"
             "INSERT INTO engrams VALUES('a','sim-1','2026-01-01T00:00:00Z'),"
             "('b','other','2026-01-01T00:00:00Z'),('c','sim-1','2026-01-02T00:00:00Z');");
    const auto snap = take_snapshot(reg, "sim-1", SnapshotOptions{});
    EXPECT_EQ(snap.content_mark, "e2:3:c|-|-");
    EXPECT_EQ(snap.sessions_complete, std::optional<std::int64_t>{0});
}

// 测试自带的标记（与 world_store 的 content_mark_on 独立实现，照头文件第 2a 步的格式）：给 I-2 的
// 测试当对照，证明条目上的标记就是快照文件里的内容。
std::string mark_of(const std::string& db_path, const std::string& tenant) {
    struct Segment {
        const char* table;
        const char* id_expr;
        char tag;
    };
    const std::array<Segment, 3> segments{{{"engrams", "id", 'e'},
                                           {"statements", "id", 's'},
                                           {"source_unit_meta", "engram_ref || ':' || clause_id", 'u'}}};
    std::string mark;
    bool has_rows = false;
    for (const auto& seg : segments) {
        if (!mark.empty()) { mark += '|'; }
        const std::string where = std::string(" FROM ") + seg.table + " WHERE tenant_id='" + tenant + "'";
        const auto rows = count_rows(db_path, ("SELECT COUNT(*)" + where).c_str());
        const auto top = count_rows(db_path, ("SELECT COALESCE(MAX(rowid),0)" + where).c_str());
        std::string top_id;
        if (rows > 0) {
            sqlite3* raw = nullptr;
            EXPECT_EQ(sqlite3_open_v2(("file:" + db_path + "?mode=ro").c_str(), &raw,
                                      SQLITE_OPEN_READONLY | SQLITE_OPEN_URI, nullptr), SQLITE_OK);
            const persistence::SqliteHandle handle(raw);
            const std::string sql = std::string("SELECT ") + seg.id_expr + " FROM " + seg.table +
                                    " WHERE rowid=" + std::to_string(top);
            persistence::Stmt query(raw, sql.c_str());
            EXPECT_TRUE(query.step_row());
            top_id = reinterpret_cast<const char*>(sqlite3_column_text(query.raw(), 0));
        }
        has_rows = has_rows || rows > 0;
        mark += seg.tag + std::to_string(rows) + ":" + std::to_string(top) + ":" + top_id;
    }
    return has_rows ? mark : std::string{};
}

std::int64_t sessions_in(const std::string& db_path) {
    return count_rows(db_path,
                      "SELECT COUNT(DISTINCT session_id) FROM source_unit_meta WHERE tenant_id='sim-1'");
}

// Codex I-2：标记与会话数在拍出来的快照文件上算，恰好描述快照里有什么。这里 VACUUM 之前插进来
// 一行 engram 与一个新会话的 unit：它们进了快照，也就进了标记（与源库在钩子之前的标记不同）。
// 下一轮按时间检查看到的当前标记与快照的标记相同 → unchanged，不会再为同样的内容多拍一份。
TEST(WorldSnapshotMeta, MarkDescribesTheCopyWhenARowLandsBeforeVacuum) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    set_auto_snapshot(reg, "sim-1", AutoSnapshotConfig{false, 24});
    const std::string db_path = reg.get("sim-1").db_path;
    seed_marked_world(db_path);
    const std::string before_hook = mark_of(db_path, "sim-1");
    EXPECT_EQ(before_hook, "e3:3:eng-2|s2:2:st-2|u3:3:eng-1:c0");
    int fired = 0;
    SnapshotOptions base;
    base.before_vacuum = [&] {
        if (fired++ > 0) { return; }
        append_engrams(db_path, "sim-1", 3, 1);   // rowid 5（4 是 other 的）
        insert_units(db_path, "sim-1", {{3, "c0", "spk-2", "s-c", "2030-01-01T00:05:00Z"}});
    };
    const auto first = take_auto_snapshot(reg, "sim-1",
                                          AutoTrigger{"interval", "", "2030-01-01T00:00:00Z"}, base);
    ASSERT_TRUE(first.taken.has_value()) << first.skipped;
    EXPECT_EQ(fired, 1);
    const std::string copy = first.taken->path;
    EXPECT_EQ(first.taken->content_mark, mark_of(copy, "sim-1"));
    EXPECT_EQ(first.taken->content_mark, "e4:5:eng-3|s2:2:st-2|u4:5:eng-3:c0");
    EXPECT_NE(first.taken->content_mark, before_hook);
    EXPECT_EQ(first.taken->sessions_complete, std::optional<std::int64_t>{sessions_in(copy)});
    EXPECT_EQ(first.taken->sessions_complete, std::optional<std::int64_t>{3});

    const auto second = take_auto_snapshot(reg, "sim-1",
                                           AutoTrigger{"interval", "", "2030-01-02T01:00:00Z"}, base);
    EXPECT_FALSE(second.taken.has_value());
    EXPECT_EQ(second.skipped, "unchanged");
}

// Codex I-2：标记与复制之间提交的删除（forget、clear_memory）。旧实现在源库上、VACUUM 之前算标记，
// 快照因此少于标记：标记声称拍下了被删的行。现在标记在快照文件上算，删掉的行两边都没有。
// 删的是 s-b 的唯一一个 unit 与没有被引用的 eng-2：e、u 两段与会话数都要跟着快照变。
TEST(WorldSnapshotMeta, MarkDescribesTheCopyWhenARowIsDeletedBeforeVacuum) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    set_auto_snapshot(reg, "sim-1", AutoSnapshotConfig{false, 24});
    const std::string db_path = reg.get("sim-1").db_path;
    seed_marked_world(db_path);
    EXPECT_EQ(sessions_in(db_path), 2);
    int fired = 0;
    SnapshotOptions base;
    base.before_vacuum = [&] {
        if (fired++ > 0) { return; }
        exec_sql(db_path, "DELETE FROM source_unit_meta WHERE tenant_id='sim-1' AND session_id='s-b';"
                          "DELETE FROM engrams WHERE tenant_id='sim-1' AND id='eng-2';");
    };
    const auto taken = take_auto_snapshot(reg, "sim-1",
                                          AutoTrigger{"interval", "", "2030-01-01T00:00:00Z"}, base);
    ASSERT_TRUE(taken.taken.has_value()) << taken.skipped;
    EXPECT_EQ(fired, 1);
    const std::string copy = taken.taken->path;
    EXPECT_EQ(count_rows(copy, "SELECT COUNT(*) FROM engrams WHERE id='eng-2'"), 0);
    EXPECT_EQ(taken.taken->content_mark, mark_of(copy, "sim-1"));
    EXPECT_EQ(taken.taken->content_mark, "e2:2:eng-1|s2:2:st-2|u2:2:eng-0:c1");
    // 会话数也是快照里的：s-b 的最后一个 unit 删了，快照里只剩 s-a。
    EXPECT_EQ(taken.taken->sessions_complete, std::optional<std::int64_t>{sessions_in(copy)});
    EXPECT_EQ(taken.taken->sessions_complete, std::optional<std::int64_t>{1});
}

// Codex I-2（按会话）：按会话的快照同样在快照文件上数会话。VACUUM 之前删掉一个会话的最后一个
// unit，sessions_complete 数的是快照里剩下的会话；标记也与快照文件一致，所以同一会话重试时
// 当前标记与它相同 → unchanged（I-1 的去重照常成立）。
TEST(WorldSnapshotMeta, SessionCountDescribesTheCopyWhenASessionVanishesBeforeVacuum) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");   // 新世界默认按会话开
    const std::string db_path = reg.get("sim-1").db_path;
    seed_marked_world(db_path);
    int fired = 0;
    SnapshotOptions base;
    base.before_vacuum = [&] {
        if (fired++ > 0) { return; }
        exec_sql(db_path, "DELETE FROM source_unit_meta WHERE tenant_id='sim-1' AND session_id='s-b';");
    };
    const auto taken = take_auto_snapshot(reg, "sim-1",
                                          AutoTrigger{"session", "s-new", "2030-01-01T00:00:00Z"}, base);
    ASSERT_TRUE(taken.taken.has_value()) << taken.skipped;
    const std::string copy = taken.taken->path;
    EXPECT_EQ(sessions_in(copy), 1);
    EXPECT_EQ(taken.taken->sessions_complete, std::optional<std::int64_t>{1});
    EXPECT_EQ(taken.taken->content_mark, mark_of(copy, "sim-1"));

    const auto retry = take_auto_snapshot(reg, "sim-1",
                                          AutoTrigger{"session", "s-new", "2030-01-01T00:00:05Z"}, base);
    EXPECT_FALSE(retry.taken.has_value());
    EXPECT_EQ(retry.skipped, "unchanged");
}

// 一张表按 rowid 排的 "rowid=id" 列表（只看 sim-1），用来比对源库与快照的 rowid → id。
std::string rowid_map(const std::string& db_path, const std::string& table, const std::string& id_expr) {
    sqlite3* raw = nullptr;
    EXPECT_EQ(sqlite3_open_v2(("file:" + db_path + "?mode=ro").c_str(), &raw,
                              SQLITE_OPEN_READONLY | SQLITE_OPEN_URI, nullptr), SQLITE_OK);
    const persistence::SqliteHandle handle(raw);
    const std::string sql = "SELECT rowid, " + id_expr + " FROM " + table +
                            " WHERE tenant_id='sim-1' ORDER BY rowid";
    persistence::Stmt query(raw, sql.c_str());
    std::string out;
    while (query.step_row()) {
        out += std::to_string(sqlite3_column_int64(query.raw(), 0)) + "=" +
               reinterpret_cast<const char*>(sqlite3_column_text(query.raw(), 1)) + ";";
    }
    return out;
}

// 契约钉测（Codex r2 R2-1）：标记里有 max_rowid，「当前标记（源库上算）== 快照标记」这一比较依赖
// VACUUM INTO 保留 rowid。SQLite 文档并不承诺这一点：没有 INTEGER PRIMARY KEY 的表（engrams、
// statements、source_unit_meta 都是）VACUUM 可以重排 rowid；当前 SQLite（3.51.3）实测保留，生产副本上
// 三张表 rowid → id 也完全一致。这里用真实迁移的 schema，在三张表里各删掉一行**中间**的行（max rowid
// 之下留出空洞，重排就会让 max_rowid 变小），再走真实的 take_auto_snapshot：快照标记必须等于源库当前
// 标记，按时间到期后与按会话重试都得是 unchanged。将来哪个 SQLite 在 VACUUM INTO 里重排 rowid，这条
// 测试就会失败，届时标记要换成与 rowid 无关的形式。失败方向是安全的：误判 changed → 多拍（受保留
// 份数封顶），不会漏拍。没有回退验证：没法让 SQLite 重排 rowid。
TEST(WorldSnapshotMeta, VacuumIntoKeepsRowidsAcrossAMiddleGapContract) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    set_auto_snapshot(reg, "sim-1", AutoSnapshotConfig{true, 24});
    const std::string db_path = reg.get("sim-1").db_path;
    seed_marked_world(db_path);   // engrams sim-1 rowid 1..3（other 4）；statements 1..2（other 3）；
                                  // units sim-1 1..3（other 4）
    append_engrams(db_path, "sim-1", 3, 2);   // eng-3、eng-4：rowid 5、6
    insert_statement(db_path, "sim-1", "st-3");   // rowid 4
    insert_statement(db_path, "sim-1", "st-4");   // rowid 5
    insert_units(db_path, "sim-1", {{3, "c0", "spk-1", "s-c", "2030-01-01T00:03:00Z"},    // rowid 5
                                    {4, "c0", "spk-2", "s-c", "2030-01-01T00:04:00Z"}});  // rowid 6
    // 各删一行中间的：engrams 的 eng-1（rowid 2，先删引用它的 unit，外键）、statements 的 st-2（rowid 2）、
    // source_unit_meta 的 eng-0:c1（rowid 2）。max rowid 都不变，之下留出空洞。
    exec_sql(db_path,
             "DELETE FROM source_unit_meta WHERE tenant_id='sim-1' AND engram_ref='eng-1';"
             "DELETE FROM source_unit_meta WHERE tenant_id='sim-1' AND engram_ref='eng-0' AND clause_id='c1';"
             "DELETE FROM engrams WHERE tenant_id='sim-1' AND id='eng-1';"
             "DELETE FROM statements WHERE tenant_id='sim-1' AND id='st-2';");
    const std::string source_mark = mark_of(db_path, "sim-1");
    ASSERT_EQ(source_mark, "e4:6:eng-4|s3:5:st-4|u3:6:eng-4:c0");
    ASSERT_EQ(rowid_map(db_path, "engrams", "id"), "1=eng-0;3=eng-2;5=eng-3;6=eng-4;");

    const auto first = take_auto_snapshot(reg, "sim-1",
                                          AutoTrigger{"interval", "", "2030-01-01T00:00:00Z"},
                                          SnapshotOptions{});
    ASSERT_TRUE(first.taken.has_value()) << first.skipped;
    const std::string copy = first.taken->path;
    EXPECT_EQ(first.taken->content_mark, source_mark);
    for (const auto& [table, id_expr] : std::vector<std::pair<std::string, std::string>>{
             {"engrams", "id"}, {"statements", "id"},
             {"source_unit_meta", "engram_ref || ':' || clause_id"}}) {
        EXPECT_EQ(rowid_map(copy, table, id_expr), rowid_map(db_path, table, id_expr)) << table;
    }

    // 按时间：间隔早已到了（48 小时），内容没变 → unchanged（比较走的是 world_store 自己的当前标记）。
    const auto later = take_auto_snapshot(reg, "sim-1",
                                          AutoTrigger{"interval", "", "2030-01-03T00:00:00Z"},
                                          SnapshotOptions{});
    EXPECT_FALSE(later.taken.has_value());
    EXPECT_EQ(later.skipped, "unchanged");

    // 按会话：新会话拍一份，同一会话重试（写入没发生）→ unchanged（I-1 去重）。
    const auto session = take_auto_snapshot(reg, "sim-1",
                                            AutoTrigger{"session", "s-new", "2030-01-03T00:00:01Z"},
                                            SnapshotOptions{});
    ASSERT_TRUE(session.taken.has_value()) << session.skipped;
    EXPECT_EQ(session.taken->content_mark, source_mark);
    const auto retry = take_auto_snapshot(reg, "sim-1",
                                          AutoTrigger{"session", "s-new", "2030-01-03T00:00:05Z"},
                                          SnapshotOptions{});
    EXPECT_FALSE(retry.taken.has_value());
    EXPECT_EQ(retry.skipped, "unchanged");
    EXPECT_EQ(reg.snapshots("sim-1").size(), 2U);
}

// R2：注入的时钟（SnapshotOptions::now_iso）同时决定快照 id 的时间前缀与 created_at，两者是同一秒。
// take_auto_snapshot 把触发的 now_iso 传下去。不注入时照旧用真实时钟。
TEST(WorldSnapshotMeta, InjectedNowDrivesBothTheIdPrefixAndCreatedAt) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    seed_world_db(reg.get("sim-1").db_path, "sim-1", 1);
    SnapshotOptions opts;
    opts.now_iso = "2030-01-03T04:05:06Z";
    const auto first = take_snapshot(reg, "sim-1", opts);
    EXPECT_EQ(first.created_at, "2030-01-03T04:05:06Z");
    EXPECT_EQ(first.id.substr(0, 17), "20300103T040506Z-") << first.id;
    EXPECT_EQ(first.id.size(), 21U);
    EXPECT_TRUE(fs::exists(first.path));
    // 同一秒再拍：时间前缀相同，只重抽十六进制部分，id 不重复。
    const auto second = take_snapshot(reg, "sim-1", opts);
    EXPECT_EQ(second.id.substr(0, 17), "20300103T040506Z-") << second.id;
    EXPECT_NE(second.id, first.id);

    const auto fresh = take_auto_snapshot(reg, "sim-1",
                                          AutoTrigger{"session", "s-new", "2031-02-03T10:20:30Z"},
                                          SnapshotOptions{});
    ASSERT_TRUE(fresh.taken.has_value()) << fresh.skipped;
    EXPECT_EQ(fresh.taken->created_at, "2031-02-03T10:20:30Z");
    EXPECT_EQ(fresh.taken->id.substr(0, 17), "20310203T102030Z-") << fresh.taken->id;

    const auto real = take_snapshot(reg, "sim-1", SnapshotOptions{});
    EXPECT_EQ(real.created_at.size(), 20U);   // 2026-10-28T01:12:00Z
    EXPECT_EQ(real.id.size(), 21U);           // 20261028T011200Z-a1b2
    EXPECT_NE(real.id.substr(0, 4), "2030");
}

// R2：SnapshotOptions::now_iso 形状不对抛 invalid_auto_snapshot，在任何 I/O 之前（证明方式同
// RejectsMismatchedKindAndReasonBeforeAnyIo）。Python isoformat() 的微秒与 +00:00 写法都不收。
TEST(WorldSnapshotMeta, MalformedInjectedNowIsRejectedBeforeAnyIo) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    seed_world_db(reg.get("sim-1").db_path, "sim-1", 1);
    int probed = 0;
    for (const char* bad : {"2030-01-01 00:00:00Z", "2030-01-01T00:00:00", "2030-02-30T00:00:00Z",
                            "2030-01-01T00:00:00.123456Z", "2030-01-01T00:00:00+00:00", "now"}) {
        SnapshotOptions opts;
        opts.now_iso = bad;
        opts.free_space = [&probed](const fs::path&) -> std::int64_t {
            ++probed;
            return std::numeric_limits<std::int64_t>::max();
        };
        for (const char* world : {"sim-1", "default"}) {
            EXPECT_EQ(code_of([&] { (void)take_snapshot(reg, world, opts); }),
                      "invalid_auto_snapshot") << world << " now='" << bad << "'";
        }
    }
    EXPECT_EQ(probed, 0);
    EXPECT_TRUE(fs::is_empty(reg.get("sim-1").snapshot_dir));
    EXPECT_FALSE(fs::exists(reg.get("default").snapshot_dir));
    EXPECT_TRUE(reg.snapshots("sim-1").empty());
    EXPECT_TRUE(reg.snapshots("default").empty());
}

// ---- 按会话 ----

TEST(WorldAutoSnapshot, SessionTakesBeforeANewSessionAndSkipsAKnownOne) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");   // 新世界默认按会话开
    const std::string db_path = reg.get("sim-1").db_path;
    seed_world_db(db_path, "sim-1", 2);
    insert_units(db_path, "sim-1", {{0, "c0", "spk-1", "s-old", "2030-01-01T00:00:00Z"}});

    const auto fresh = take_auto_snapshot(reg, "sim-1",
                                          AutoTrigger{"session", "s-new", "2030-01-03T04:05:06Z"},
                                          SnapshotOptions{});
    ASSERT_TRUE(fresh.taken.has_value()) << fresh.skipped;
    EXPECT_EQ(fresh.skipped, "");
    EXPECT_EQ(fresh.taken->kind, "auto");
    EXPECT_EQ(fresh.taken->reason, "session");
    EXPECT_EQ(fresh.taken->session_id, "s-new");
    EXPECT_EQ(fresh.taken->created_at, "2030-01-03T04:05:06Z");   // 触发的时钟
    EXPECT_EQ(fresh.taken->sessions_complete, std::optional<std::int64_t>{1});
    EXPECT_TRUE(fs::exists(fresh.taken->path));
    ASSERT_EQ(reg.snapshots("sim-1").size(), 1U);
    EXPECT_EQ(reg.snapshots("sim-1").front().id, fresh.taken->id);

    const auto known = take_auto_snapshot(reg, "sim-1",
                                          AutoTrigger{"session", "s-old", "2030-01-03T04:05:07Z"},
                                          SnapshotOptions{});
    EXPECT_FALSE(known.taken.has_value());
    EXPECT_EQ(known.skipped, "known_session");
    EXPECT_EQ(reg.snapshots("sim-1").size(), 1U);

    // 会话写进库之后，同一个会话再触发就是已知的。
    insert_units(db_path, "sim-1", {{1, "c0", "spk-1", "s-new", "2030-01-03T04:05:06Z"}});
    EXPECT_EQ(take_auto_snapshot(reg, "sim-1",
                                 AutoTrigger{"session", "s-new", "2030-01-03T04:05:08Z"},
                                 SnapshotOptions{}).skipped,
              "known_session");
    EXPECT_EQ(reg.snapshots("sim-1").size(), 1U);
}

// 「库里已经有这个会话」按租户判断：别的租户的同名会话不算。
TEST(WorldAutoSnapshot, SessionKnownIsPerTenant) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const std::string db_path = reg.get("sim-1").db_path;
    seed_world_db(db_path, "sim-1", 1);
    {
        auto adapter = persistence::SqliteAdapter::open(db_path);
        insert_engram(adapter->connection().raw(), "other", 100);
    }
    insert_units(db_path, "other", {{100, "c0", "spk-9", "s-x", "2030-01-01T00:00:00Z"}});
    const auto res = take_auto_snapshot(reg, "sim-1",
                                        AutoTrigger{"session", "s-x", "2030-01-01T00:00:00Z"},
                                        SnapshotOptions{});
    EXPECT_TRUE(res.taken.has_value()) << res.skipped;
}

TEST(WorldAutoSnapshot, SessionDisabledAndMissingSessionId) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    seed_world_db(reg.get("sim-1").db_path, "sim-1", 1);
    // on_session=true 时 session_id 为空是调用方的错。
    EXPECT_EQ(code_of([&] {
        (void)take_auto_snapshot(reg, "sim-1", AutoTrigger{"session", "", "2030-01-01T00:00:00Z"},
                                 SnapshotOptions{});
    }), "invalid_auto_snapshot");
    set_auto_snapshot(reg, "sim-1", AutoSnapshotConfig{false, 24});
    const auto off = take_auto_snapshot(reg, "sim-1",
                                        AutoTrigger{"session", "s-new", "2030-01-01T00:00:00Z"},
                                        SnapshotOptions{});
    EXPECT_FALSE(off.taken.has_value());
    EXPECT_EQ(off.skipped, "disabled");
    // 关着的时候先报 disabled（不检查 session_id）。
    EXPECT_EQ(take_auto_snapshot(reg, "sim-1", AutoTrigger{"session", "", "2030-01-01T00:00:00Z"},
                                 SnapshotOptions{}).skipped,
              "disabled");
    EXPECT_TRUE(fs::is_empty(reg.get("sim-1").snapshot_dir));
    EXPECT_TRUE(reg.snapshots("sim-1").empty());
}

// 生产库迁移停在 36，没有 source_unit_meta：表不存在算「没有这个会话」，照常拍。
TEST(WorldAutoSnapshot, SessionOnDbWithoutSourceUnitMetaTable) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    exec_sql_unmigrated(reg.get("sim-1").db_path,
             "CREATE TABLE engrams(id TEXT, tenant_id TEXT, created_at TEXT);"
             "INSERT INTO engrams VALUES('a','sim-1','2026-01-01T00:00:00Z');");
    const auto res = take_auto_snapshot(reg, "sim-1",
                                        AutoTrigger{"session", "s-1", "2030-01-01T00:00:00Z"},
                                        SnapshotOptions{});
    ASSERT_TRUE(res.taken.has_value()) << res.skipped;
    EXPECT_EQ(res.taken->sessions_complete, std::optional<std::int64_t>{0});
    EXPECT_EQ(res.taken->content_mark, "e1:1:a|-|-");
}

// I-1 的几个用例共用：按会话触发 session，时间为 now。
AutoSnapshotResult session_at(RegistryDb& reg, const std::string& session, const std::string& now) {
    return take_auto_snapshot(reg, "sim-1", AutoTrigger{"session", session, now},
                              SnapshotOptions{});
}

// I-1：调用方在写入之前触发，写入反复失败（契约拒绝、策略拒绝、SQLite 错误）时会话一直是新的。
// 同一会话已有内容相同的按会话快照就返回 unchanged：否则每次重试（engine 最多 5 次）一份，
// 保留策略就把更早会话的锚点挤掉了。别的会话不受影响。
TEST(WorldAutoSnapshot, SessionRetryWithoutAWriteDoesNotSnapshotAgain) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const std::string db_path = reg.get("sim-1").db_path;
    seed_world_db(db_path, "sim-1", 2);
    insert_units(db_path, "sim-1", {{0, "c0", "spk-1", "s-old", "2030-01-01T00:00:00Z"}});
    const auto first = session_at(reg, "s-new", "2030-01-02T00:00:00Z");
    ASSERT_TRUE(first.taken.has_value()) << first.skipped;
    for (int retry = 1; retry <= 5; ++retry) {
        const auto again = session_at(reg, "s-new",
                                      "2030-01-02T00:00:0" + std::to_string(retry) + "Z");
        EXPECT_FALSE(again.taken.has_value()) << "retry " << retry;
        EXPECT_EQ(again.skipped, "unchanged") << "retry " << retry;
    }
    ASSERT_EQ(reg.snapshots("sim-1").size(), 1U);
    EXPECT_EQ(reg.snapshots("sim-1").front().id, first.taken->id);
    // 规则只认同一个 session_id：另一个新会话照常拍。
    const auto other = session_at(reg, "s-other", "2030-01-02T00:01:00Z");
    ASSERT_TRUE(other.taken.has_value()) << other.skipped;
    EXPECT_EQ(other.taken->session_id, "s-other");
    EXPECT_EQ(reg.snapshots("sim-1").size(), 2U);
}

// I-1：两个空标记也算相同。新世界的库已建好、还没有内容，首个会话反复失败也只拍一份空库。
TEST(WorldAutoSnapshot, SessionRetryOnAnEmptyWorldDoesNotSnapshotAgain) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    { auto adapter = persistence::SqliteAdapter::open(reg.get("sim-1").db_path); }
    const auto first = session_at(reg, "s-1", "2030-01-01T00:00:00Z");
    ASSERT_TRUE(first.taken.has_value()) << first.skipped;
    EXPECT_EQ(first.taken->content_mark, "");
    EXPECT_EQ(session_at(reg, "s-1", "2030-01-01T00:00:01Z").skipped, "unchanged");
    EXPECT_EQ(reg.snapshots("sim-1").size(), 1U);
}

// I-1：两次触发之间有别的写入（另一个会话的 unit、一行 engram），标记变了，再拍一份：
// 那份快照里没有这些写入，回滚到它会丢东西。
TEST(WorldAutoSnapshot, SessionRetryAfterAnUnrelatedWriteSnapshotsAgain) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    set_retention(reg, 10);
    const std::string db_path = reg.get("sim-1").db_path;
    seed_world_db(db_path, "sim-1", 3);
    insert_units(db_path, "sim-1", {{0, "c0", "spk-1", "s-old", "2030-01-01T00:00:00Z"}});
    const auto first = session_at(reg, "s-new", "2030-01-02T00:00:00Z");
    ASSERT_TRUE(first.taken.has_value()) << first.skipped;

    insert_units(db_path, "sim-1", {{1, "c0", "spk-2", "s-other", "2030-01-02T00:00:30Z"}});
    const auto after_unit = session_at(reg, "s-new", "2030-01-02T00:01:00Z");
    ASSERT_TRUE(after_unit.taken.has_value()) << after_unit.skipped;
    EXPECT_NE(after_unit.taken->content_mark, first.taken->content_mark);

    append_engrams(db_path, "sim-1", 3, 1);
    const auto after_engram = session_at(reg, "s-new", "2030-01-02T00:02:00Z");
    ASSERT_TRUE(after_engram.taken.has_value()) << after_engram.skipped;
    EXPECT_NE(after_engram.taken->content_mark, after_unit.taken->content_mark);

    const auto list = reg.snapshots("sim-1");
    ASSERT_EQ(list.size(), 3U);
    for (const auto& snap : list) {
        EXPECT_EQ(snap.reason, "session") << snap.id;
        EXPECT_EQ(snap.session_id, "s-new") << snap.id;
    }
    // 之后没有新写入：再触发是 unchanged。
    EXPECT_EQ(session_at(reg, "s-new", "2030-01-02T00:03:00Z").skipped, "unchanged");
    EXPECT_EQ(reg.snapshots("sim-1").size(), 3U);
}

// I-1：匹配任意一份按会话快照，不只看最新的 auto。两次重试之间落下一份按时间的快照、成了
// created_at 最新的 auto，也不重复拍。（这里用 take_snapshot 直接记一份 auto/interval：内容没变时
// 按时间触发自己会判 unchanged，这一步只为让它成为最新的 auto。）同一标记的按时间快照、带同一
// session_id 的 manual 快照都不算按会话的快照：在它们之后第一次触发照常拍。
TEST(WorldAutoSnapshot, SessionRetryAfterAnIntervalSnapshotStillDoesNotDuplicate) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    set_retention(reg, 10);
    set_auto_snapshot(reg, "sim-1", AutoSnapshotConfig{true, 24});
    const std::string db_path = reg.get("sim-1").db_path;
    seed_world_db(db_path, "sim-1", 2);
    {
        SnapshotOptions interval;
        interval.kind = "auto";
        interval.reason = "interval";
        interval.session_id = "s-new";
        interval.now_iso = "2030-01-01T00:00:00Z";
        (void)take_snapshot(reg, "sim-1", interval);
        SnapshotOptions manual;
        manual.session_id = "s-new";
        manual.now_iso = "2030-01-01T00:10:00Z";
        (void)take_snapshot(reg, "sim-1", manual);
    }
    const auto first = session_at(reg, "s-new", "2030-01-01T01:00:00Z");
    ASSERT_TRUE(first.taken.has_value()) << first.skipped;

    SnapshotOptions between;
    between.kind = "auto";
    between.reason = "interval";
    between.now_iso = "2030-01-02T02:00:00Z";
    const auto newest_auto = take_snapshot(reg, "sim-1", between);
    EXPECT_EQ(newest_auto.content_mark, first.taken->content_mark);

    const auto retry = session_at(reg, "s-new", "2030-01-02T03:00:00Z");
    EXPECT_FALSE(retry.taken.has_value());
    EXPECT_EQ(retry.skipped, "unchanged");
    const auto list = reg.snapshots("sim-1");
    EXPECT_EQ(std::ranges::count_if(list, [](const SnapshotEntry& snap) {
                  return snap.kind == "auto" && snap.reason == "session";
              }), 1);
    EXPECT_EQ(list.size(), 4U);
}

// ---- 按时间 ----

// sim-1 改成按时间每 24 小时、按会话关，库里有 3 行 engram。
RegistryDb interval_world(const TempDir& tmp) {
    auto reg = registry_with_world(tmp, "sim-1");
    set_auto_snapshot(reg, "sim-1", AutoSnapshotConfig{false, 24});
    seed_world_db(reg.get("sim-1").db_path, "sim-1", 3);
    return reg;
}

AutoSnapshotResult interval_at(RegistryDb& reg, const std::string& now) {
    return take_auto_snapshot(reg, "sim-1", AutoTrigger{"interval", "", now}, SnapshotOptions{});
}

TEST(WorldAutoSnapshot, IntervalTakesFirstWhenNoAutoSnapshotExists) {
    const TempDir tmp;
    auto reg = interval_world(tmp);
    const auto res = interval_at(reg, "2030-01-01T00:00:00Z");
    ASSERT_TRUE(res.taken.has_value()) << res.skipped;
    EXPECT_EQ(res.skipped, "");
    EXPECT_EQ(res.taken->kind, "auto");
    EXPECT_EQ(res.taken->reason, "interval");
    EXPECT_EQ(res.taken->session_id, "");
    EXPECT_EQ(res.taken->created_at, "2030-01-01T00:00:00Z");
    EXPECT_EQ(res.taken->content_mark, "e3:3:eng-2|s0:0:|u0:0:");
    EXPECT_TRUE(fs::exists(res.taken->path));
    ASSERT_EQ(reg.snapshots("sim-1").size(), 1U);
}

TEST(WorldAutoSnapshot, IntervalDisabledWhenZero) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");   // 新世界默认 {true, 0}
    seed_world_db(reg.get("sim-1").db_path, "sim-1", 3);
    const auto res = interval_at(reg, "2030-01-01T00:00:00Z");
    EXPECT_FALSE(res.taken.has_value());
    EXPECT_EQ(res.skipped, "disabled");
    EXPECT_TRUE(fs::is_empty(reg.get("sim-1").snapshot_dir));
}

// 间隔没到：哪怕内容变了也不拍。恰好到间隔（差值 == interval）就算到了。
TEST(WorldAutoSnapshot, IntervalNotDueBeforeTheIntervalEvenIfContentChanged) {
    const TempDir tmp;
    auto reg = interval_world(tmp);
    ASSERT_TRUE(interval_at(reg, "2030-01-01T00:00:00Z").taken.has_value());
    append_engrams(reg.get("sim-1").db_path, "sim-1", 3, 1);
    const auto early = interval_at(reg, "2030-01-01T23:59:59Z");
    EXPECT_FALSE(early.taken.has_value());
    EXPECT_EQ(early.skipped, "not_due");
    EXPECT_EQ(reg.snapshots("sim-1").size(), 1U);
    const auto due = interval_at(reg, "2030-01-02T00:00:00Z");
    ASSERT_TRUE(due.taken.has_value()) << due.skipped;
    EXPECT_EQ(reg.snapshots("sim-1").size(), 2U);
}

// 间隔到了、标记相同：不拍。原地更新（tick 改 activation 一类）不改变标记。
TEST(WorldAutoSnapshot, IntervalUnchangedWhenMarkIsTheSame) {
    const TempDir tmp;
    auto reg = interval_world(tmp);
    ASSERT_TRUE(interval_at(reg, "2030-01-01T00:00:00Z").taken.has_value());
    const auto same = interval_at(reg, "2030-01-02T01:00:00Z");
    EXPECT_FALSE(same.taken.has_value());
    EXPECT_EQ(same.skipped, "unchanged");
    exec_sql(reg.get("sim-1").db_path, "UPDATE engrams SET refcount = refcount + 7");
    EXPECT_EQ(interval_at(reg, "2030-01-03T01:00:00Z").skipped, "unchanged");
    EXPECT_EQ(reg.snapshots("sim-1").size(), 1U);
}

// 间隔到了、标记不同：拍。插入与删除都会改变标记。
TEST(WorldAutoSnapshot, IntervalTakesWhenContentChangedAfterTheInterval) {
    const TempDir tmp;
    auto reg = interval_world(tmp);
    const std::string db_path = reg.get("sim-1").db_path;
    ASSERT_TRUE(interval_at(reg, "2030-01-01T00:00:00Z").taken.has_value());
    append_engrams(db_path, "sim-1", 3, 1);
    const auto inserted = interval_at(reg, "2030-01-02T01:00:00Z");
    ASSERT_TRUE(inserted.taken.has_value()) << inserted.skipped;
    EXPECT_EQ(inserted.taken->content_mark, "e4:4:eng-3|s0:0:|u0:0:");
    exec_sql(db_path, "DELETE FROM engrams WHERE id='eng-0'");
    const auto deleted = interval_at(reg, "2030-01-03T02:00:00Z");
    ASSERT_TRUE(deleted.taken.has_value()) << deleted.skipped;
    EXPECT_EQ(deleted.taken->content_mark, "e3:4:eng-3|s0:0:|u0:0:");
    EXPECT_EQ(reg.snapshots("sim-1").size(), 3U);
}

// 标记每段去掉 id 之后的 "<tag><count>:<max_rowid>"（W4 T1 的旧格式），用来证明用例确实落在
// 旧格式看不见的情形里。表不存在的 "-" 原样保留。
std::vector<std::string> count_rowid_parts(const std::string& mark) {
    std::vector<std::string> out;
    std::size_t start = 0;
    while (start <= mark.size()) {
        const std::size_t stop = std::min(mark.find('|', start), mark.size());
        const std::string segment = mark.substr(start, stop - start);
        const std::size_t first_colon = segment.find(':');
        const std::size_t second_colon =
            first_colon == std::string::npos ? first_colon : segment.find(':', first_colon + 1);
        out.push_back(second_colon == std::string::npos ? segment
                                                        : segment.substr(0, second_colon));
        start = stop + 1;
    }
    return out;
}

// sim-1 只有本租户的行（没有别的租户插在中间抬高 rowid）：3 行 engram、2 条 statement、2 个 unit。
void seed_single_tenant(const std::string& db_path) {
    seed_world_db(db_path, "sim-1", 3);   // eng-0..2，rowid 1..3
    insert_statement(db_path, "sim-1", "st-1");
    insert_statement(db_path, "sim-1", "st-2");   // rowid 2
    insert_units(db_path, "sim-1", {{0, "c0", "spk-1", "s-a", "2030-01-01T00:00:00Z"},
                                    {1, "c0", "spk-1", "s-a", "2030-01-01T00:01:00Z"}});
}

// R1：删掉 rowid 最大的那一行、再插入一行，SQLite 复用那个 rowid，count:max_rowid 不变；标记里
// 那一行的 id 不同，标记照样会变。三张表各验一次，都走按时间触发（旧格式会判 unchanged）。
TEST(WorldSnapshotMeta, DeletingTheMaxRowThenInsertingChangesTheMark) {
    const TempDir tmp;
    auto reg = interval_world(tmp);   // 已有 eng-0..2
    set_retention(reg, 10);
    const std::string db_path = reg.get("sim-1").db_path;
    insert_statement(db_path, "sim-1", "st-1");
    insert_statement(db_path, "sim-1", "st-2");
    insert_units(db_path, "sim-1", {{0, "c0", "spk-1", "s-a", "2030-01-01T00:00:00Z"},
                                    {1, "c0", "spk-1", "s-a", "2030-01-01T00:01:00Z"}});
    const auto first = interval_at(reg, "2030-01-01T00:00:00Z");
    ASSERT_TRUE(first.taken.has_value()) << first.skipped;
    std::string prev = first.taken->content_mark;
    EXPECT_EQ(prev, "e3:3:eng-2|s2:2:st-2|u2:2:eng-1:c0");
    const auto expect_retaken = [&](const std::string& now, const std::string& what,
                                    const std::string& want) {
        const auto res = interval_at(reg, now);
        ASSERT_TRUE(res.taken.has_value()) << what << ": " << res.skipped;
        EXPECT_EQ(res.taken->content_mark, want) << what;
        EXPECT_EQ(count_rowid_parts(res.taken->content_mark), count_rowid_parts(prev)) << what;
        prev = res.taken->content_mark;
    };
    exec_sql(db_path, "DELETE FROM engrams WHERE id='eng-2'");
    append_engrams(db_path, "sim-1", 9, 1);   // 复用 rowid 3
    expect_retaken("2030-01-02T01:00:00Z", "engrams", "e3:3:eng-9|s2:2:st-2|u2:2:eng-1:c0");
    exec_sql(db_path, "DELETE FROM statements WHERE id='st-2'");
    insert_statement(db_path, "sim-1", "st-3");   // 复用 rowid 2
    expect_retaken("2030-01-03T02:00:00Z", "statements", "e3:3:eng-9|s2:2:st-3|u2:2:eng-1:c0");
    exec_sql(db_path, "DELETE FROM source_unit_meta WHERE engram_ref='eng-1' AND clause_id='c0'");
    insert_units(db_path, "sim-1", {{0, "c1", "spk-1", "s-a", "2030-01-01T00:02:00Z"}});
    expect_retaken("2030-01-04T03:00:00Z", "source_unit_meta",
                   "e3:3:eng-9|s2:2:st-3|u2:2:eng-0:c1");
    EXPECT_EQ(reg.snapshots("sim-1").size(), 4U);
}

// R1：清空租户（dashboard 的 clear_memory 走的就是 memoryops::clear_tenant）后重新摄入到相同的
// 行数：表空了，rowid 从 1 重新分配，count:max_rowid 与清空前一模一样；新行的 id 不同（真实 id
// 是随机 UUID），标记照样会变。
TEST(WorldSnapshotMeta, ClearingTheTenantThenReingestingTheSameCountsChangesTheMark) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    set_auto_snapshot(reg, "sim-1", AutoSnapshotConfig{false, 24});
    const std::string db_path = reg.get("sim-1").db_path;
    seed_single_tenant(db_path);
    const auto first = interval_at(reg, "2030-01-01T00:00:00Z");
    ASSERT_TRUE(first.taken.has_value()) << first.skipped;
    EXPECT_EQ(first.taken->content_mark, "e3:3:eng-2|s2:2:st-2|u2:2:eng-1:c0");
    {
        auto adapter = persistence::SqliteAdapter::open(db_path);
        EXPECT_GT(memoryops::clear_tenant(*adapter, "sim-1").total, 0);
    }
    EXPECT_EQ(interval_at(reg, "2030-01-02T01:00:00Z").skipped, "empty");
    append_engrams(db_path, "sim-1", 10, 3);   // eng-10..12，rowid 1..3
    insert_statement(db_path, "sim-1", "st-8");
    insert_statement(db_path, "sim-1", "st-9");
    insert_units(db_path, "sim-1", {{10, "c0", "spk-1", "s-b", "2030-01-02T00:00:00Z"},
                                    {11, "c0", "spk-1", "s-b", "2030-01-02T00:01:00Z"}});
    const auto again = interval_at(reg, "2030-01-03T01:00:00Z");
    ASSERT_TRUE(again.taken.has_value()) << again.skipped;
    EXPECT_EQ(again.taken->content_mark, "e3:3:eng-12|s2:2:st-9|u2:2:eng-11:c0");
    EXPECT_EQ(count_rowid_parts(again.taken->content_mark),
              count_rowid_parts(first.taken->content_mark));
    EXPECT_EQ(reg.snapshots("sim-1").size(), 2U);
}

TEST(WorldAutoSnapshot, IntervalSkipsAnEmptyWorld) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    set_auto_snapshot(reg, "sim-1", AutoSnapshotConfig{false, 24});
    // 库文件还不存在。
    EXPECT_EQ(interval_at(reg, "2030-01-01T00:00:00Z").skipped, "empty");
    // 已迁移、没有行。
    { auto adapter = persistence::SqliteAdapter::open(reg.get("sim-1").db_path); }
    EXPECT_EQ(interval_at(reg, "2030-01-01T00:00:00Z").skipped, "empty");
    // 别的租户有行也不算这个世界有内容。
    {
        auto adapter = persistence::SqliteAdapter::open(reg.get("sim-1").db_path);
        insert_engram(adapter->connection().raw(), "other", 100);
    }
    EXPECT_EQ(interval_at(reg, "2030-01-01T00:00:00Z").skipped, "empty");
    EXPECT_TRUE(fs::is_empty(reg.get("sim-1").snapshot_dir));
    EXPECT_TRUE(reg.snapshots("sim-1").empty());
}

// W4 之前的 auto 条目没有标记：间隔到了就拍，不按 unchanged 跳过。
TEST(WorldAutoSnapshot, IntervalTreatsLegacyEntryWithoutMarkAsChanged) {
    const TempDir tmp;
    auto reg = interval_world(tmp);
    auto legacy = fake_snapshot(reg.get("sim-1").snapshot_dir, "20300101T000000Z-aaaa",
                                "auto");
    legacy.reason = "interval";
    legacy.created_at = "2030-01-01T00:00:00Z";
    append_oldest(reg, "sim-1", legacy);
    EXPECT_EQ(interval_at(reg, "2030-01-01T12:00:00Z").skipped, "not_due");
    const auto res = interval_at(reg, "2030-01-02T01:00:00Z");
    ASSERT_TRUE(res.taken.has_value()) << res.skipped;
    EXPECT_EQ(reg.snapshots("sim-1").size(), 2U);
}

// last 的 created_at 解析不了：时间这一项按「到了」处理；标记那一项照常比较。
TEST(WorldAutoSnapshot, IntervalTreatsUnparseableLastTimeAsDue) {
    const TempDir tmp;
    auto reg = interval_world(tmp);
    ASSERT_TRUE(interval_at(reg, "2030-01-01T00:00:00Z").taken.has_value());
    set_snapshot_column(reg, "sim-1", reg.snapshots("sim-1").front().id, "created_at", "not-a-time");
    EXPECT_EQ(interval_at(reg, "2030-01-01T01:00:00Z").skipped, "unchanged");
    append_engrams(reg.get("sim-1").db_path, "sim-1", 3, 1);
    const auto res = interval_at(reg, "2030-01-01T01:00:00Z");
    ASSERT_TRUE(res.taken.has_value()) << res.skipped;
}

// M-1：last 晚于 now（时钟回拨，或那份快照的时间来自跳到未来的时钟）算「到了」，标记照常比较。
// 以前差值为负判 not_due，一份未来时间的条目会让按时间的快照一直停到真实时间追上它。
TEST(WorldAutoSnapshot, IntervalTreatsAFutureLastAsDue) {
    const TempDir tmp;
    auto reg = interval_world(tmp);
    ASSERT_TRUE(interval_at(reg, "2030-06-01T00:00:00Z").taken.has_value());   // 跳到未来的时钟
    const auto same = interval_at(reg, "2030-01-01T00:00:00Z");
    EXPECT_FALSE(same.taken.has_value());
    EXPECT_EQ(same.skipped, "unchanged");
    append_engrams(reg.get("sim-1").db_path, "sim-1", 3, 1);
    const auto changed = interval_at(reg, "2030-01-01T00:00:00Z");
    ASSERT_TRUE(changed.taken.has_value()) << changed.skipped;
    EXPECT_EQ(changed.taken->created_at, "2030-01-01T00:00:00Z");
    EXPECT_EQ(reg.snapshots("sim-1").size(), 2U);
}

// 比 last 更新的 manual 快照不参与判断：时间和标记都只看最新的 auto。
TEST(WorldAutoSnapshot, IntervalIgnoresANewerManualSnapshot) {
    const TempDir tmp;
    auto reg = interval_world(tmp);
    ASSERT_TRUE(interval_at(reg, "2030-01-01T00:00:00Z").taken.has_value());
    append_engrams(reg.get("sim-1").db_path, "sim-1", 3, 1);
    SnapshotOptions manual;
    manual.now_iso = "2030-01-01T23:00:00Z";   // 若算它：差 2 小时 not_due，标记也相同
    const auto snap = take_snapshot(reg, "sim-1", manual);
    ASSERT_EQ(snap.kind, "manual");
    const auto res = interval_at(reg, "2030-01-02T01:00:00Z");
    ASSERT_TRUE(res.taken.has_value()) << res.skipped;
    EXPECT_EQ(reg.snapshots("sim-1").size(), 3U);
}

// 「最新」按 created_at，不按列表位置：把更旧的那份挪到列表开头，结论不变。
TEST(WorldAutoSnapshot, IntervalUsesTheNewestAutoByCreatedAt) {
    const TempDir tmp;
    auto reg = interval_world(tmp);
    ASSERT_TRUE(interval_at(reg, "2030-01-01T00:00:00Z").taken.has_value());
    append_engrams(reg.get("sim-1").db_path, "sim-1", 3, 1);
    ASSERT_TRUE(interval_at(reg, "2030-01-02T01:00:00Z").taken.has_value());
    auto list = reg.snapshots("sim-1");
    ASSERT_EQ(list.size(), 2U);
    std::swap(list.front(), list.back());   // 旧的（标记过时）在前：重新按这个顺序登记
    set_snapshots(reg, "sim-1", list);
    ASSERT_EQ(reg.snapshots("sim-1").front().created_at, "2030-01-01T00:00:00Z");
    // 最新的 auto 在 01-02T01:00，差 1 小时：not_due。错拿旧的会因标记不同而拍。
    EXPECT_EQ(interval_at(reg, "2030-01-02T02:00:00Z").skipped, "not_due");
    EXPECT_EQ(reg.snapshots("sim-1").size(), 2U);
}

// 按会话拍的 auto 快照同样算 last（「任意 reason」）。
TEST(WorldAutoSnapshot, IntervalCountsASessionSnapshotAsLast) {
    const TempDir tmp;
    auto reg = interval_world(tmp);
    set_auto_snapshot(reg, "sim-1", AutoSnapshotConfig{true, 24});
    ASSERT_TRUE(take_auto_snapshot(reg, "sim-1",
                                   AutoTrigger{"session", "s-1", "2030-01-01T00:00:00Z"},
                                   SnapshotOptions{}).taken.has_value());
    EXPECT_EQ(interval_at(reg, "2030-01-01T05:00:00Z").skipped, "not_due");
    EXPECT_EQ(interval_at(reg, "2030-01-02T05:00:00Z").skipped, "unchanged");
}

TEST(WorldAutoSnapshot, RejectsBadTriggerAndUnknownWorld) {
    const TempDir tmp;
    auto reg = interval_world(tmp);
    EXPECT_EQ(code_of([&] {
        (void)take_auto_snapshot(reg, "nope", AutoTrigger{"interval", "", "2030-01-01T00:00:00Z"},
                                 SnapshotOptions{});
    }), "world_not_found");
    EXPECT_EQ(code_of([&] {
        (void)take_auto_snapshot(reg, "sim-1", AutoTrigger{"boundary", "", "2030-01-01T00:00:00Z"},
                                 SnapshotOptions{});
    }), "invalid_auto_snapshot");
    for (const char* bad : {"", "2030-01-01 00:00:00Z", "2030-01-01T00:00:00", "2030-02-30T00:00:00Z",
                            "2030-01-01T24:00:00Z", "2030-13-01T00:00:00Z", "2030-01-01T00:00:60Z",
                            "2030-1-01T00:00:00Z", "203a-01-01T00:00:00Z", "2030-01-01T00:00:00Z "}) {
        for (const char* kind : {"interval", "session"}) {
            EXPECT_EQ(code_of([&] {
                (void)take_auto_snapshot(reg, "sim-1", AutoTrigger{kind, "s-1", bad},
                                         SnapshotOptions{});
            }), "invalid_auto_snapshot") << kind << " now='" << bad << "'";
        }
    }
    EXPECT_TRUE(reg.snapshots("sim-1").empty());
}

// zvec：不拍、不抛、不写盘（两种触发都是）。
TEST(WorldAutoSnapshot, NonSqliteBackendIsSkippedWithoutWriting) {
    const TempDir tmp;
    auto reg = interval_world(tmp);
    set_auto_snapshot(reg, "sim-1", AutoSnapshotConfig{true, 24});
    SnapshotOptions base;
    base.vector_backend = "zvec";
    for (const char* kind : {"interval", "session"}) {
        AutoSnapshotResult res;
        EXPECT_EQ(code_of([&] {
            res = take_auto_snapshot(reg, "sim-1", AutoTrigger{kind, "s-1", "2030-01-01T00:00:00Z"},
                                     base);
        }), "<no throw>") << kind;
        EXPECT_FALSE(res.taken.has_value()) << kind;
        EXPECT_EQ(res.skipped, "unsupported_backend") << kind;
    }
    EXPECT_TRUE(fs::is_empty(reg.get("sim-1").snapshot_dir));
    EXPECT_TRUE(reg.snapshots("sim-1").empty());
}

// 拍快照走 take_snapshot 的原有路径：磁盘守卫照常生效，失败照常抛出。
TEST(WorldAutoSnapshot, TakeSnapshotFailuresPropagate) {
    const TempDir tmp;
    auto reg = interval_world(tmp);
    SnapshotOptions base;
    base.free_space = [](const fs::path&) -> std::int64_t { return 1; };
    EXPECT_EQ(code_of([&] {
        (void)take_auto_snapshot(reg, "sim-1", AutoTrigger{"interval", "", "2030-01-01T00:00:00Z"},
                                 base);
    }), "insufficient_disk");
    EXPECT_TRUE(reg.snapshots("sim-1").empty());
}

// 保留策略对自动快照照常生效，并且总留着最新的一份 auto。
TEST(WorldAutoSnapshot, RetentionStillKeepsTheNewestAuto) {
    const TempDir tmp;
    auto reg = interval_world(tmp);
    set_retention(reg, 2);
    const std::string db_path = reg.get("sim-1").db_path;
    std::string newest;
    for (int day = 1; day <= 3; ++day) {
        append_engrams(db_path, "sim-1", 10 + day, 1);
        const auto res = interval_at(reg, "2030-01-0" + std::to_string(day) + "T00:00:00Z");
        ASSERT_TRUE(res.taken.has_value()) << res.skipped;
        newest = res.taken->id;
    }
    for (int idx = 0; idx < 2; ++idx) { (void)take_snapshot(reg, "sim-1", SnapshotOptions{}); }
    const auto list = reg.snapshots("sim-1");
    EXPECT_EQ(list.size(), 2U);
    EXPECT_TRUE(std::ranges::any_of(list, [&newest](const SnapshotEntry& snap) {
        return snap.id == newest;
    }));
}

// ---- 修剪卡住时自动快照不再增长（Codex I-3） ----

// 一个按时间（24h）与按会话都开的世界，保留 3 份。注册表里 3+extra 份 auto 快照（最新在前），最旧的
// 那份在 snapshot_dir 里是指向外部的符号链接（字面在目录里，validate_world 放行）：apply_retention
// 删到它就 fail closed 抛 snapshot_failed。outside 里那个文件就是它，修剪不得删它。
RegistryDb stuck_retention_world(const TempDir& tmp, const TempDir& outside, int extra) {
    auto reg = registry_with_world(tmp, "sim-1");
    set_auto_snapshot(reg, "sim-1", AutoSnapshotConfig{true, 24});
    set_retention(reg, 3);
    seed_world_db(reg.get("sim-1").db_path, "sim-1", 3);
    const fs::path dir = reg.get("sim-1").snapshot_dir;
    std::vector<SnapshotEntry> list;
    for (int idx = 3 + extra; idx >= 2; --idx) {   // 最新在前：日期大的在前；共 extra+2 份
        auto snap = fake_snapshot(dir, "a" + std::to_string(idx), "auto");
        snap.reason = "interval";
        snap.created_at = "2029-12-0" + std::to_string(idx) + "T00:00:00Z";
        list.push_back(snap);
    }
    SnapshotEntry stuck;
    stuck.id = "a1";
    stuck.kind = "auto";
    stuck.reason = "interval";
    stuck.created_at = "2029-12-01T00:00:00Z";
    stuck.path = (dir / "a1.db").string();
    { std::ofstream out(outside.path() / "a1.db"); out << "x"; }
    fs::create_symlink(outside.path() / "a1.db", stuck.path);
    list.push_back(stuck);
    set_snapshots(reg, "sim-1", list);
    return reg;
}

std::size_t files_in(const fs::path& dir) {
    std::size_t held = 0;
    for (const auto& item : fs::directory_iterator(dir)) {
        (void)item;
        ++held;
    }
    return held;
}

// 修剪卡住、快照数已超出保留份数（上一次修剪失败留下的样子）：到期的按时间触发返回 refused（不抛），
// 原因点名 retention；不拍、快照目录不多文件、列表不变（卡住的是唯一的 victim，没有可删的），越界的
// 那份也没被碰。
TEST(WorldAutoRetention, IntervalRefusesWhenRetentionCannotPrune) {
    const TempDir tmp;
    const TempDir outside;
    auto reg = stuck_retention_world(tmp, outside, 1);   // 4 份，保留 3
    const fs::path dir = reg.get("sim-1").snapshot_dir;
    const auto before = ids_of(reg.snapshots("sim-1"));
    ASSERT_EQ(before.size(), 4U);
    const auto files_before = files_in(dir);
    AutoSnapshotResult res;
    ASSERT_NO_THROW(res = take_auto_snapshot(reg, "sim-1",
                                             AutoTrigger{"interval", "", "2030-01-01T00:00:00Z"},
                                             SnapshotOptions{}));
    EXPECT_FALSE(res.taken.has_value());
    EXPECT_EQ(res.skipped, "");
    EXPECT_TRUE(res.refused.starts_with("retention cannot prune")) << res.refused;
    EXPECT_NE(res.refused.find("snapshot_failed"), std::string::npos) << res.refused;   // 修剪的错误
    EXPECT_EQ(ids_of(reg.snapshots("sim-1")), before);
    EXPECT_EQ(files_in(dir), files_before);
    EXPECT_TRUE(fs::exists(outside.path() / "a1.db"));
}

// 同样的注册表，按会话触发（新会话）同样拒绝。
TEST(WorldAutoRetention, SessionRefusesWhenRetentionCannotPrune) {
    const TempDir tmp;
    const TempDir outside;
    auto reg = stuck_retention_world(tmp, outside, 1);
    const fs::path dir = reg.get("sim-1").snapshot_dir;
    const auto before = ids_of(reg.snapshots("sim-1"));
    const auto files_before = files_in(dir);
    AutoSnapshotResult res;
    ASSERT_NO_THROW(res = take_auto_snapshot(reg, "sim-1",
                                             AutoTrigger{"session", "s-new", "2030-01-01T00:00:00Z"},
                                             SnapshotOptions{}));
    EXPECT_FALSE(res.taken.has_value());
    EXPECT_TRUE(res.refused.starts_with("retention cannot prune")) << res.refused;
    EXPECT_EQ(ids_of(reg.snapshots("sim-1")), before);
    EXPECT_EQ(files_in(dir), files_before);
}

// Codex r2 R2-2：积压到保留份数 + 2（手动快照或调低了保留份数），最旧的正常 victim 排在卡住的那份
// 之前。修剪先删掉正常 victim（文件 + 注册项），再卡在越界的那份上。拒绝必须把这份部分修剪后的
// 注册表留给调用方保存：正常 victim 的文件与注册项都没了，卡住的那份还在，没有写新快照文件。
// 抛出去的话，绑定拿的注册表副本就丢了，worlds.json 里留下文件已不在的条目。
TEST(WorldAutoRetention, RefusalKeepsThePartialPruneForTheCallerToSave) {
    const TempDir tmp;
    const TempDir outside;
    auto reg = stuck_retention_world(tmp, outside, 1);   // a4 a3 a2 + 越界的 a1（created_at 12-01）
    const fs::path dir = reg.get("sim-1").snapshot_dir;
    // 再加一份更旧、能正常删的 auto（a0，11-30）：victim 从最旧起，它排在 a1 之前。共 5 = R + 2。
    auto normal = fake_snapshot(dir, "a0", "auto");
    normal.reason = "interval";
    normal.created_at = "2029-11-30T00:00:00Z";
    append_oldest(reg, "sim-1", normal);
    ASSERT_EQ(ids_of(reg.snapshots("sim-1")),
              (std::vector<std::string>{"a4", "a3", "a2", "a1", "a0"}));
    ASSERT_TRUE(fs::exists(dir / "a0.db"));
    const auto files_before = files_in(dir);

    AutoSnapshotResult res;
    ASSERT_NO_THROW(res = take_auto_snapshot(reg, "sim-1",
                                             AutoTrigger{"interval", "", "2030-01-01T00:00:00Z"},
                                             SnapshotOptions{}));
    EXPECT_FALSE(res.taken.has_value());
    EXPECT_TRUE(res.refused.starts_with("retention cannot prune")) << res.refused;
    EXPECT_NE(res.refused.find("4 held"), std::string::npos) << res.refused;
    // 正常 victim：文件删了，注册项也摘了（就在调用方拿到的 reg 里）。
    EXPECT_FALSE(fs::exists(dir / "a0.db"));
    EXPECT_EQ(ids_of(reg.snapshots("sim-1")),
              (std::vector<std::string>{"a4", "a3", "a2", "a1"}));
    // 卡住的那份还在，文件没被碰；没有写新快照文件（目录里只少了 a0.db）。
    EXPECT_TRUE(fs::exists(outside.path() / "a1.db"));
    EXPECT_EQ(files_in(dir), files_before - 1);

    // 调用方存下这份注册表之后，再触发不会重演：没有可删的正常 victim，照样拒绝，列表不变。
    const auto again = take_auto_snapshot(reg, "sim-1",
                                          AutoTrigger{"interval", "", "2030-01-02T01:00:00Z"},
                                          SnapshotOptions{});
    EXPECT_FALSE(again.taken.has_value());
    EXPECT_FALSE(again.refused.empty());
    EXPECT_EQ(ids_of(reg.snapshots("sim-1")),
              (std::vector<std::string>{"a4", "a3", "a2", "a1"}));
}

// 卡住的世界恰好在保留份数上（修剪无事可做，不会抛）：这一次照常拍，take_snapshot 自己的修剪
// 随后卡在越界的那份上（吞掉），总数到保留份数 + 1；之后的每一次触发都拒绝。自动触发的快照数
// 因此有上限，不随触发次数增长（手动快照仍沿用吞掉修剪失败的语义，可以越过它）。
TEST(WorldAutoRetention, StuckWorldIsCappedAtRetentionPlusOne) {
    const TempDir tmp;
    const TempDir outside;
    auto reg = stuck_retention_world(tmp, outside, 0);   // 3 份 = 保留 3
    const std::string db_path = reg.get("sim-1").db_path;
    ASSERT_EQ(reg.snapshots("sim-1").size(), 3U);
    const auto first = take_auto_snapshot(reg, "sim-1",
                                          AutoTrigger{"interval", "", "2030-01-01T00:00:00Z"},
                                          SnapshotOptions{});
    ASSERT_TRUE(first.taken.has_value()) << first.skipped;
    EXPECT_EQ(first.refused, "");
    EXPECT_EQ(reg.snapshots("sim-1").size(), 4U);
    for (int day = 2; day <= 6; ++day) {
        append_engrams(db_path, "sim-1", 10 + day, 1);   // 每天都有新内容：不会因 unchanged 跳过
        const auto res = take_auto_snapshot(
            reg, "sim-1",
            AutoTrigger{"interval", "", "2030-01-0" + std::to_string(day) + "T01:00:00Z"},
            SnapshotOptions{});
        EXPECT_FALSE(res.taken.has_value()) << day;
        EXPECT_FALSE(res.refused.empty()) << day;
        EXPECT_EQ(reg.snapshots("sim-1").size(), 4U) << day;
    }
    EXPECT_TRUE(fs::exists(outside.path() / "a1.db"));
}

// 修剪能删（正常情况）：超出的那份先被修剪掉，自动快照照常拍，总数停在保留份数。
TEST(WorldAutoRetention, PrunableWorldProceedsAndStaysAtRetention) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    set_auto_snapshot(reg, "sim-1", AutoSnapshotConfig{true, 24});
    set_retention(reg, 3);
    seed_world_db(reg.get("sim-1").db_path, "sim-1", 3);
    const fs::path dir = reg.get("sim-1").snapshot_dir;
    std::vector<SnapshotEntry> list;
    for (int idx = 4; idx >= 1; --idx) {   // 4 份，超出 1（上一次修剪失败、如今已能删）
        auto snap = fake_snapshot(dir, "a" + std::to_string(idx), "auto");
        snap.reason = "interval";
        snap.created_at = "2029-12-0" + std::to_string(idx) + "T00:00:00Z";
        list.push_back(snap);
    }
    set_snapshots(reg, "sim-1", list);
    const auto taken = take_auto_snapshot(reg, "sim-1",
                                          AutoTrigger{"interval", "", "2030-01-01T00:00:00Z"},
                                          SnapshotOptions{});
    ASSERT_TRUE(taken.taken.has_value()) << taken.skipped;
    EXPECT_EQ(taken.refused, "");
    const auto after = reg.snapshots("sim-1");
    EXPECT_EQ(after.size(), 3U);
    EXPECT_EQ(after.front().id, taken.taken->id);
    EXPECT_FALSE(fs::exists(dir / "a1.db"));
    EXPECT_FALSE(fs::exists(dir / "a2.db"));
    EXPECT_EQ(files_in(dir), 3U);
}

// ---- 会话清单 ----

// 四个会话：s-b 最新，s-a 次之，s-d 最旧，s-c 没有时间（排最后）。另一个租户有同名会话。
void seed_sessions(const std::string& db_path) {
    seed_world_db(db_path, "sim-1", 5);   // eng-0..4
    {
        auto adapter = persistence::SqliteAdapter::open(db_path);
        insert_engram(adapter->connection().raw(), "other", 100);
    }
    insert_units(db_path, "sim-1", {
        {0, "c0", "spk-1", "s-a", "2030-01-01T10:00:00Z"},
        {0, "c1", "spk-2", "s-a", "2030-01-01T10:05:00Z"},
        {1, "c0", "spk-1", "s-a", "2030-01-01T10:10:00Z"},
        {2, "c0", "spk-3", "s-b", "2030-01-02T09:00:00Z"},
        {2, "c1", "spk-3", "s-b", std::nullopt},
        {3, "c0", "spk-4", "s-c", std::nullopt},
        {4, "c0", "spk-5", "s-d", "2029-12-31T00:00:00Z"},
    });
    insert_units(db_path, "other", {{100, "c0", "spk-9", "s-a", "2031-01-01T00:00:00Z"},
                                    {100, "c1", "spk-9", "s-z", std::nullopt}});
}

std::vector<std::string> session_ids(const SessionList& list) {
    std::vector<std::string> out;
    for (const auto& row : list.rows) { out.push_back(row.session_id); }
    return out;
}

TEST(WorldSessions, AggregatesPerSessionInFirstAtDescendingOrder) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    seed_sessions(reg.get("sim-1").db_path);
    const auto list = list_sessions(reg, "sim-1", 200);
    EXPECT_EQ(session_ids(list), (std::vector<std::string>{"s-b", "s-a", "s-d", "s-c"}));
    EXPECT_EQ(list.total, 4);
    EXPECT_EQ(list.null_observed, 2);
    EXPECT_FALSE(list.truncated);
    ASSERT_EQ(list.rows.size(), 4U);
    const auto& sb = list.rows.at(0);
    EXPECT_EQ(sb.units, 2);
    EXPECT_EQ(sb.speakers, 1);
    EXPECT_EQ(sb.first_at, "2030-01-02T09:00:00Z");
    EXPECT_EQ(sb.last_at, "2030-01-02T09:00:00Z");
    const auto& sa = list.rows.at(1);
    EXPECT_EQ(sa.units, 3);
    EXPECT_EQ(sa.speakers, 2);
    EXPECT_EQ(sa.first_at, "2030-01-01T10:00:00Z");
    EXPECT_EQ(sa.last_at, "2030-01-01T10:10:00Z");
    const auto& sc = list.rows.at(3);
    EXPECT_EQ(sc.units, 1);
    EXPECT_EQ(sc.first_at, "");
    EXPECT_EQ(sc.last_at, "");
    for (const auto& row : list.rows) { EXPECT_TRUE(row.anchor_snapshot_id.empty()); }
}

TEST(WorldSessions, LimitTruncatesAndValidatesItsRange) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    seed_sessions(reg.get("sim-1").db_path);
    const auto two = list_sessions(reg, "sim-1", 2);
    EXPECT_EQ(session_ids(two), (std::vector<std::string>{"s-b", "s-a"}));
    EXPECT_EQ(two.total, 4);
    EXPECT_TRUE(two.truncated);
    EXPECT_EQ(two.null_observed, 2);   // 不受 limit 影响
    const auto four = list_sessions(reg, "sim-1", 4);
    EXPECT_EQ(four.rows.size(), 4U);
    EXPECT_FALSE(four.truncated);
    EXPECT_EQ(list_sessions(reg, "sim-1", 1).rows.size(), 1U);
    EXPECT_EQ(list_sessions(reg, "sim-1", 1000).rows.size(), 4U);
    for (const std::int64_t bad : {std::int64_t{0}, std::int64_t{-1}, std::int64_t{1001}}) {
        EXPECT_EQ(code_of([&] { (void)list_sessions(reg, "sim-1", bad); }),
                  "invalid_auto_snapshot") << bad;
    }
    EXPECT_EQ(code_of([&] { (void)list_sessions(reg, "nope", 10); }), "world_not_found");
}

// 锚点：kind=auto、reason=session、session_id 相同的快照里 created_at 最新的一份。
TEST(WorldSessions, AnchorsTheNewestSessionSnapshotOfEachSession) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    set_retention(reg, 10);   // 不让保留策略删掉构造的条目
    const std::string db_path = reg.get("sim-1").db_path;
    seed_sessions(db_path);
    const fs::path dir = reg.get("sim-1").snapshot_dir;
    const auto entry = [&dir](const std::string& snap_id, const std::string& kind,
                              const std::string& reason, const std::string& session,
                              const std::string& created) {
        auto snap = fake_snapshot(dir, snap_id, kind);
        snap.reason = reason;
        snap.session_id = session;
        snap.created_at = created;
        return snap;
    };
    // 列表顺序故意不按时间：更新的 a-new 排在 a-old 之后。
    set_snapshots(reg, "sim-1", {
        entry("a-old", "auto", "session", "s-a", "2030-01-01T09:00:00Z"),
        entry("a-new", "auto", "session", "s-a", "2030-01-01T09:30:00Z"),
        entry("i-1", "auto", "interval", "", "2030-01-03T00:00:00Z"),
        entry("m-1", "manual", "manual", "s-b", "2030-01-03T00:00:00Z"),   // manual 不算
        entry("d-1", "auto", "session", "s-d", "2029-12-30T23:00:00Z"),
    });
    // 再用真实的按会话触发拍一份，然后才写这个会话。
    const auto fresh = take_auto_snapshot(reg, "sim-1",
                                          AutoTrigger{"session", "s-e", "2030-01-05T00:00:00Z"},
                                          SnapshotOptions{});
    ASSERT_TRUE(fresh.taken.has_value()) << fresh.skipped;
    insert_units(db_path, "sim-1", {{0, "c2", "spk-1", "s-e", "2030-01-05T00:00:01Z"}});

    const auto list = list_sessions(reg, "sim-1", 200);
    EXPECT_EQ(session_ids(list), (std::vector<std::string>{"s-e", "s-b", "s-a", "s-d", "s-c"}));
    std::map<std::string, std::pair<std::string, std::string>> anchors;
    for (const auto& row : list.rows) {
        anchors[row.session_id] = {row.anchor_snapshot_id, row.anchor_created_at};
    }
    EXPECT_EQ(anchors.at("s-e"),
              (std::pair<std::string, std::string>{fresh.taken->id, "2030-01-05T00:00:00Z"}));
    EXPECT_EQ(anchors.at("s-a"),
              (std::pair<std::string, std::string>{"a-new", "2030-01-01T09:30:00Z"}));
    EXPECT_EQ(anchors.at("s-d"),
              (std::pair<std::string, std::string>{"d-1", "2029-12-30T23:00:00Z"}));
    EXPECT_EQ(anchors.at("s-b"), (std::pair<std::string, std::string>{"", ""}));
    EXPECT_EQ(anchors.at("s-c"), (std::pair<std::string, std::string>{"", ""}));
}

// 空串的 observed_at 与 NULL 一样不算时间（同 world_time_on）：不会把 first_at 拉成空。
// null_observed 只数 IS NULL（计划原文）。
TEST(WorldSessions, EmptyObservedAtIsNotATime) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const std::string db_path = reg.get("sim-1").db_path;
    seed_world_db(db_path, "sim-1", 1);
    insert_units(db_path, "sim-1", {{0, "c0", "spk-1", "s-a", ""},
                                    {0, "c1", "spk-1", "s-a", "2030-01-01T10:00:00Z"},
                                    {0, "c2", "spk-1", "s-b", ""}});
    const auto list = list_sessions(reg, "sim-1", 10);
    EXPECT_EQ(session_ids(list), (std::vector<std::string>{"s-a", "s-b"}));
    ASSERT_EQ(list.rows.size(), 2U);
    EXPECT_EQ(list.rows.at(0).first_at, "2030-01-01T10:00:00Z");
    EXPECT_EQ(list.rows.at(0).units, 2);
    EXPECT_EQ(list.rows.at(1).first_at, "");
    EXPECT_EQ(list.null_observed, 0);
}

TEST(WorldSessions, EmptyWhenDbOrTableIsMissingAndFailsOnUnreadableDb) {
    const TempDir tmp;
    auto reg = registry_with_world(tmp, "sim-1");
    const auto expect_empty = [&reg](const std::string& label) {
        const auto list = list_sessions(reg, "sim-1", 200);
        EXPECT_TRUE(list.rows.empty()) << label;
        EXPECT_EQ(list.total, 0) << label;
        EXPECT_EQ(list.null_observed, 0) << label;
        EXPECT_FALSE(list.truncated) << label;
    };
    expect_empty("db missing");
    const std::string db_path = reg.get("sim-1").db_path;
    exec_sql_unmigrated(db_path, "CREATE TABLE engrams(id TEXT, tenant_id TEXT, created_at TEXT);");
    expect_empty("table missing");
    fs::remove(db_path);
    { std::ofstream out(db_path); out << "this is not a sqlite database at all, nope"; }
    EXPECT_EQ(code_of([&] { (void)list_sessions(reg, "sim-1", 200); }), "snapshot_failed");
}

}  // namespace
}  // namespace starling::governance
