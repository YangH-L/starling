// import_worlds_json：旧注册表 worlds.json 一次性导入 registry.db（设计稿 §5.4）。
//
// 旧注册表的解析与校验随导入器搬进 registry_import.cpp（裁定 R7），原来对 load_registry 的校验用例
// （RegistryValidation）改成对导入的用例：坏的 worlds.json 导入失败，原文件逐字节不变，不留
// registry.db 与 registry.db.importing。
#include "starling/governance/registry_db.hpp"
#include "starling/governance/world_settings.hpp"
#include "starling/governance/world_store.hpp"

#include <gtest/gtest.h>
#include <unistd.h>

#include <atomic>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iterator>
#include <optional>
#include <string>
#include <utility>
#include <vector>

namespace starling::governance {
namespace {

namespace fs = std::filesystem;

class TempDir {
public:
    TempDir() {
        static std::atomic<int> seq{0};
        path_ = fs::temp_directory_path() /
                ("starling-import-" + std::to_string(::getpid()) + "-" + std::to_string(++seq));
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

void write_text(const fs::path& file, const std::string& text) {
    std::ofstream out(file, std::ios::binary | std::ios::trunc);
    out << text;
}

std::string code_of(const std::function<void()>& action) {
    try { action(); } catch (const WorldStoreError& err) { return err.code(); }
    return "<no throw>";
}

std::string what_of(const std::function<void()>& action) {
    try { action(); } catch (const WorldStoreError& err) { return err.what(); }
    return {};
}

// 数据目录里名字以 worlds.json.imported- 开头的文件。
std::vector<fs::path> imported_copies(const fs::path& data_dir) {
    std::vector<fs::path> out;
    for (const auto& item : fs::directory_iterator(data_dir)) {
        if (item.path().filename().string().starts_with("worlds.json.imported-")) {
            out.push_back(item.path());
        }
    }
    return out;
}

// registry.db 与 registry.db.importing（含 SQLite 旁车）一个都不在。
void expect_no_registry_files(const fs::path& data_dir, const std::string& label) {
    for (const char* name : {"registry.db", "registry.db-wal", "registry.db-shm",
                             "registry.db.importing", "registry.db.importing-wal",
                             "registry.db.importing-shm", "registry.db.importing-journal"}) {
        EXPECT_FALSE(fs::exists(fs::symlink_status(data_dir / name))) << label << ": " << name;
    }
}

AutoSnapshotConfig auto_of(const RegistryDb& reg, const std::string& world) {
    return settings_from_json(reg.get(world).settings_json).background.auto_snapshot;
}

RegistryDb reopen(const fs::path& data_dir) {
    // 已有 registry.db：这两个参数不再起作用（默认世界的路径与租户取自导入的记录）。
    return RegistryDb::open(data_dir, data_dir / "ignored.db", "ignored-tenant");
}

// default 原地（<tmp>/dashboard.db），sim-1 在 worlds/sim-1/ 下，带两份快照：较新的一份是 W4 的
// 按会话自动快照（三个新字段都有），较旧的一份是 W2 的手动快照（没有新字段）。active 指向 sim-1
// （活动世界的概念已去掉，导入时忽略）。
std::string sample_registry(const fs::path& data_dir, const std::string& older_snapshot_path) {
    const std::string home = (data_dir / "worlds").string();
    return std::string(R"({"version":1,"default":"default","active":"sim-1","snapshot_retention":7,)") +
           R"("worlds":{"default":{"tenant":"default","db_path":")" +
           (data_dir / "dashboard.db").string() + R"(","snapshot_dir":")" + home +
           R"(/default/snapshots","label":"主世界","created_at":"2026-10-25T00:00:00Z",)" +
           R"("auto_snapshot":{"on_session":false,"interval_hours":24}},)" +
           R"("sim-1":{"tenant":"sim-1","db_path":")" + home + R"(/sim-1/world.db",)" +
           R"("snapshot_dir":")" + home + R"(/sim-1/snapshots","label":"模拟一",)" +
           R"("created_at":"2026-10-26T00:00:00Z",)" +
           R"("auto_snapshot":{"on_session":true,"interval_hours":0}}},)" +
           R"("snapshots":{"sim-1":[)" +
           R"({"id":"20261028T010000Z-bbbb","path":")" + home +
           R"(/sim-1/snapshots/20261028T010000Z-bbbb.db","kind":"auto","reason":"session",)" +
           R"("note":"","created_at":"2026-10-28T01:00:00Z","world_time":"2026-10-28T00:59:00Z",)" +
           R"("size_bytes":4096,"sessions_complete":3,"session_id":"s-3","content_mark":"e1:1:a|-|-"},)" +
           R"({"id":"20261027T010000Z-aaaa","path":")" + older_snapshot_path +
           R"(","kind":"manual","reason":"manual","note":"备注 with 'quote'",)" +
           R"("created_at":"2026-10-27T01:00:00Z","world_time":"","size_bytes":123}]}})";
}

std::string older_snapshot_path(const fs::path& data_dir) {
    return (data_dir / "worlds" / "sim-1" / "snapshots" / "20261027T010000Z-aaaa.db").string();
}

}  // namespace

TEST(RegistryImport, ImportsWorldsAndSnapshots) {
    const TempDir tmp;
    const std::string text = sample_registry(tmp.path(), older_snapshot_path(tmp.path()));
    write_text(tmp.path() / "worlds.json", text);
    fs::create_directories(tmp.path() / "worlds" / "sim-1" / "snapshots");

    import_worlds_json(tmp.path());

    EXPECT_FALSE(fs::exists(tmp.path() / "worlds.json"));
    const auto copies = imported_copies(tmp.path());
    ASSERT_EQ(copies.size(), 1U);
    // worlds.json.imported-<YYYYMMDDTHHMMSSZ>，内容逐字节是原文件。
    EXPECT_EQ(copies.front().filename().string().size(),
              std::string("worlds.json.imported-20261104T001200Z").size())
        << copies.front();
    EXPECT_EQ(slurp(copies.front()), text);
    EXPECT_FALSE(fs::exists(tmp.path() / "registry.db.importing"));
    EXPECT_FALSE(fs::exists(tmp.path() / "registry.db.importing-wal"));
    ASSERT_TRUE(fs::exists(tmp.path() / "registry.db"));

    const auto reg = reopen(tmp.path());
    EXPECT_EQ(reg.default_world(), "default");
    EXPECT_EQ(reg.snapshot_retention(), 7);
    EXPECT_EQ(reg.list(WorldQuery{}).total, 2);

    const auto dflt = reg.get("default");
    EXPECT_EQ(dflt.dir_id, "default");
    EXPECT_EQ(dflt.tenant, "default");
    EXPECT_EQ(fs::path(dflt.db_path), tmp.path() / "dashboard.db");   // 取自 worlds.json，不是 open 的参数
    EXPECT_EQ(fs::path(dflt.snapshot_dir), tmp.path() / "worlds" / "default" / "snapshots");
    EXPECT_EQ(dflt.label, "主世界");
    EXPECT_EQ(dflt.created_at, "2026-10-25T00:00:00Z");
    EXPECT_EQ(auto_of(reg, "default"), (AutoSnapshotConfig{.on_session = false, .interval_hours = 24}));

    const auto sim = reg.get("sim-1");
    EXPECT_EQ(sim.dir_id, "sim-1");   // 导入的世界保留原目录名
    EXPECT_EQ(sim.tenant, "sim-1");
    EXPECT_EQ(fs::path(sim.db_path), tmp.path() / "worlds" / "sim-1" / "world.db");
    EXPECT_EQ(fs::path(sim.snapshot_dir), tmp.path() / "worlds" / "sim-1" / "snapshots");
    EXPECT_EQ(sim.label, "模拟一");
    EXPECT_EQ(sim.created_at, "2026-10-26T00:00:00Z");
    EXPECT_TRUE(sim.labels.empty());
    EXPECT_EQ(auto_of(reg, "sim-1"), (AutoSnapshotConfig{.on_session = true, .interval_hours = 0}));

    // 快照：顺序与 worlds.json 的列表相同（最新在前），字段逐一相等。
    const auto snaps = reg.snapshots("sim-1");
    ASSERT_EQ(snaps.size(), 2U);
    const auto& newer = snaps.at(0);
    EXPECT_EQ(newer.id, "20261028T010000Z-bbbb");
    EXPECT_EQ(fs::path(newer.path),
              tmp.path() / "worlds" / "sim-1" / "snapshots" / "20261028T010000Z-bbbb.db");
    EXPECT_EQ(newer.kind, "auto");
    EXPECT_EQ(newer.reason, "session");
    EXPECT_EQ(newer.note, "");
    EXPECT_EQ(newer.created_at, "2026-10-28T01:00:00Z");
    EXPECT_EQ(newer.world_time, "2026-10-28T00:59:00Z");
    EXPECT_EQ(newer.size_bytes, 4096);
    EXPECT_EQ(newer.sessions_complete, std::optional<std::int64_t>{3});
    EXPECT_EQ(newer.session_id, "s-3");
    EXPECT_EQ(newer.content_mark, "e1:1:a|-|-");
    const auto& older = snaps.at(1);
    EXPECT_EQ(older.id, "20261027T010000Z-aaaa");
    EXPECT_EQ(older.path, older_snapshot_path(tmp.path()));
    EXPECT_EQ(older.kind, "manual");
    EXPECT_EQ(older.reason, "manual");
    EXPECT_EQ(older.note, "备注 with 'quote'");
    EXPECT_EQ(older.created_at, "2026-10-27T01:00:00Z");
    EXPECT_EQ(older.world_time, "");
    EXPECT_EQ(older.size_bytes, 123);
    EXPECT_FALSE(older.sessions_complete.has_value());
    EXPECT_EQ(older.session_id, "");
    EXPECT_EQ(older.content_mark, "");
    EXPECT_TRUE(reg.snapshots("default").empty());

    // 导入的布局（世界目录 = worlds/<dir_id>，dir_id = 原世界名）通过注册表检查。
    EXPECT_EQ(code_of([&] { validate_registry(reg); }), "<no throw>");
}

TEST(RegistryImport, CrashMidImportLeavesJsonUntouched) {   // Review Focus 5
    const TempDir tmp;
    const TempDir outside;
    // 一份快照的 path 越出所属世界的 snapshot_dir：校验在写任何文件之前失败。
    const std::string bad =
        sample_registry(tmp.path(), (outside.path() / "20261027T010000Z-aaaa.db").string());
    write_text(tmp.path() / "worlds.json", bad);
    const std::string what = what_of([&] { import_worlds_json(tmp.path()); });
    EXPECT_EQ(code_of([&] { import_worlds_json(tmp.path()); }), "registry_corrupt");
    EXPECT_NE(what.find("lies outside"), std::string::npos) << what;
    EXPECT_EQ(slurp(tmp.path() / "worlds.json"), bad);
    EXPECT_TRUE(imported_copies(tmp.path()).empty());
    expect_no_registry_files(tmp.path(), "bad snapshot path");

    // 修好 worlds.json 后再导入成功。
    write_text(tmp.path() / "worlds.json", sample_registry(tmp.path(), older_snapshot_path(tmp.path())));
    EXPECT_EQ(code_of([&] { import_worlds_json(tmp.path()); }), "<no throw>");
    EXPECT_TRUE(fs::exists(tmp.path() / "registry.db"));
    EXPECT_FALSE(fs::exists(tmp.path() / "worlds.json"));
    EXPECT_EQ(reopen(tmp.path()).snapshots("sim-1").size(), 2U);
}

// 写库阶段失败（registry.db.importing 已经建出来之后）：同一世界里两份快照 id 相同，写到第二份时
// 撞主键。.importing 与旁车删掉，worlds.json 逐字节不变，抛 registry_corrupt。
TEST(RegistryImport, FailureWhileWritingRemovesTheImportingFile) {
    const TempDir tmp;
    std::string text = sample_registry(tmp.path(), older_snapshot_path(tmp.path()));
    const std::string dup_from = R"("id":"20261027T010000Z-aaaa")";
    text.replace(text.find(dup_from), dup_from.size(), R"("id":"20261028T010000Z-bbbb")");
    write_text(tmp.path() / "worlds.json", text);
    EXPECT_EQ(code_of([&] { import_worlds_json(tmp.path()); }), "registry_corrupt");
    EXPECT_EQ(slurp(tmp.path() / "worlds.json"), text);
    EXPECT_TRUE(imported_copies(tmp.path()).empty());
    expect_no_registry_files(tmp.path(), "duplicate snapshot id");
}

// 上次导入死在写库途中，留下了 .importing 与 -wal：这次先删掉它们，从头再来。
TEST(RegistryImport, StaleImportingFileFromACrashIsReplaced) {
    const TempDir tmp;
    write_text(tmp.path() / "registry.db.importing", "half-written garbage, not a database");
    write_text(tmp.path() / "registry.db.importing-wal", "stale wal");
    write_text(tmp.path() / "worlds.json", sample_registry(tmp.path(), older_snapshot_path(tmp.path())));
    import_worlds_json(tmp.path());
    EXPECT_FALSE(fs::exists(tmp.path() / "registry.db.importing"));
    EXPECT_FALSE(fs::exists(tmp.path() / "registry.db.importing-wal"));
    EXPECT_EQ(reopen(tmp.path()).get("sim-1").dir_id, "sim-1");
}

// registry.db 已在：什么都不做，worlds.json 不碰（哪怕它是坏的）。
TEST(RegistryImport, NoOpWhenRegistryDbExists) {
    const TempDir tmp;
    { const auto reg = RegistryDb::open(tmp.path(), tmp.path() / "dashboard.db", "default"); }
    write_text(tmp.path() / "worlds.json", "{not json");
    EXPECT_EQ(code_of([&] { import_worlds_json(tmp.path()); }), "<no throw>");
    EXPECT_EQ(slurp(tmp.path() / "worlds.json"), "{not json");
    EXPECT_TRUE(imported_copies(tmp.path()).empty());
    EXPECT_EQ(reopen(tmp.path()).list(WorldQuery{}).total, 1);
}

// 没有 worlds.json：什么都不做，也不建 registry.db。
TEST(RegistryImport, NoOpWithoutWorldsJson) {
    const TempDir tmp;
    EXPECT_EQ(code_of([&] { import_worlds_json(tmp.path()); }), "<no throw>");
    expect_no_registry_files(tmp.path(), "no worlds.json");
}

// JSON 语法错误、默认世界不叫 default、保留份数 < 2：都是 registry_corrupt（原码写在消息里），
// 原文件不变。（原 WorldRegistry.CorruptFileFailsLoud / RejectsRetentionBelowTwo 的导入一半。）
TEST(RegistryImport, RejectsCorruptJsonRenamedDefaultAndLowRetention) {
    const TempDir tmp;
    const std::string good = sample_registry(tmp.path(), older_snapshot_path(tmp.path()));
    std::string renamed = good;
    const std::string dkey = R"("default":"default")";
    renamed.replace(renamed.find(dkey), dkey.size(), R"("default":"sim-1")");
    std::string low = good;
    const std::string rkey = R"("snapshot_retention":7)";
    low.replace(low.find(rkey), rkey.size(), R"("snapshot_retention":1)");
    for (const auto& [label, text] : std::vector<std::pair<std::string, std::string>>{
             {"syntax", "{not json"}, {"default renamed", renamed}, {"retention 1", low}}) {
        write_text(tmp.path() / "worlds.json", text);
        const std::string what = what_of([&] { import_worlds_json(tmp.path()); });
        EXPECT_EQ(code_of([&] { import_worlds_json(tmp.path()); }), "registry_corrupt") << label;
        EXPECT_EQ(slurp(tmp.path() / "worlds.json"), text) << label;
        expect_no_registry_files(tmp.path(), label);
        if (label == "retention 1") {
            EXPECT_NE(what.find("invalid_retention"), std::string::npos) << what;
        }
    }
}

// ---- 旧注册表的校验（原 test_world_store.cpp 的 RegistryValidation，load_registry → 导入）------

// 每条校验规则一个坏注册表。基线是一份合法的注册表文本，每次只改一处。
class RegistryValidation : public ::testing::Test {
protected:
    TempDir tmp;
    std::string home = (tmp.path() / "worlds").string();

    // 合法基线：default 原地，sim-1 在 worlds/sim-1/ 下，各有一份快照。
    [[nodiscard]] std::string text(const std::string& sim_key, const std::string& sim_db,
                                   const std::string& sim_snapdir, const std::string& snap_kind,
                                   const std::string& snap_path,
                                   const std::string& snap_owner) const {
        const std::string dflt_snapdir = home + "/default/snapshots";
        return std::string(R"({"version":1,"default":"default","active":"default",)") +
               R"("snapshot_retention":5,"worlds":{)" +
               R"("default":{"tenant":"default","db_path":")" + (tmp.path() / "dashboard.db").string() +
               R"(","snapshot_dir":")" + dflt_snapdir + R"(","label":"","created_at":""},)" +
               "\"" + sim_key + R"(":{"tenant":"sim-1","db_path":")" + sim_db +
               R"(","snapshot_dir":")" + sim_snapdir + R"(","label":"","created_at":""}},)" +
               R"("snapshots":{")" + snap_owner + R"(":[{"id":"s1","path":")" + snap_path +
               R"(","kind":")" + snap_kind + R"(","reason":"manual","note":"","created_at":"",)" +
               R"("world_time":"","size_bytes":1}]}})";
    }
    [[nodiscard]] std::string good() const {
        return text("sim-1", home + "/sim-1/world.db", home + "/sim-1/snapshots", "manual",
                    home + "/sim-1/snapshots/s1.db", "sim-1");
    }

    // 写 worlds.json、导入、返回码。失败时断言原文件逐字节不变、不留 registry.db / .importing；
    // 成功时断言 registry.db 在、worlds.json 已改名。随后清掉导入的产物，同一个用例接着试下一份。
    std::string import_code_of(const std::string& body) {
        const fs::path json = tmp.path() / "worlds.json";
        write_text(json, body);
        const std::string code = code_of([&] { import_worlds_json(tmp.path()); });
        if (code == "<no throw>") {
            EXPECT_TRUE(fs::exists(tmp.path() / "registry.db"));
            EXPECT_FALSE(fs::exists(json));
            cleanup();
        } else {
            EXPECT_EQ(slurp(json), body);
            expect_no_registry_files(tmp.path(), code);
            fs::remove(json);
        }
        return code;
    }

    // 导入并打开（调用方检查完再调 cleanup）。
    RegistryDb import_and_open(const std::string& body) {
        write_text(tmp.path() / "worlds.json", body);
        import_worlds_json(tmp.path());
        return reopen(tmp.path());
    }

    void cleanup() {
        for (const char* name : {"registry.db", "registry.db-wal", "registry.db-shm"}) {
            fs::remove(tmp.path() / name);
        }
        for (const auto& copy : imported_copies(tmp.path())) { fs::remove(copy); }
    }
};

TEST_F(RegistryValidation, BaselineImports) {
    EXPECT_EQ(import_code_of(good()), "<no throw>");
}

TEST_F(RegistryValidation, RejectsInvalidWorldKey) {
    EXPECT_EQ(import_code_of(text("Bad Name", home + "/Bad Name/world.db",
                                  home + "/Bad Name/snapshots", "manual",
                                  home + "/Bad Name/snapshots/s1.db", "Bad Name")),
              "registry_corrupt");
    EXPECT_EQ(import_code_of(text("..", home + "/../world.db", home + "/../snapshots", "manual",
                                  home + "/../snapshots/s1.db", "..")),
              "registry_corrupt");
}

TEST_F(RegistryValidation, RejectsRelativePaths) {
    EXPECT_EQ(import_code_of(text("sim-1", "worlds/sim-1/world.db", home + "/sim-1/snapshots",
                                  "manual", home + "/sim-1/snapshots/s1.db", "sim-1")),
              "registry_corrupt");
    EXPECT_EQ(import_code_of(text("sim-1", home + "/sim-1/world.db", "snapshots", "manual",
                                  "snapshots/s1.db", "sim-1")),
              "registry_corrupt");
    // default 的库路径同样必须是绝对路径（它不受 worlds/ 布局约束，但也不能是相对的）。
    std::string rel_default = good();
    const std::string abs_db = (tmp.path() / "dashboard.db").string();
    rel_default.replace(rel_default.find(abs_db), abs_db.size(), "dashboard.db");
    EXPECT_EQ(import_code_of(rel_default), "registry_corrupt");
}

TEST_F(RegistryValidation, RejectsWorldPathsOutsideItsHome) {
    const TempDir outside;
    EXPECT_EQ(import_code_of(text("sim-1", (outside.path() / "world.db").string(),
                                  home + "/sim-1/snapshots", "manual",
                                  home + "/sim-1/snapshots/s1.db", "sim-1")),
              "registry_corrupt");
    EXPECT_EQ(import_code_of(text("sim-1", home + "/sim-1/world.db",
                                  (outside.path() / "snaps").string(), "manual",
                                  (outside.path() / "snaps" / "s1.db").string(), "sim-1")),
              "registry_corrupt");
    // 用 .. 绕出去也不行：字面规范化之后再比。
    EXPECT_EQ(import_code_of(text("sim-1", home + "/sim-1/../other/world.db",
                                  home + "/sim-1/snapshots", "manual",
                                  home + "/sim-1/snapshots/s1.db", "sim-1")),
              "registry_corrupt");
    // 另一个世界的目录也不算自己的家。
    EXPECT_EQ(import_code_of(text("sim-1", home + "/sim-2/world.db", home + "/sim-1/snapshots",
                                  "manual", home + "/sim-1/snapshots/s1.db", "sim-1")),
              "registry_corrupt");
}

TEST_F(RegistryValidation, RejectsDefaultSnapshotDirOutsideItsHome) {
    // default 的库保持原地，但它的快照目录同样固定在 worlds/default/ 下：否则把它改成 "/"，
    // 删除快照 / 保留策略 / 残留清扫就能删到任意位置。
    std::string bad = good();
    const std::string dflt_snapdir = home + "/default/snapshots";
    bad.replace(bad.find(dflt_snapdir), dflt_snapdir.size(), "/");
    EXPECT_EQ(import_code_of(bad), "registry_corrupt");
}

TEST_F(RegistryValidation, RejectsUnknownSnapshotKind) {
    EXPECT_EQ(import_code_of(text("sim-1", home + "/sim-1/world.db", home + "/sim-1/snapshots",
                                  "Auto", home + "/sim-1/snapshots/s1.db", "sim-1")),
              "registry_corrupt");
}

TEST_F(RegistryValidation, RejectsSnapshotPathOutsideItsWorldsSnapshotDir) {
    const TempDir outside;
    EXPECT_EQ(import_code_of(text("sim-1", home + "/sim-1/world.db", home + "/sim-1/snapshots",
                                  "manual", (outside.path() / "precious.db").string(), "sim-1")),
              "registry_corrupt");
    // 挂在别的世界的快照目录下也不行。
    EXPECT_EQ(import_code_of(text("sim-1", home + "/sim-1/world.db", home + "/sim-1/snapshots",
                                  "manual", home + "/default/snapshots/s1.db", "sim-1")),
              "registry_corrupt");
}

TEST_F(RegistryValidation, RejectsSnapshotsOfUnregisteredWorld) {
    EXPECT_EQ(import_code_of(text("sim-1", home + "/sim-1/world.db", home + "/sim-1/snapshots",
                                  "manual", home + "/ghost/snapshots/s1.db", "ghost")),
              "registry_corrupt");
}

// 布局规则在解析符号链接之后判断：worlds/sim-1/snapshots 是指到外部的符号链接，导入拒绝。
TEST_F(RegistryValidation, RejectsSnapshotsDirSymlinkedOutside) {
    const TempDir outside;
    fs::create_directories(tmp.path() / "worlds" / "sim-1");
    fs::create_directory_symlink(outside.path(), tmp.path() / "worlds" / "sim-1" / "snapshots");
    write_text(tmp.path() / "worlds.json", good());
    const std::string what = what_of([&] { import_worlds_json(tmp.path()); });
    EXPECT_NE(what.find("snapshot_dir of 'sim-1'"), std::string::npos) << what;
    EXPECT_NE(what.find("move the whole registry directory"), std::string::npos) << what;
    EXPECT_EQ(import_code_of(good()), "registry_corrupt");
}

// 旧注册表里缺 auto_snapshot 时补的默认值（default {false, 24}，其余 {true, 0}）明确写进各自的
// settings_json：导入的世界保持原来的行为，不落到新设置的默认值 {false, 24} 上。
TEST_F(RegistryValidation, LegacyRegistryWithoutAutoSnapshotGetsPerWorldDefaults) {
    ASSERT_EQ(good().find("auto_snapshot"), std::string::npos);
    {
        const auto reg = import_and_open(good());
        EXPECT_EQ(auto_of(reg, "default"), (AutoSnapshotConfig{.on_session = false, .interval_hours = 24}));
        EXPECT_EQ(auto_of(reg, "sim-1"), (AutoSnapshotConfig{.on_session = true, .interval_hours = 0}));
        // W2/W3 的快照条目照常导入，新字段都是缺省。
        const auto snap = reg.snapshots("sim-1").front();
        EXPECT_FALSE(snap.sessions_complete.has_value());
        EXPECT_TRUE(snap.session_id.empty());
        EXPECT_TRUE(snap.content_mark.empty());
        EXPECT_EQ(snap.size_bytes, 1);
    }
    cleanup();
}

// 字段存在时用存的值，不被默认值覆盖（两个世界都写成与各自默认相反的值）。
TEST_F(RegistryValidation, ExplicitAutoSnapshotIsKeptAsWritten) {
    std::string body = good();
    const std::string skey = R"("tenant":"sim-1",)";
    body.replace(body.find(skey), skey.size(),
                 skey + R"("auto_snapshot":{"on_session":false,"interval_hours":8760},)");
    const std::string dkey = R"("tenant":"default",)";
    body.replace(body.find(dkey), dkey.size(),
                 dkey + R"("auto_snapshot":{"on_session":true,"interval_hours":0},)");
    {
        const auto reg = import_and_open(body);
        EXPECT_EQ(auto_of(reg, "default"), (AutoSnapshotConfig{.on_session = true, .interval_hours = 0}));
        EXPECT_EQ(auto_of(reg, "sim-1"), (AutoSnapshotConfig{.on_session = false, .interval_hours = 8760}));
    }
    cleanup();
}

// 往基线里 sim-1 的世界条目加一个 auto_snapshot 字段（原样的 JSON 文本）。
std::string with_sim_auto(std::string body, const std::string& value) {
    const std::string key = R"("tenant":"sim-1",)";
    const auto pos = body.find(key);
    EXPECT_NE(pos, std::string::npos);
    body.replace(pos, key.size(), key + R"("auto_snapshot":)" + value + ",");
    return body;
}

// 把基线快照条目的 kind/reason 换掉，extra 追加在 reason 之后（例如新字段）。
std::string with_snap_fields(std::string body, const std::string& kind, const std::string& reason,
                             const std::string& extra = "") {
    const std::string key = R"("kind":"manual","reason":"manual",)";
    const auto pos = body.find(key);
    EXPECT_NE(pos, std::string::npos);
    body.replace(pos, key.size(), R"("kind":")" + kind + R"(","reason":")" + reason + R"(",)" + extra);
    return body;
}

TEST_F(RegistryValidation, RejectsMalformedAutoSnapshot) {
    for (const char* bad : {
             "5", "[]", "null", R"("on")",
             R"({"on_session":1,"interval_hours":0})",
             R"({"on_session":"true","interval_hours":0})",
             R"({"on_session":true,"interval_hours":"24"})",
             R"({"on_session":true,"interval_hours":1.5})",
             R"({"on_session":true,"interval_hours":true})",
             R"({"on_session":true,"interval_hours":-1})",
             R"({"on_session":true,"interval_hours":8761})",
             R"({"on_session":true,"interval_hours":18446744073709551615})",
             R"({"on_session":true})",
             R"({"interval_hours":24})",
         }) {
        EXPECT_EQ(import_code_of(with_sim_auto(good(), bad)), "registry_corrupt")
            << "auto_snapshot=" << bad;
    }
    for (const char* fine : {R"({"on_session":false,"interval_hours":0})",
                             R"({"on_session":true,"interval_hours":8760})"}) {
        EXPECT_EQ(import_code_of(with_sim_auto(good(), fine)), "<no throw>")
            << "auto_snapshot=" << fine;
    }
}

TEST_F(RegistryValidation, RejectsKindReasonMismatch) {
    for (const auto& [kind, reason] : std::vector<std::pair<std::string, std::string>>{
             {"manual", "session"}, {"manual", "interval"}, {"manual", ""},
             {"auto", "manual"}, {"auto", "scene_boundary"}, {"auto", ""}}) {
        EXPECT_EQ(import_code_of(with_snap_fields(good(), kind, reason)), "registry_corrupt")
            << kind << "/" << reason;
    }
    for (const char* reason : {"session", "interval"}) {
        EXPECT_EQ(import_code_of(with_snap_fields(good(), "auto", reason)), "<no throw>")
            << "auto/" << reason;
    }
    // 新字段类型不对同样是损坏。
    for (const char* extra : {R"("sessions_complete":"3",)", R"("session_id":5,)",
                              R"("content_mark":[],)", R"("sessions_complete":2.5,)"}) {
        EXPECT_EQ(import_code_of(with_snap_fields(good(), "auto", "session", extra)),
                  "registry_corrupt") << extra;
    }
    // 三个新字段为 null 都按缺省读（旧绑定层的 dict 可能带 None）。
    {
        const auto reg = import_and_open(with_snap_fields(
            good(), "auto", "session",
            R"("sessions_complete":null,"session_id":null,"content_mark":null,)"));
        const auto nulls = reg.snapshots("sim-1").front();
        EXPECT_FALSE(nulls.sessions_complete);
        EXPECT_TRUE(nulls.session_id.empty());
        EXPECT_TRUE(nulls.content_mark.empty());
    }
    cleanup();
    {
        const auto reg = import_and_open(with_snap_fields(
            good(), "auto", "session",
            R"("sessions_complete":4,"session_id":"s-9","content_mark":"e1:1|-|-",)"));
        const auto full = reg.snapshots("sim-1").front();
        EXPECT_EQ(full.sessions_complete, std::optional<std::int64_t>{4});
        EXPECT_EQ(full.session_id, "s-9");
        EXPECT_EQ(full.content_mark, "e1:1|-|-");
    }
    cleanup();
}

}  // namespace starling::governance
