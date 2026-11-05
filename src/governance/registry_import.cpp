// registry_import.cpp — 旧注册表 worlds.json 一次性导入 registry.db（见 registry_db.hpp
// import_worlds_json；设计稿 docs/superpowers/specs/2026-11-03-memory-service-design.md §5.4）。
//
// 旧格式的解析与校验从 world_store.cpp 搬到这里（裁定 R7），只给导入用；布局规则与 validate_world
// 是同一份代码（world_store_internal.hpp 的 check_*）。

#include "starling/governance/registry_db.hpp"

#include "registry_db_internal.hpp"
#include "world_store_internal.hpp"

#include "starling/governance/world_settings.hpp"
#include "starling/governance/world_store.hpp"

#include <nlohmann/json.hpp>

#include <fcntl.h>
#include <unistd.h>

#include <cerrno>
#include <cstdint>
#include <cstring>
#include <exception>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <map>
#include <optional>
#include <string>
#include <system_error>
#include <utility>
#include <vector>

namespace starling::governance {

namespace {

namespace fs = std::filesystem;
using Json = nlohmann::json;

constexpr std::int64_t kMaxIntervalHours = 8760;

[[noreturn]] void corrupt(const std::string& detail) {
    throw WorldStoreError("registry_corrupt", detail);
}

// worlds.json 里的一个世界（旧格式的 WorldEntry）。
struct LegacyWorld {
    std::string tenant;
    std::string db_path;
    std::string snapshot_dir;
    std::string label;
    std::string created_at;
    std::optional<AutoSnapshotConfig> auto_snapshot;   // 字段缺失时为空，按世界补默认值
};

struct LegacyRegistry {
    std::string default_world = "default";
    int snapshot_retention = 5;
    std::map<std::string, LegacyWorld> worlds;                       // 按 name 排序
    std::map<std::string, std::vector<SnapshotEntry>> snapshots;    // 最新在前
};

// ---- JSON 映射（缺字段用默认值；类型不符由 nlohmann 抛 Json::exception，统一转 registry_corrupt） ----

// W4 的可选字符串字段：缺失或 null 读成空串，其余非字符串是损坏（由 get 抛 type_error）。
std::string optional_text(const Json& node, const char* key) {
    const auto found = node.find(key);
    if (found == node.end() || found->is_null()) { return {}; }
    return found->get<std::string>();
}

SnapshotEntry snapshot_from_json(const Json& node) {
    if (!node.is_object()) { corrupt("snapshot entry is not an object"); }
    SnapshotEntry snap;
    snap.id = node.at("id").get<std::string>();
    snap.path = node.at("path").get<std::string>();
    snap.kind = node.at("kind").get<std::string>();
    snap.reason = node.value("reason", std::string{});
    snap.note = node.value("note", std::string{});
    snap.created_at = node.value("created_at", std::string{});
    snap.world_time = node.value("world_time", std::string{});
    snap.size_bytes = node.value("size_bytes", std::int64_t{0});
    // W4 字段，读取都是可选的，null 按缺省。sessions_complete 必须是整数（不接受 3.5 被截成 3）。
    if (const auto done = node.find("sessions_complete");
        done != node.end() && !done->is_null()) {
        if (!done->is_number_integer()) {
            corrupt("sessions_complete of snapshot '" + snap.id + "' is not an integer");
        }
        snap.sessions_complete = done->get<std::int64_t>();
    }
    snap.session_id = optional_text(node, "session_id");
    snap.content_mark = optional_text(node, "content_mark");
    return snap;
}

// 注册表里缺 auto_snapshot 时的默认值（用户裁定 2026-10-28）：默认世界是日常在用的连续部署，
// 按时间每 24 小时；其余世界按会话。
AutoSnapshotConfig default_auto_snapshot(bool is_default_world) {
    return is_default_world ? AutoSnapshotConfig{.on_session = false, .interval_hours = 24}
                            : AutoSnapshotConfig{.on_session = true, .interval_hours = 0};
}

// 只认 {"on_session": bool, "interval_hours": 整数}：两个键都必须在（旧代码保存时总是写全），
// 类型不对就是损坏。范围在校验那一步查（check_auto_snapshot）。
AutoSnapshotConfig auto_snapshot_from_json(const std::string& name, const Json& node) {
    const std::string where = "auto_snapshot of '" + name + "'";
    if (!node.is_object()) { corrupt(where + " is not an object"); }
    const auto flag = node.find("on_session");
    if (flag == node.end() || !flag->is_boolean()) {
        corrupt(where + ": on_session is missing or not a boolean");
    }
    const auto hours = node.find("interval_hours");
    if (hours == node.end() || !hours->is_number_integer()) {
        corrupt(where + ": interval_hours is missing or not an integer");
    }
    // 大于 int64 的无符号数直接拒绝，不让 get<int64_t> 回绕成负数。
    if (hours->is_number_unsigned() &&
        hours->get<std::uint64_t>() > static_cast<std::uint64_t>(kMaxIntervalHours)) {
        corrupt(where + ": interval_hours is out of range 0-8760");
    }
    return AutoSnapshotConfig{.on_session = flag->get<bool>(),
                              .interval_hours = hours->get<std::int64_t>(),};
}

LegacyWorld world_from_json(const std::string& name, const Json& node) {
    if (!node.is_object()) { corrupt("world '" + name + "' is not an object"); }
    LegacyWorld world;
    world.tenant = node.at("tenant").get<std::string>();
    world.db_path = node.at("db_path").get<std::string>();
    world.snapshot_dir = node.at("snapshot_dir").get<std::string>();
    world.label = node.value("label", std::string{});
    world.created_at = node.value("created_at", std::string{});
    if (const auto cfg = node.find("auto_snapshot"); cfg != node.end()) {
        world.auto_snapshot = auto_snapshot_from_json(name, *cfg);
    }
    return world;
}

LegacyRegistry registry_from_node(const Json& root) {
    if (!root.is_object()) { corrupt("top-level value is not an object"); }
    LegacyRegistry reg;
    (void)root.value("version", 1);   // 类型不对照样是损坏
    reg.default_world = root.value("default", std::string{"default"});
    // "active"（活动世界）的概念已去掉：照常读出来（类型不对是损坏），不再使用，也不要求它已登记。
    (void)root.value("active", std::string{"default"});
    reg.snapshot_retention = root.value("snapshot_retention", 5);

    const auto worlds = root.find("worlds");
    if (worlds == root.end() || !worlds->is_object()) {
        corrupt("'worlds' is missing or not an object");
    }
    for (const auto& [name, node] : worlds->items()) {
        reg.worlds.emplace(name, world_from_json(name, node));
    }

    const auto snapshots = root.find("snapshots");
    if (snapshots != root.end()) {
        if (!snapshots->is_object()) { corrupt("'snapshots' is not an object"); }
        for (const auto& [name, list] : snapshots->items()) {
            if (!list.is_array()) { corrupt("snapshots of '" + name + "' is not an array"); }
            auto& dest = reg.snapshots[name];
            for (const auto& node : list) { dest.push_back(snapshot_from_json(node)); }
        }
    }
    return reg;
}

// 旧 load_registry 的全部校验，顺序不变：先是与目录无关的一层（validate_shape），再是布局
// （validate_layout，世界目录 = <data_dir>/worlds/<name>）。retention 放在路径检查之前：retention
// 非法的注册表报 invalid_retention（导入时再转成 registry_corrupt），不被路径问题盖掉。
void validate_legacy(const LegacyRegistry& reg, const fs::path& data_dir) {
    // RegistryDb 只认名为 "default" 的默认世界（RegistryDb::open 与 remove 都按这个名字）。
    if (reg.default_world != "default") {
        corrupt("default world is '" + reg.default_world + "', expected 'default'");
    }
    if (!reg.worlds.contains(reg.default_world)) {
        corrupt("default world '" + reg.default_world + "' is not registered");
    }
    internal::require_valid_retention(reg.snapshot_retention);
    for (const auto& [name, world] : reg.worlds) {
        const bool is_default = name == reg.default_world;
        internal::check_world_shape(name, name, world.db_path, world.snapshot_dir, is_default);
        if (world.auto_snapshot) { internal::check_auto_snapshot(name, *world.auto_snapshot); }
    }
    for (const auto& [owner, list] : reg.snapshots) {
        const auto found = reg.worlds.find(owner);
        if (found == reg.worlds.end()) {
            corrupt("snapshots listed for unregistered world '" + owner + "'");
        }
        for (const auto& snap : list) {
            internal::check_snapshot_entry(owner, found->second.snapshot_dir, snap);
        }
    }
    for (const auto& [name, world] : reg.worlds) {
        internal::check_world_layout(data_dir, name, name, world.db_path, world.snapshot_dir,
                                     name == reg.default_world);
    }
}

LegacyRegistry read_legacy(const fs::path& json_path, const fs::path& data_dir) {
    std::ifstream file(json_path, std::ios::binary);
    if (!file) { corrupt("cannot open " + json_path.string()); }
    const std::string text{std::istreambuf_iterator<char>(file), std::istreambuf_iterator<char>()};
    if (file.bad()) { corrupt("cannot read " + json_path.string()); }
    LegacyRegistry reg;
    try {
        reg = registry_from_node(Json::parse(text));
    } catch (const Json::exception& err) {
        corrupt(json_path.string() + ": " + err.what());
    }
    validate_legacy(reg, data_dir);
    return reg;
}

// 每个世界的设置：默认设置，background.auto_snapshot 取 worlds.json 的值（缺字段时补旧的默认值）。
// 必须写明：新设置的默认值是 {false, 24}，不写的话非默认世界会从按会话变成按时间。
std::string imported_settings(const LegacyWorld& world, bool is_default) {
    WorldSettings settings;
    settings.background.auto_snapshot = world.auto_snapshot.value_or(default_auto_snapshot(is_default));
    return settings_to_json(settings);
}

std::vector<internal::ImportedWorld> to_imported(const LegacyRegistry& reg) {
    std::vector<internal::ImportedWorld> out;
    for (const auto& [name, world] : reg.worlds) {
        const bool is_default = name == reg.default_world;
        internal::ImportedWorld item;
        item.record.name = name;
        item.record.dir_id = name;   // 导入的世界保留原目录名（默认世界即 "default"）
        item.record.tenant = world.tenant;
        item.record.db_path = world.db_path;
        item.record.snapshot_dir = world.snapshot_dir;
        item.record.label = world.label;
        item.record.created_at = world.created_at;
        item.record.settings_json = imported_settings(world, is_default);
        if (const auto listed = reg.snapshots.find(name); listed != reg.snapshots.end()) {
            item.snapshots = listed->second;
        }
        out.push_back(std::move(item));
    }
    return out;
}

// registry.db.importing 与它的 SQLite 旁车。
std::vector<fs::path> importing_files(const fs::path& importing) {
    std::vector<fs::path> out{importing};
    for (const char* suffix : {"-wal", "-shm", "-journal"}) {
        out.emplace_back(importing.string() + suffix);
    }
    return out;
}

// 删掉 .importing 与旁车。throw_on_error=false 时用在失败路径上：尽力删，删不掉的记进返回的说明。
std::string remove_importing(const fs::path& importing, bool throw_on_error) {
    std::string why;
    for (const auto& file : importing_files(importing)) {
        std::error_code err;
        fs::remove(file, err);
        if (!err) { continue; }
        if (throw_on_error) {
            corrupt("cannot remove stale " + file.string() + ": " + err.message());
        }
        why += "; cannot remove " + file.string() + ": " + err.message();
    }
    return why;
}

// worlds.json 改名后的位置：worlds.json.imported-<compact_now>，被占用就加 -<4 hex> 重抽。
fs::path imported_name(const fs::path& json_path) {
    const std::string base = json_path.string() + ".imported-" + internal::compact_now();
    fs::path cand = base;
    std::error_code err;
    while (fs::exists(fs::symlink_status(cand, err))) {
        cand = base + "-" + internal::random_hex().substr(0, 4);
    }
    return cand;
}

// 把目录项的改动（rename）落盘（审查 T2-1）：以只读打开目录、fsync、关闭。macOS 上对目录 fd 的普通 fsync 即可。
// 成功返回空串，失败返回原因（errno 文本）。
std::string fsync_dir(const fs::path& dir) {
    // NOLINTNEXTLINE(cppcoreguidelines-pro-type-vararg,hicpp-vararg) — POSIX open 是变参函数
    const int dir_fd = ::open(dir.c_str(), O_RDONLY);
    if (dir_fd < 0) { return std::string("open: ") + std::strerror(errno); }
    std::string failure;
    if (::fsync(dir_fd) != 0) { failure = std::string("fsync: ") + std::strerror(errno); }
    (void)::close(dir_fd);
    return failure;
}

bool present(const fs::path& path) {
    std::error_code err;
    return fs::exists(fs::symlink_status(path, err));
}

}  // namespace

void import_worlds_json(const fs::path& data_dir) {
    const fs::path root = fs::absolute(data_dir);
    const fs::path registry = root / "registry.db";
    const fs::path json_path = root / "worlds.json";
    if (present(registry) || !present(json_path)) { return; }

    // 1–2. 读与校验：只读，失败时什么都还没建。
    std::vector<internal::ImportedWorld> worlds;
    std::string default_world;
    int retention = 0;
    try {
        const LegacyRegistry legacy = read_legacy(json_path, root);
        worlds = to_imported(legacy);
        default_world = legacy.default_world;
        retention = legacy.snapshot_retention;
    } catch (const WorldStoreError& err) {
        if (err.code() == "registry_corrupt") { throw; }
        corrupt(json_path.string() + ": " + err.what());   // 例如 invalid_retention
    }

    // 3–4. 在 .importing 里建库，改名为 registry.db。失败时删掉 .importing，worlds.json 不动。
    const fs::path importing = root / "registry.db.importing";
    (void)remove_importing(importing, true);   // 上次崩溃的残留
    try {
        internal::write_fresh_registry(importing, default_world, retention, worlds);
        fs::rename(importing, registry);
    } catch (const std::exception& err) {
        const std::string leftover = remove_importing(importing, false);
        const std::string detail = "cannot import " + json_path.string() + ": " + err.what() +
                                   leftover + "; " + json_path.string() + " is unchanged";
        corrupt(detail);
    }

    // 4 之后、5 之前把数据目录 fsync（审查 T2-1）：先让 registry.db 的改名落盘，再改名 worlds.json。否则断电时
    // 两个改名可能只留下后一个——registry.db 不在、worlds.json 也不在，下次启动建一个只有默认世界的新注册表，
    // 随后的快照清扫把没登记的快照文件全删掉。fsync 失败按第 5 步失败处理：registry.db 已就位，worlds.json 留在
    // 原处（下次启动见到 registry.db 就什么都不做，留着的 worlds.json 无害）。
    if (const std::string failure = fsync_dir(root); !failure.empty()) {
        corrupt("imported " + json_path.string() + " into " + registry.string() +
                ", but could not sync " + root.string() + " (" + failure + "); " +
                json_path.string() + " was left in place and is ignored from now on");
    }

    // 5. 原文件改名保留。失败时 registry.db 已就位（下次调用见到它就什么都不做）。
    try {
        fs::rename(json_path, imported_name(json_path));
    } catch (const std::exception& err) {
        corrupt("imported " + json_path.string() + " into " + registry.string() +
                ", but could not rename it: " + err.what() +
                "; registry.db is in use from now on and the old file is ignored");
    }
    // 第 5 步的改名同样落盘：之后的启动不再见到 worlds.json。失败只影响这一个改名能否留下，registry.db 已经落盘，
    // 不报错（下次启动见到 registry.db 就什么都不做）。
    (void)fsync_dir(root);
}

}  // namespace starling::governance
