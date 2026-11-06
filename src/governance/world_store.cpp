#include "starling/governance/world_store.hpp"

#include "world_store_internal.hpp"

#include "starling/governance/registry_db.hpp"
#include "starling/governance/world_settings.hpp"
#include "starling/memory/memory_digest.hpp"
#include "starling/persistence/connection.hpp"
#include "starling/persistence/migration_runner.hpp"
#include "starling/persistence/sqlite_adapter.hpp"
#include "starling/persistence/sqlite_handles.hpp"
#include "starling/persistence/stmt.hpp"

#include <sqlite3.h>

#include <algorithm>
#include <array>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <ctime>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <optional>
#include <random>
#include <set>
#include <sstream>
#include <stdexcept>
#include <system_error>
#include <utility>

namespace starling::governance {

namespace fs = std::filesystem;

WorldStoreError::WorldStoreError(std::string code, const std::string& detail)
    : std::runtime_error(code + ": " + detail), code_(std::move(code)) {}
const std::string& WorldStoreError::code() const noexcept { return code_; }
WorldStoreError WorldStoreError::after_replace(std::string code, const std::string& detail) {
    WorldStoreError err(std::move(code), detail);
    err.replaced_ = true;
    return err;
}

bool is_valid_world_name(std::string_view name) {
    // 世界名：首字符字母或数字，其余字母、数字、'.'、'_'、'-'，长度 1–128。
    // 磁盘上的目录用 dir_id，不用世界名，所以大小写与 '.' 都安全。
    constexpr std::size_t kMaxNameLength = 128;
    if (name.empty() || name.size() > kMaxNameLength) { return false; }
    const auto alnum = [](char chr) {
        return (chr >= 'a' && chr <= 'z') || (chr >= 'A' && chr <= 'Z') ||
               (chr >= '0' && chr <= '9');
    };
    if (!alnum(name.front())) { return false; }
    return std::ranges::all_of(name, [&alnum](char chr) {
        return alnum(chr) || chr == '.' || chr == '_' || chr == '-';
    });
}

namespace {

// ---- 时间 ----------------------------------------------------------------

std::string format_utc_now(const char* pattern) {
    const std::time_t now = std::time(nullptr);
    std::tm parts{};
    if (gmtime_r(&now, &parts) == nullptr) {
        throw std::runtime_error("gmtime_r failed");
    }
    std::array<char, 32> buf{};
    const std::size_t len = std::strftime(buf.data(), buf.size(), pattern, &parts);
    if (len == 0) { throw std::runtime_error("strftime failed"); }
    return {buf.data(), len};
}

// 2026-10-25T01:12:00Z
std::string iso_now() { return format_utc_now("%Y-%m-%dT%H:%M:%SZ"); }
// 20261025T011200Z（快照 id 前缀）
std::string compact_now() { return format_utc_now("%Y%m%dT%H%M%SZ"); }

// 严格解析 YYYY-MM-DDTHH:MM:SSZ（iso_now 与 Python _now_iso 的格式）。日期必须真实存在
// （2030-02-30 不行），时分秒在 0–23 / 0–59 / 0–59 之内。别的写法一律解析不了。
std::optional<std::chrono::sys_seconds> parse_utc_seconds(std::string_view text) {
    constexpr std::string_view kShape = "dddd-dd-ddTdd:dd:ddZ";
    if (text.size() != kShape.size()) { return std::nullopt; }
    for (std::size_t pos = 0; pos < kShape.size(); ++pos) {
        const char want = kShape.at(pos);
        const char chr = text.at(pos);
        const bool good = want == 'd' ? (chr >= '0' && chr <= '9') : chr == want;
        if (!good) { return std::nullopt; }
    }
    const auto number = [text](std::size_t from, std::size_t len) {
        int value = 0;
        for (std::size_t pos = from; pos < from + len; ++pos) {
            value = (value * 10) + (text.at(pos) - '0');
        }
        return value;
    };
    const int hour = number(11, 2);
    const int minute = number(14, 2);
    const int second = number(17, 2);
    if (hour > 23 || minute > 59 || second > 59) { return std::nullopt; }
    const std::chrono::year_month_day date{std::chrono::year{number(0, 4)},
                                           std::chrono::month{static_cast<unsigned>(number(5, 2))},
                                           std::chrono::day{static_cast<unsigned>(number(8, 2))}};
    if (!date.ok()) { return std::nullopt; }
    return std::chrono::sys_days{date} + std::chrono::hours{hour} + std::chrono::minutes{minute} +
           std::chrono::seconds{second};
}

// 已校验的 2030-01-03T04:05:06Z → 20300103T040506Z（与 compact_now 同一形状）。
std::string compact_of(std::string_view iso) {
    std::string out;
    out.reserve(iso.size());
    for (const char chr : iso) {
        if (chr != '-' && chr != ':') { out += chr; }
    }
    return out;
}

// ---- 路径 ----------------------------------------------------------------

// lexically_normal 之后去掉末尾的分隔符（"/a/b/" → "/a/b"），否则 lexically_relative 会把
// 末尾的空文件名当成一级目录。根目录保持原样。
fs::path normalized(const fs::path& path) {
    fs::path out = path.lexically_normal();
    if (!out.has_filename() && out.has_parent_path() && out != out.root_path()) {
        out = out.parent_path();
    }
    return out;
}

// target 严格位于 base 之下（两者都已规范化；相等不算）。
bool strictly_under(const fs::path& base, const fs::path& target) {
    const fs::path rel = target.lexically_relative(base);
    return !rel.empty() && rel != "." && *rel.begin() != "..";
}

// 字面判断，不解析符号链接。这是字面预检：形状校验（快照路径在 snapshot_dir 之下）与世界名推出的
// home 的字面检查用它；布局校验另外解析符号链接之后再判断（见 require_resolved_within）。
bool lexically_within(const fs::path& base, const fs::path& target) {
    return strictly_under(normalized(base), normalized(target));
}

// 仅当 target 严格位于 base 之下（解析符号链接与 ".." 之后）才递归删除。
// 返回 true 表示已执行删除；确认 target 越界（被篡改的注册表、指向外部的
// 符号链接）时返回 false，什么都不碰。路径解析出错（EACCES、ELOOP 等）时无法
// 判断是否越界，删除本身失败（EACCES、EBUSY 等）时文件仍在盘上，两种情况都抛
// snapshot_failed（不抛裸 filesystem_error，上层按 code 映射），由调用方保留
// 注册项，数据不会被悄悄遗弃在磁盘上。
// NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
bool remove_within(const fs::path& base, const fs::path& target) {
    std::error_code err;
    const fs::path real_base = fs::weakly_canonical(base, err);
    if (err) {
        throw WorldStoreError("snapshot_failed",
                              "cannot resolve " + base.string() + ": " + err.message());
    }
    const fs::path real_target = fs::weakly_canonical(target, err);
    if (err) {
        throw WorldStoreError("snapshot_failed",
                              "cannot resolve " + target.string() + ": " + err.message());
    }
    if (!strictly_under(real_base, real_target)) { return false; }
    (void)fs::remove_all(real_target, err);
    if (err) {
        throw WorldStoreError("snapshot_failed",
                              "cannot remove " + real_target.string() + ": " + err.message());
    }
    return true;
}

// ---- 注册表不变式 ---------------------------------------------------------

[[noreturn]] void corrupt(const std::string& detail) {
    throw WorldStoreError("registry_corrupt", detail);
}

constexpr std::int64_t kMaxIntervalHours = 8760;   // 一年

bool interval_in_range(std::int64_t hours) { return hours >= 0 && hours <= kMaxIntervalHours; }

// 保留份数 < 2 会让修剪删掉唯一的回滚点。注册表校验、导入与快照代码共用。
void require_valid_retention(int retention) {
    if (retention < 2) {
        throw WorldStoreError("invalid_retention",
                              "snapshot_retention must be >= 2, got " +
                                  std::to_string(retention));
    }
}

// kind 与 reason 配对：manual/manual，auto/session 或 auto/interval，held/held。注册表校验
// （validate_world、导入）与 take_snapshot 的入参校验共用。
bool kind_reason_paired(const std::string& kind, const std::string& reason) {
    if (kind == "manual") { return reason == "manual"; }
    if (kind == "auto") { return reason == "session" || reason == "interval"; }
    if (kind == "held") { return reason == "held"; }
    return false;
}

bool is_snapshot_kind(const std::string& kind) {
    return kind == "manual" || kind == "auto" || kind == "held";
}

// 参与保留份数修剪的快照（held 由调用方持有，不参与，spec §7.1）。顺序不变。
std::vector<SnapshotEntry> prunable(std::vector<SnapshotEntry> listed) {
    std::erase_if(listed, [](const SnapshotEntry& snap) { return snap.kind == "held"; });
    return listed;
}

// 布局校验用的解析：跟随符号链接之后的绝对位置（已规范化）。末端是悬空的符号链接时也跟到底：
// fs::weakly_canonical 把它当成一个不存在的普通名字原样留下，而 SQLite 以 O_CREAT 打开它会在
// 链接目标处建库（实测），世界库就建到了世界目录之外。中间某一级悬空时，在它下面什么都建不
// 出来（mkdir 报 EEXIST、open 报 ENOENT），weakly_canonical 的字面结果够用。
// 也就是说，中间某一级是悬空链接时只解析末尾，其余部分按字面算。这可能多拒绝一些布局（例如
// worlds/sim-1 悬空地指向 worlds/sim-1-real），但不会让路径越界放行，即 fail closed。
// 解析出错（EACCES、ELOOP 等）时证明不了路径在哪里，按越界处理：抛 code（布局校验是
// registry_corrupt，不经 load 的破坏性入口是 snapshot_failed），带上原因。
[[noreturn]] void unresolvable(const fs::path& path, const std::error_code& err,
                               const std::string& code) {
    throw WorldStoreError(code, "cannot resolve " + path.string() + ": " + err.message());
}

// 去掉末尾的 "/" 与 "."（"a/b/"、"a/b/." → "a/b"）。主要理由：lstat("链接/") 会跟随链接，把末端
// 的链接本身藏起来（悬空时就成了「不存在」），字面结果于是放行；去掉末尾的分隔符，规则才能一致地
// 执行，不取决于注册表里的写法。另外 mkdir("链接/") 在 macOS 上会在链接目标处建目录（实测），但
// take_snapshot 的 create_directories 走不走得到这一步取决于实现（libc++ 可能先在父路径那一级报
// EEXIST），不作为依据。
fs::path without_trailing_dot_or_slash(fs::path path) {
    while (path.has_relative_path() && (path.filename().empty() || path.filename() == ".")) {
        path = path.parent_path();
    }
    return path;
}

// code 是解析出错时抛的码（见 unresolvable）。
fs::path resolve_for_layout(const fs::path& path, const std::string& code = "registry_corrupt") {
    constexpr int kMaxLinkHops = 40;
    fs::path current = path;
    for (int hop = 0; hop < kMaxLinkHops; ++hop) {
        // 每一跳都去：链接内容本身也可以以 "/" 结尾。
        current = without_trailing_dot_or_slash(current);
        std::error_code err;
        const fs::file_status status = fs::symlink_status(current, err);
        // 不存在时 libc++ 也会把 err 设成 ENOENT，不算错误（同 lstat_or_throw）。
        if (err && status.type() != fs::file_type::not_found) { unresolvable(path, err, code); }
        if (status.type() != fs::file_type::symlink) {
            err.clear();
            const fs::path real = fs::weakly_canonical(current, err);
            if (err) { unresolvable(path, err, code); }
            return normalized(real);
        }
        const fs::path link = fs::read_symlink(current, err);
        if (err) { unresolvable(path, err, code); }
        // 相对的链接内容相对于链接所在的目录，与内核解析同义；".." 留给 weakly_canonical 处理。
        current = link.is_absolute() ? link : current.parent_path() / link;
    }
    unresolvable(path, std::make_error_code(std::errc::too_many_symbolic_link_levels), code);
}

// require_resolved_* 拒绝时附在消息末尾的补救提示。违反布局的注册表在启动时（validate_registry、
// 导入 worlds.json）就让服务拒绝启动，消息要让人看得出怎么改。
constexpr std::string_view kLayoutRemedy =
    " (a world directory and the paths inside it must not be symlinks that lead out of it; "
    "to move worlds to another disk, move the whole registry directory)";

// 布局校验：target 解析符号链接之后严格位于 base 解析之后的位置之下，否则 registry_corrupt。
// 两侧都解析，所以同一处换一种写法（/tmp 与 /private/tmp、经符号链接打开的注册表目录）仍然放行。
// 不保留「字面在 base 之下就放行」的捷径：字面上在世界目录里、磁盘上却是指向外部的符号链接时，
// W2 的快照写入、残留清扫、保留策略、删快照以 snapshot_dir 为根，引擎在 db_path 建库，全都会落到
// 世界目录之外（用户裁定 2026-10-27：这种布局不再支持）。".." 越界照样拒绝。
// NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
void require_resolved_within(const fs::path& base, const fs::path& target,
                             const std::string& what) {
    const fs::path real_base = resolve_for_layout(base);
    const fs::path real_target = resolve_for_layout(target);
    if (!strictly_under(real_base, real_target)) {
        corrupt(what + " " + target.string() + " resolves to " + real_target.string() +
                ", outside " + real_base.string() + std::string(kLayoutRemedy));
    }
}

// 世界目录本身：解析符号链接之后必须恰好是「解析后的 worlds/」/<dir_id>，只在 worlds/ 之下不够。
// 否则 worlds/<sim-1> -> worlds/<sim-2>（或 worlds/<sim-2>/sub）能通过，删除 sim-1 经 remove_within
// 解析到 sim-2，删掉兄弟世界的整个目录和库（用户裁定 2026-10-27：每个世界目录解析后都是
// worlds/<它自己的目录名>）。世界目录还不存在时 weakly_canonical 把缺的末尾原样接上，照常通过；
// worlds/ 整体外链到数据目录内、数据目录本身是符号链接，两侧同样解析，仍然放行。
// code 是违反（含解析出错）时抛的码：注册表校验（validate_world、导入）是 registry_corrupt；
// RegistryDb::remove 与 checked_world_home（回滚、列出、清理归档）可以不经校验直接调用，自己也查，
// 抛 snapshot_failed。消息点名 world（世界名；为空时用 dir_id）。
// NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
void require_resolved_home(const fs::path& worlds_root, const std::string& dir_id,
                           const std::string& code, const std::string& world = {}) {
    const fs::path home = worlds_root / dir_id;
    const fs::path expected = resolve_for_layout(worlds_root, code) / dir_id;
    const fs::path real_home = resolve_for_layout(home, code);
    if (real_home != expected) {
        const std::string& label = world.empty() ? dir_id : world;
        throw WorldStoreError(code, "home of '" + label + "' " + home.string() + " resolves to " +
                                        real_home.string() + ", not " + expected.string() +
                                        std::string(kLayoutRemedy));
    }
}

// dir_id 只能是 worlds/ 下的一级目录名。它来自注册表（RegistryDb 生成，或导入时取旧世界名），
// 手改过的 registry.db 可以把它改成 "../x" 或 "a/b"：先验形状，再谈解析。
// NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
void require_plain_dir_id(const std::string& dir_id, const std::string& world,
                          const std::string& code) {
    if (dir_id.empty() || dir_id == "." || dir_id == ".." ||
        dir_id.find('/') != std::string::npos || dir_id.find('\\') != std::string::npos) {
        throw WorldStoreError(code, "invalid dir_id '" + dir_id + "' for world '" + world + "'");
    }
}

// worlds/ 解析符号链接之后必须仍在数据目录之下。逐项比较两侧都解析符号链接，
// 若 worlds/ 本身指向外部，「目标在 home 之下」会在外部成立，快照清扫、保留策略、删快照
// 便以外部目录为根。所以在逐项比较之前先把根钉死：worlds/ 还不存在（全新数据目录）时放行。
// 用 resolve_for_layout 而不是 weakly_canonical：worlds/ 是悬空的符号链接（例如指到没挂上的盘）时，
// weakly_canonical 把它原样留下、字面放行，随后才被世界目录那条规则以「解析到自己」这种让人困惑的
// 消息拒绝；跟到底之后，这里直接报 worlds/ 解析到了哪里。解析出错同样 registry_corrupt。
void require_worlds_root_inside(const fs::path& data_dir, const fs::path& worlds_root) {
    std::error_code err;
    if (!fs::exists(fs::symlink_status(worlds_root, err))) { return; }
    const fs::path real_dir = resolve_for_layout(data_dir);
    const fs::path real_root = resolve_for_layout(worlds_root);
    if (!strictly_under(real_dir, real_root)) {
        corrupt(worlds_root.string() + " resolves to " + real_root.string() + ", outside " +
                real_dir.string());
    }
}

// 与目录布局无关的一层（validate_world 与导入共用）。default 的名字是固定的（"default"），不是用户
// 起的名字，不查。
// NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
void check_world_shape(const std::string& name, const std::string& dir_id,
                       const std::string& db_path, const std::string& snapshot_dir,
                       bool is_default) {
    if (!is_default && !is_valid_world_name(name)) {
        corrupt("world name '" + name + "' does not match [A-Za-z0-9][A-Za-z0-9._-]{0,127}");
    }
    require_plain_dir_id(dir_id, name, "registry_corrupt");
    if (!fs::path(db_path).is_absolute()) {
        corrupt("db_path of '" + name + "' is not absolute: " + db_path);
    }
    if (!fs::path(snapshot_dir).is_absolute()) {
        corrupt("snapshot_dir of '" + name + "' is not absolute: " + snapshot_dir);
    }
}

void check_auto_snapshot(const std::string& name, const AutoSnapshotConfig& config) {
    if (!interval_in_range(config.interval_hours)) {
        corrupt("auto_snapshot of '" + name + "': interval_hours " +
                std::to_string(config.interval_hours) + " is out of range 0-8760");
    }
}

// 世界目录布局（解析符号链接之后判断，见 require_resolved_within）：
//   - 世界目录 <data_dir>/worlds/<dir_id> 解析后恰好是解析后的 worlds/ 下的 <dir_id>
//     （require_resolved_home），不能是别的世界的目录或它的子目录；
//   - 非 default 世界的 db_path、所有世界的 snapshot_dir 严格位于各自的世界目录之下。
// default 的库保持原地（spec §0.1），不受此限；但它的快照目录同样固定在 worlds/default/ 下
// ——否则把它改成 "/"，删除快照、保留策略和残留清扫就能删到任意位置。
// 数据目录本身可以是符号链接：两侧都解析，比的是同一处。
// NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
void check_world_layout(const fs::path& data_dir, const std::string& name,
                        const std::string& dir_id, const std::string& db_path,
                        const std::string& snapshot_dir, bool is_default) {
    const fs::path root = fs::absolute(data_dir);
    const fs::path worlds_root = root / "worlds";
    require_worlds_root_inside(root, worlds_root);
    const fs::path home = worlds_root / dir_id;
    if (!lexically_within(worlds_root, home)) {
        corrupt("world '" + name + "' has no home under " + worlds_root.string());
    }
    require_resolved_home(worlds_root, dir_id, "registry_corrupt", name);
    if (!is_default) {
        require_resolved_within(home, db_path, "db_path of '" + name + "'");
    }
    require_resolved_within(home, snapshot_dir, "snapshot_dir of '" + name + "'");
}

// 一份快照条目的形状：kind / reason 配对，path 字面在所属世界的 snapshot_dir 之下。
// NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
void check_snapshot_entry(const std::string& owner, const std::string& snapshot_dir,
                          const SnapshotEntry& snap) {
    if (!is_snapshot_kind(snap.kind)) {
        corrupt("snapshot '" + snap.id + "' of '" + owner + "' has kind '" + snap.kind + "'");
    }
    if (!kind_reason_paired(snap.kind, snap.reason)) {
        corrupt("snapshot '" + snap.id + "' of '" + owner + "' has kind '" + snap.kind +
                "' with reason '" + snap.reason + "'");
    }
    if (!lexically_within(snapshot_dir, snap.path)) {
        corrupt("snapshot '" + snap.id + "' of '" + owner + "' lies outside " + snapshot_dir +
                ": " + snap.path);
    }
}

// base 解析符号链接之后必须仍在 root 之下（两侧 weakly_canonical），否则 snapshot_failed。
// 回滚、归档、快照路径用它：例如 worlds/ 本身若是指向外部的符号链接，remove_within 两侧都解析到
// 外部，会把外部的目录当成合法子目录删掉。
void require_inside_registry(const fs::path& root, const fs::path& base) {
    std::error_code err;
    const fs::path real_root = fs::weakly_canonical(root, err);
    if (err) {
        throw WorldStoreError("snapshot_failed",
                              "cannot resolve " + root.string() + ": " + err.message());
    }
    const fs::path real_worlds = fs::weakly_canonical(base, err);
    if (err) {
        throw WorldStoreError("snapshot_failed",
                              "cannot resolve " + base.string() + ": " + err.message());
    }
    if (!strictly_under(real_root, real_worlds)) {
        throw WorldStoreError("snapshot_failed", base.string() + " resolves to " +
                                                     real_worlds.string() + ", outside " +
                                                     real_root.string());
    }
}

// 快照 id 的形状：20261025T011200Z-a1b2（compact_now + "-" + 4 位小写十六进制）。
bool looks_like_snapshot_id(std::string_view stem) {
    constexpr std::size_t kIdLength = 21;
    if (stem.size() != kIdLength) { return false; }
    const auto digit = [](char chr) { return chr >= '0' && chr <= '9'; };
    const auto hex = [&digit](char chr) { return digit(chr) || (chr >= 'a' && chr <= 'f'); };
    for (std::size_t pos = 0; pos < kIdLength; ++pos) {
        const char chr = stem.at(pos);
        bool good = false;
        if (pos == 8) { good = chr == 'T'; }
        else if (pos == 15) { good = chr == 'Z'; }
        else if (pos == 16) { good = chr == '-'; }
        else if (pos > 16) { good = hex(chr); }
        else { good = digit(chr); }
        if (!good) { return false; }
    }
    return true;
}

bool ends_with(std::string_view text, std::string_view suffix) {
    return text.size() >= suffix.size() &&
           text.substr(text.size() - suffix.size()) == suffix;
}

// take_snapshot 前清扫上次崩溃的残留（见 world_store.hpp take_snapshot 第 1b 步）。
// 返回删掉的文件数；任何错误只记日志，不外抛。
// listed 是该世界在注册表里登记的快照。
std::size_t sweep_leftovers(const std::vector<SnapshotEntry>& listed, const fs::path& dir) {
    // 按路径和 id 两种方式认「已登记」：任一命中都不删，宁可漏扫也不误删登记过的快照。
    std::set<std::string> referenced;
    std::set<std::string> referenced_ids;
    for (const auto& snap : listed) {
        referenced.insert(normalized(snap.path).string());
        referenced_ids.insert(snap.id);
    }
    std::vector<fs::path> victims;
    std::error_code err;
    fs::directory_iterator iter(dir, err);
    const fs::directory_iterator done;
    for (; !err && iter != done; iter.increment(err)) {
        const fs::directory_entry& item = *iter;
        std::error_code status_err;
        // 只碰普通文件：符号链接（我们从不创建）一概跳过，免得经它删到登记过的快照。
        if (item.is_symlink(status_err) || !item.is_regular_file(status_err)) { continue; }
        const std::string name = item.path().filename().string();
        constexpr std::string_view kPartial = ".db.partial";
        constexpr std::string_view kFinal = ".db";
        if (ends_with(name, kPartial)) {
            if (looks_like_snapshot_id(std::string_view(name).substr(0, name.size() - kPartial.size()))) {
                victims.push_back(item.path());
            }
        } else if (ends_with(name, kFinal)) {
            const std::string stem = name.substr(0, name.size() - kFinal.size());
            if (looks_like_snapshot_id(stem) && !referenced_ids.contains(stem) &&
                !referenced.contains(normalized(item.path()).string())) {
                victims.push_back(item.path());
            }
        }
    }
    std::size_t swept = 0;
    for (const auto& victim : victims) {
        try {
            if (remove_within(dir, victim)) { ++swept; }
        } catch (const WorldStoreError& sweep_err) {
            std::cerr << "[world_store] cannot sweep " << victim.string() << ": "
                      << sweep_err.what() << "\n";
        }
    }
    if (swept > 0) {
        std::cerr << "[world_store] swept " << swept << " leftover snapshot file(s) in "
                  << dir.string() << "\n";
    }
    return swept;
}

// SQL 标识符加双引号（内部的双引号写两遍）。表名来自库文件自己的 sqlite_master。
std::string quote_identifier(const std::string& name) {
    std::string out = "\"";
    for (const char chr : name) {
        out += chr;
        if (chr == '"') { out += '"'; }
    }
    out += '"';
    return out;
}

std::string random_hex() {
    std::mt19937_64 gen{std::random_device{}()};
    std::ostringstream out;
    out << std::hex << std::setw(16) << std::setfill('0') << gen();
    return out.str();
}

// ---- SQLite 只读访问 -------------------------------------------------------

// URI 里 %、?、# 有特殊含义，路径来自注册表（可能被手改），必须百分号编码。
std::string uri_escape_path(const std::string& path) {
    std::string out;
    out.reserve(path.size());
    for (const char chr : path) {
        switch (chr) {
            case '%': out += "%25"; break;
            case '?': out += "%3f"; break;
            case '#': out += "%23"; break;
            default: out += chr; break;
        }
    }
    return out;
}

// 只读打开（裁定 R28）：URI 取自 Connection::read_only_uri，与 Connection::open_read_only 同一条规则（WAL 头、
// 旁边没有 -wal / -shm 的库用 immutable=1，不在库旁边建文件），这里不另拼 URI。打开的都是调用方持着的世界库
// （Python 侧持世界锁或租约），或本进程刚写完、已关闭的 partial / 复制品。
persistence::SqliteHandle open_read_only(const std::string& path) {
    sqlite3* raw = nullptr;
    const std::string uri = persistence::Connection::read_only_uri(path).uri;
    const int code = sqlite3_open_v2(uri.c_str(), &raw, SQLITE_OPEN_READONLY | SQLITE_OPEN_URI,
                                     nullptr);
    persistence::SqliteHandle handle(raw);
    if (code != SQLITE_OK) {
        throw WorldStoreError("snapshot_failed",
                              std::string("open ") + path + ": " +
                                  (raw != nullptr ? sqlite3_errmsg(raw) : "alloc"));
    }
    sqlite3_busy_timeout(raw, 5000);
    return handle;
}

std::string column_text(sqlite3_stmt* stmt, int idx) {
    // NOLINTNEXTLINE(cppcoreguidelines-pro-type-reinterpret-cast)
    const auto* txt = reinterpret_cast<const char*>(sqlite3_column_text(stmt, idx));
    return txt != nullptr ? std::string(txt) : std::string{};
}

std::int64_t single_int(sqlite3* dbh, const char* sql) {
    persistence::Stmt query(dbh, sql);
    if (!query.step_row()) { return 0; }
    return sqlite3_column_int64(query.raw(), 0);
}

bool table_exists(sqlite3* dbh, const std::string& name) {
    persistence::Stmt query(dbh, "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?1");
    query.bind(name);
    return query.step_row();
}

// 活数据量：不含 freelist。生产库 2.4GB 里 97.7% 是 freelist，磁盘守卫看文件
// 大小会误拒。
std::int64_t live_bytes_of(sqlite3* dbh) {
    const std::int64_t pages = single_int(dbh, "PRAGMA page_count");
    const std::int64_t free_pages = single_int(dbh, "PRAGMA freelist_count");
    const std::int64_t page_size = single_int(dbh, "PRAGMA page_size");
    return (pages - free_pages) * page_size;
}

std::int64_t count_tenant_rows(sqlite3* dbh, const char* table, const std::string& tenant) {
    if (!table_exists(dbh, table)) { return 0; }
    persistence::Stmt query(dbh, (std::string("SELECT COUNT(*) FROM ") + table +
                                 " WHERE tenant_id=?1").c_str());
    query.bind(tenant);
    return query.step_row() ? sqlite3_column_int64(query.raw(), 0) : 0;
}

// 统计缓存的两项（裁定 R18）：该租户 statements 的不同 holder 数与最近一次写入时刻。表不存在时
// 0 与空串；MAX 在没有行时是 NULL，column_text 给空串。
void statement_activity_on(sqlite3* dbh, const std::string& tenant, WorldStats& stats) {
    if (!table_exists(dbh, "statements")) { return; }
    persistence::Stmt query(dbh,
        "SELECT COUNT(DISTINCT holder_id), MAX(created_at) FROM statements WHERE tenant_id=?1");
    query.bind(tenant);
    if (!query.step_row()) { return; }
    stats.holders = sqlite3_column_int64(query.raw(), 0);
    stats.last_write_at = column_text(query.raw(), 1);
}

// source_unit_meta 在生产库里不存在（迁移停在 36），先查 sqlite_master，不靠 SQL 报错。
std::string world_time_on(sqlite3* dbh, const std::string& tenant) {
    if (table_exists(dbh, "source_unit_meta")) {
        persistence::Stmt query(dbh,
            "SELECT MAX(observed_at) FROM source_unit_meta "
            "WHERE tenant_id=?1 AND observed_at IS NOT NULL AND observed_at <> ''");
        query.bind(tenant);
        if (query.step_row()) {
            std::string stamp = column_text(query.raw(), 0);
            if (!stamp.empty()) { return stamp; }
        }
    }
    if (table_exists(dbh, "engrams")) {
        persistence::Stmt query(dbh, "SELECT MAX(created_at) FROM engrams WHERE tenant_id=?1");
        query.bind(tenant);
        if (query.step_row()) { return column_text(query.raw(), 0); }
    }
    return {};
}

// 快照时该世界已有的会话数。表不存在（迁移停在 36）记 0。
std::int64_t sessions_on(sqlite3* dbh, const std::string& tenant) {
    if (!table_exists(dbh, "source_unit_meta")) { return 0; }
    persistence::Stmt query(dbh,
        "SELECT COUNT(DISTINCT session_id) FROM source_unit_meta WHERE tenant_id=?1");
    query.bind(tenant);
    return query.step_row() ? sqlite3_column_int64(query.raw(), 0) : 0;
}

// 内容标记 "e<count>:<max_rowid>:<id>|s<count>:<max_rowid>:<id>|u<count>:<max_rowid>:<id>"
// （engrams、statements、source_unit_meta，都按租户过滤；表不存在那段写 "-"）。<id> 是 rowid
// 最大那一行的主键：engrams.id、statements.id、source_unit_meta 的 engram_ref || ':' || clause_id，
// 计数为 0 时为空。这些 id 是随机 UUID，所以只看 count:max_rowid 会漏掉的两种情况也会改变标记：
//   - 删掉 rowid 最大的那一行再插入一行（SQLite 复用那个 rowid）；
//   - 清空租户（clear_memory）后重新摄入到相同的行数（表空了，rowid 从 1 重新分配）。
// 原地更新不改变它，插入与删除会改变。三张表的计数都为 0 时返回空串：库里没有这个世界的内容。
std::string content_mark_on(sqlite3* dbh, const std::string& tenant) {
    struct Segment {
        std::string_view table;
        std::string_view id_expr;
        char tag;
    };
    static constexpr std::array<Segment, 3> kSegments{{
        {.table = "engrams", .id_expr = "id", .tag = 'e'},
        {.table = "statements", .id_expr = "id", .tag = 's'},
        {.table = "source_unit_meta", .id_expr = "engram_ref || ':' || clause_id", .tag = 'u'},
    },};
    std::string mark;
    bool has_rows = false;
    for (const auto& segment : kSegments) {
        if (!mark.empty()) { mark += '|'; }
        const std::string name(segment.table);
        if (!table_exists(dbh, name)) {
            mark += '-';
            continue;
        }
        // 一条语句（autocommit 下即一个读版本）：计数、max rowid 与那一行的 id 出自同一版本。
        // 「该租户 rowid 最大的那一行」按聚合出的 MAX(rowid) 走 INTEGER PRIMARY KEY 查找（与
        // ORDER BY rowid DESC LIMIT 1 是同一行；后者在这几张表上要临时 B 树排序整个租户，
        // 实测见 EXPLAIN QUERY PLAN）。计数为 0 时 top=0，查不到行 → NULL → 空串。
        std::string sql = "SELECT agg.cnt, agg.top, (SELECT ";
        sql += segment.id_expr;
        sql += " FROM ";
        sql += name;
        sql += " WHERE rowid=agg.top AND tenant_id=?1) FROM (SELECT COUNT(*) AS cnt, "
               "COALESCE(MAX(rowid), 0) AS top FROM ";
        sql += name;
        sql += " WHERE tenant_id=?1) AS agg";
        persistence::Stmt query(dbh, sql.c_str());
        query.bind(tenant);
        std::int64_t rows = 0;
        std::int64_t top = 0;
        std::string top_id;
        if (query.step_row()) {
            rows = sqlite3_column_int64(query.raw(), 0);
            top = sqlite3_column_int64(query.raw(), 1);
            top_id = column_text(query.raw(), 2);
        }
        has_rows = has_rows || rows > 0;
        mark += segment.tag;
        mark += std::to_string(rows);
        mark += ':';
        mark += std::to_string(top);
        mark += ':';
        mark += top_id;
    }
    return has_rows ? mark : std::string{};
}

// 只读打开 db_path 跑 body(连接)。SQLite 错误（库读不出、不是 SQLite）一律转成 snapshot_failed。
// 调用方先确认库文件存在。
template <typename Body>
auto with_read_only(const std::string& db_path, const Body& body) {
    try {
        const auto handle = open_read_only(db_path);
        return body(handle.get());
    } catch (const WorldStoreError&) {
        throw;
    } catch (const std::exception& ex) {
        throw WorldStoreError("snapshot_failed", db_path + ": " + ex.what());
    }
}

// 本租户的 library 摘要（memoryops::digest）。它读 statements、statement_vectors、events 三张表，
// 缺任何一张（0038 之前的库）时返回空串（裁定 R10）。
std::string digest_or_empty(persistence::Connection& conn, const std::string& tenant) {
    for (const char* table : {"statements", "statement_vectors", "events"}) {
        if (!table_exists(conn.raw(), table)) { return {}; }
    }
    return memoryops::digest(conn, tenant);
}

// 只读打开 db_file 算 digest_or_empty。SQLite 错误原样抛出（调用方定码）。
std::string digest_of_file(const fs::path& db_file, const std::string& tenant) {
    auto conn = persistence::Connection::open_read_only(db_file);
    return digest_or_empty(conn, tenant);
}

// 向量在库文件之外的后端（zvec）拍出来的快照、复制出来的世界缺向量，不是完整的世界（spec §12）。快照、回滚、
// 分叉共用这一个判定（裁定 R31 M4）。
void require_sqlite_vectors(const std::string& vector_backend) {
    if (vector_backend != "sqlite") {
        throw WorldStoreError("snapshot_unsupported_backend",
                              "vector_backend '" + vector_backend + "' keeps vectors "
                              "outside the database file");
    }
}

void require_valid_auto_snapshot(const AutoSnapshotConfig& config) {
    if (!interval_in_range(config.interval_hours)) {
        throw WorldStoreError("invalid_auto_snapshot",
                              "interval_hours must be 0-8760, got " +
                                  std::to_string(config.interval_hours));
    }
}

// 可用字节数。标准允许 fs::space 在 FUSE / 网络文件系统上返回 uintmax_t(-1)
// 表示「未知」，直接转成 int64 会得到 -1，让每次快照都被误拒为 insufficient_disk。
// 未知或超出 int64 的值一律按「足够」处理，等于跳过守卫。
std::int64_t available_bytes(const fs::path& dir) {
    const std::uintmax_t avail = fs::space(dir).available;
    constexpr auto kCeiling = static_cast<std::uintmax_t>(std::numeric_limits<std::int64_t>::max());
    return avail > kCeiling ? std::numeric_limits<std::int64_t>::max()
                            : static_cast<std::int64_t>(avail);
}

// 与该世界已有快照（注册项 listed 或磁盘文件）不重名的 id。now_iso（已校验）非空时时间前缀取它，
// 与条目的 created_at 是同一秒；为空时取真实时钟。
std::string fresh_snapshot_id(const std::vector<SnapshotEntry>& listed, const fs::path& dir,
                              const std::string& now_iso) {
    const auto taken = [&](const std::string& cand) {
        std::error_code err;
        if (fs::exists(dir / (cand + ".db"), err)) { return true; }
        return std::ranges::any_of(listed,
                                   [&cand](const SnapshotEntry& snap) { return snap.id == cand; });
    };
    const auto draw = [&now_iso] {
        const std::string stamp = now_iso.empty() ? compact_now() : compact_of(now_iso);
        return stamp + "-" + random_hex().substr(0, 4);
    };
    std::string cand = draw();
    while (taken(cand)) { cand = draw(); }
    return cand;
}

// 文件已删之后摘注册项。摘不掉（SQLite 错误）时注册项还在、文件已经没了：抛 snapshot_failed，下一次
// 修剪或删除对不存在的文件照常删（remove_within 返回 true），再摘一次。
void drop_registered(RegistryDb& reg, const std::string& world, const std::string& snapshot_id) {
    try {
        reg.drop_snapshot(world, snapshot_id);
    } catch (const WorldStoreError&) {
        throw;
    } catch (const std::exception& err) {
        throw WorldStoreError("snapshot_failed", "snapshot '" + snapshot_id + "' of '" + world +
                                                     "' was removed from disk but its registry "
                                                     "entry could not be dropped: " + err.what());
    }
}

}  // namespace

// ---- 注册表不变式（validate_world / validate_registry） -----------------------

void validate_world(const RegistryDb& reg, const std::string& world) {
    const WorldRecord rec = reg.get(world);   // world_not_found
    require_valid_retention(reg.snapshot_retention());
    const bool is_default = world == reg.default_world();
    // 先查与布局无关的一层（形状、配置、快照条目），再查布局：与 worlds.json 时代 load_registry
    // 的顺序相同（validate_shape 在 validate_layout 之前）。
    check_world_shape(rec.name, rec.dir_id, rec.db_path, rec.snapshot_dir, is_default);
    // 存着的设置解析不了（手改过的 registry.db；写路径都经 merge_settings，存不进这样的值）是注册表
    // 损坏，不是调用方的输入错：invalid_settings / invalid_auto_snapshot 等转成 registry_corrupt。
    AutoSnapshotConfig auto_snapshot;
    try {
        auto_snapshot = settings_from_json(rec.settings_json).background.auto_snapshot;
    } catch (const WorldStoreError& err) {
        corrupt("settings_json of '" + rec.name + "': " + err.what());
    }
    check_auto_snapshot(rec.name, auto_snapshot);   // 纵深防御：settings_from_json 已查过范围
    for (const auto& snap : reg.snapshots(world)) {
        check_snapshot_entry(world, rec.snapshot_dir, snap);
    }
    check_world_layout(reg.data_dir(), rec.name, rec.dir_id, rec.db_path, rec.snapshot_dir,
                       is_default);
}

void validate_registry(const RegistryDb& reg) {
    constexpr std::int64_t kPage = 500;   // RegistryDb::list 的上限
    WorldQuery query;
    query.limit = kPage;
    for (;;) {
        const WorldPage page = reg.list(query);
        for (const auto& rec : page.worlds) { validate_world(reg, rec.name); }
        query.offset += kPage;
        if (page.worlds.empty() || query.offset >= page.total) { break; }
    }
}

void set_auto_snapshot(RegistryDb& reg, const std::string& world, const AutoSnapshotConfig& config) {
    (void)reg.get(world);   // world_not_found，先于范围校验（与旧行为同一顺序）
    require_valid_auto_snapshot(config);
    // 经 update_world_settings 合并：读—改—写在注册表锁内一次完成，与并发的设置同步（引擎池保存
    // config 后的 sync_default_settings）互不丢改动。只覆盖 background.auto_snapshot 这一个子键。
    const std::string overrides = R"({"background":{"auto_snapshot":{"on_session":)" +
                                  std::string(config.on_session ? "true" : "false") +
                                  R"(,"interval_hours":)" + std::to_string(config.interval_hours) +
                                  "}}}";
    (void)reg.update_world_settings(world, overrides);
}

// ---- 世界统计与快照（W2） --------------------------------------------------

std::string snapshot_world_time(const std::string& db_path, const std::string& tenant) {
    std::error_code err;
    if (!fs::exists(db_path, err)) { return {}; }
    try {
        const auto handle = open_read_only(db_path);
        return world_time_on(handle.get(), tenant);
    } catch (const WorldStoreError&) {
        throw;
    } catch (const std::exception& ex) {
        throw WorldStoreError("snapshot_failed", db_path + ": " + ex.what());
    }
}

WorldStats world_stats(const std::string& db_path, const std::string& tenant) {
    WorldStats stats;
    std::error_code err;
    if (!fs::exists(db_path, err)) { return stats; }
    const auto main_size = fs::file_size(db_path, err);
    if (!err) { stats.db_bytes += static_cast<std::int64_t>(main_size); }
    const auto wal_size = fs::file_size(db_path + "-wal", err);   // 不存在时按 0 计
    if (!err) { stats.db_bytes += static_cast<std::int64_t>(wal_size); }
    try {
        const auto handle = open_read_only(db_path);
        stats.live_bytes = live_bytes_of(handle.get());
        stats.statements = count_tenant_rows(handle.get(), "statements", tenant);
        stats.engrams = count_tenant_rows(handle.get(), "engrams", tenant);
        stats.world_time = world_time_on(handle.get(), tenant);
        statement_activity_on(handle.get(), tenant, stats);
    } catch (const WorldStoreError&) {
        throw;
    } catch (const std::exception& ex) {
        throw WorldStoreError("snapshot_failed", db_path + ": " + ex.what());
    }
    return stats;
}

// NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
void verify_world_tenant(const std::string& db_path, const std::string& tenant) {
    std::error_code err;
    if (!fs::exists(db_path, err)) { return; }   // 新世界：库留到首次激活才建
    try {
        const auto handle = open_read_only(db_path);
        std::vector<std::string> tables;
        {
            persistence::Stmt list(handle.get(),
                "SELECT tbl.name FROM sqlite_master AS tbl WHERE tbl.type = 'table' "
                "AND EXISTS (SELECT 1 FROM pragma_table_info(tbl.name) AS col "
                "WHERE col.name = 'tenant_id') ORDER BY tbl.name");
            while (list.step_row()) { tables.push_back(column_text(list.raw(), 0)); }
        }
        for (const auto& table : tables) {
            persistence::Stmt probe(handle.get(),
                ("SELECT 1 FROM " + quote_identifier(table) +
                 " WHERE tenant_id IS NOT NULL AND tenant_id <> ?1 LIMIT 1").c_str());
            probe.bind(tenant);
            if (probe.step_row()) {
                std::string detail = "table '";
                detail += table;
                detail += "' in ";
                detail += db_path;
                detail += " holds rows of a tenant other than '";
                detail += tenant;
                detail += '\'';
                throw WorldStoreError("world_tenant_mismatch", detail);
            }
        }
    } catch (const WorldStoreError&) {
        throw;
    } catch (const std::exception& ex) {
        throw WorldStoreError("snapshot_failed", db_path + ": " + ex.what());
    }
}

SnapshotEntry take_snapshot(RegistryDb& reg, const std::string& world,
                            const SnapshotOptions& opts) {
    const WorldRecord entry = reg.get(world);   // world_not_found
    // 先校验入参与保留份数：否则快照写完才发现配置非法，留下无人登记的文件。
    // kind 拼错（"Auto"）不能静默当成 manual：保留策略只认 "auto"，会丢掉回滚锚点保护。
    if (!is_snapshot_kind(opts.kind)) {
        throw WorldStoreError("invalid_snapshot_kind",
                              "kind must be 'manual', 'auto' or 'held', got '" + opts.kind + "'");
    }
    // reason 不配对（manual/session、auto/manual……）同样在任何 I/O 之前拒：否则快照文件已经
    // 写出，登记时才发现条目不合注册表的规则。
    if (!kind_reason_paired(opts.kind, opts.reason)) {
        throw WorldStoreError("invalid_snapshot_kind",
                              "kind '" + opts.kind + "' cannot have reason '" + opts.reason +
                                  "' (manual/manual, auto/session, auto/interval, held/held)");
    }
    // 注入的时钟：快照 id 的时间前缀与 created_at 都由它推出，形状不对在 I/O 之前拒。
    if (!opts.now_iso.empty() && !parse_utc_seconds(opts.now_iso)) {
        throw WorldStoreError("invalid_auto_snapshot",
                              "now_iso is not YYYY-MM-DDTHH:MM:SSZ: '" + opts.now_iso + "'");
    }
    // 向量在库文件之外的后端（zvec）拍出来的快照缺向量，不是完整的世界（spec §12）。
    require_sqlite_vectors(opts.vector_backend);
    require_valid_retention(reg.snapshot_retention());
    // 裁定 R12：写文件、清扫残留之前先查这个世界的注册表行与布局（snapshot_dir 被手改到世界目录之外、
    // snapshots/ 外链时拒绝，清扫与 VACUUM INTO 都不会落到外部）。只读：仍在任何文件 I/O 之前。
    validate_world(reg, world);
    const fs::path dir = entry.snapshot_dir;

    // 库文件不存在（世界从未激活）：在打开连接前拒绝，否则 mode=ro 只会报含糊的
    // "unable to open database file"。
    std::error_code exists_err;
    if (!fs::exists(entry.db_path, exists_err)) {
        throw WorldStoreError("snapshot_failed",
                              "database file " + entry.db_path + " does not exist");
    }

    SnapshotEntry snap;
    snap.kind = opts.kind;
    snap.reason = opts.reason;
    snap.note = opts.note;
    snap.session_id = opts.session_id;
    fs::path partial;
    try {
        // default 世界的快照目录由 RegistryDb::open 登记但不创建，首次快照时补建。
        fs::create_directories(dir);
        // 清扫与选 id 用同一份已登记列表：两者之间没有别的写者（调用方按世界串行化快照）。
        const std::vector<SnapshotEntry> listed = reg.snapshots(world);
        (void)sweep_leftovers(listed, dir);

        auto source = open_read_only(entry.db_path);
        const std::int64_t live = live_bytes_of(source.get());
        const std::int64_t avail = opts.free_space ? opts.free_space(dir) : available_bytes(dir);
        if (avail < 3 * live) {
            throw WorldStoreError("insufficient_disk",
                                  "need " + std::to_string(3 * live) + " bytes free in " +
                                      dir.string() + ", have " + std::to_string(avail));
        }

        snap.id = fresh_snapshot_id(listed, dir, opts.now_iso);
        partial = dir / (snap.id + ".db.partial");
        const fs::path final_path = dir / (snap.id + ".db");
        {
            std::error_code ignore;
            fs::remove(partial, ignore);
        }
        if (opts.before_vacuum) { opts.before_vacuum(); }
        {
            persistence::Stmt vacuum(source.get(), "VACUUM INTO ?1");
            vacuum.bind(partial.string());
            vacuum.step_done();
        }
        source.reset();

        {
            const auto copy = open_read_only(partial.string());
            bool intact = false;
            {
                persistence::Stmt check(copy.get(), "PRAGMA integrity_check");
                intact = check.step_row() && column_text(check.raw(), 0) == "ok";
            }
            if (!intact) {
                throw WorldStoreError("snapshot_failed",
                                      "integrity_check failed for " + partial.string());
            }
            // 会话数与内容标记在拍出来的快照文件上算（Codex I-2）：快照是静止的，标记因此恰好描述
            // 快照里有什么。在源库上、VACUUM 之前算是错的：autocommit 下每条语句读各自的版本，标记
            // 与复制之间提交的删除（forget、clear_memory）会让快照少于标记，三段标记也可能出自不同
            // 版本。比较当前标记（current_content_mark，源库上算）与快照的标记，依赖 VACUUM INTO 保留
            // rowid：这是实测行为（SQLite 3.51.3、生产副本），由契约测试 VacuumIntoKeepsRowidsAcrossA-
            // MiddleGapContract 钉住，SQLite 文档不承诺。坏了的方向是误判有变化、多拍，不会漏拍。
            snap.world_time = world_time_on(copy.get(), entry.tenant);
            snap.sessions_complete = sessions_on(copy.get(), entry.tenant);
            snap.content_mark = content_mark_on(copy.get(), entry.tenant);
        }
        snap.size_bytes = static_cast<std::int64_t>(fs::file_size(partial));
        snap.path = final_path.string();
        snap.created_at = opts.now_iso.empty() ? iso_now() : opts.now_iso;
        fs::rename(partial, final_path);
    } catch (const WorldStoreError&) {
        std::error_code ignore;
        if (!partial.empty()) { fs::remove(partial, ignore); }
        throw;
    } catch (const std::exception& ex) {
        std::error_code ignore;
        if (!partial.empty()) { fs::remove(partial, ignore); }
        throw WorldStoreError("snapshot_failed", ex.what());
    }

    // 改好名之后的失败（算摘要、登记）都删掉刚改好名的文件：没有登记的快照列不出来、也回滚不到，
    // 留着只是孤儿（原来由调用方存注册表失败后调 discard_snapshot_file 补偿）。删的时候经
    // remove_within，不出 snapshot_dir。返回追加到错误信息里的说明。
    const auto discard_final = [&dir, &snap] {
        std::string why;
        try {
            if (!remove_within(dir, snap.path)) { why = "; " + snap.path + " resolves outside"; }
        } catch (const WorldStoreError& remove_err) {
            why = std::string("; cannot remove ") + snap.path + ": " + remove_err.what();
        }
        return why;
    };
    // 6a. 摘要在拍好的快照文件上算（与快照里的内容是同一份字节）。缺表时空串（裁定 R10）。
    try {
        snap.digest = digest_of_file(snap.path, entry.tenant);
    } catch (const std::exception& digest_err) {
        const std::string why = digest_err.what() + discard_final();
        throw WorldStoreError("snapshot_failed", "cannot compute the digest of snapshot '" +
                                                     snap.id + "' of '" + world + "': " + why);
    }
    // 7. 登记（成为最新的一份）。
    try {
        reg.put_snapshot(world, snap);
    } catch (const std::exception& record_err) {
        const std::string why = record_err.what() + discard_final();
        throw WorldStoreError("snapshot_failed",
                              "cannot record snapshot '" + snap.id + "' of '" + world + "': " + why);
    }
    // 快照已落盘并登记，成功的快照必须把条目返回给调用方。修剪失败（旧快照路径
    // 无法解析或无法删除，WorldStoreError snapshot_failed）不外抛：apply_retention
    // 逐个「先删文件、再摘注册项」，失败时注册表与磁盘仍一致，只是暂时多出几份，
    // 下一次 take_snapshot 或显式 apply_retention 会重试。
    try {
        (void)apply_retention(reg, world);
    } catch (const WorldStoreError& retention_err) {
        // 见上：留待下次修剪重试。越界的注册项会一直卡住修剪，记下来才看得见。
        std::cerr << "[world_store] retention for '" << world
                  << "' deferred: " << retention_err.what() << "\n";
    }
    return snap;
}

std::vector<std::string> apply_retention(RegistryDb& reg, const std::string& world) {
    const WorldRecord found = reg.get(world);   // world_not_found
    const int retention = reg.snapshot_retention();
    require_valid_retention(retention);
    validate_world(reg, world);   // 裁定 R12：删文件之前先查布局
    std::vector<std::string> removed;
    // 最新在前；held 不参与（不计数、不删，spec §7.1）。
    const std::vector<SnapshotEntry> list = prunable(reg.snapshots(world));
    const auto limit = static_cast<std::size_t>(retention);
    if (list.size() <= limit) { return removed; }

    // 先定好要删谁：第一轮从最旧的 auto 起（最新的那份 auto 是回滚锚点，跳过），
    // 仍超出时第二轮从最旧的 manual 起。
    const auto newest_auto = std::ranges::find_if(
        list, [](const SnapshotEntry& snap) { return snap.kind == "auto"; });
    const SnapshotEntry* anchor = newest_auto == list.end() ? nullptr : &*newest_auto;
    const std::size_t excess = list.size() - limit;
    std::vector<const SnapshotEntry*> victims;
    for (auto iter = list.rbegin(); iter != list.rend() && victims.size() < excess; ++iter) {
        if (iter->kind == "auto" && &*iter != anchor) { victims.push_back(&*iter); }
    }
    for (auto iter = list.rbegin(); iter != list.rend() && victims.size() < excess; ++iter) {
        if (iter->kind != "auto") { victims.push_back(&*iter); }
    }

    // 逐个「先删文件、再摘注册项」：中途失败时注册表仍与磁盘一致。路径越界时
    // fail closed：文件没删，摘掉注册项就再也无法经注册表清理它。
    const fs::path dir = found.snapshot_dir;
    for (const SnapshotEntry* victim : victims) {
        if (!remove_within(dir, victim->path)) {
            throw WorldStoreError("snapshot_failed", "snapshot '" + victim->id + "' path " +
                                                         victim->path + " is outside " +
                                                         dir.string());
        }
        drop_registered(reg, world, victim->id);
        removed.push_back(victim->id);
    }
    return removed;
}

void delete_snapshot(RegistryDb& reg, const std::string& world, const std::string& snapshot_id) {
    const WorldRecord found = reg.get(world);   // world_not_found
    validate_world(reg, world);   // 裁定 R12：删文件之前先查布局
    const std::vector<SnapshotEntry> list = reg.snapshots(world);
    const auto pos = std::ranges::find_if(
        list, [&snapshot_id](const SnapshotEntry& snap) { return snap.id == snapshot_id; });
    if (pos == list.end()) {
        throw WorldStoreError("snapshot_not_found", "snapshot '" + snapshot_id + "' not found");
    }
    // 先删文件再摘注册项。文件已经不在了（保存失败后的补偿、手工删除）照摘；路径越界
    // （被篡改的注册表）时 fail closed：文件不动，注册项保留。
    if (!remove_within(found.snapshot_dir, pos->path)) {
        throw WorldStoreError("snapshot_failed", "snapshot '" + snapshot_id + "' path " +
                                                     pos->path + " is outside " +
                                                     found.snapshot_dir);
    }
    drop_registered(reg, world, snapshot_id);
}

// ---- 回滚与归档（W3） ------------------------------------------------------

namespace {

// 该世界在注册表里的快照条目（按值返回：注册表行随时可能被别的调用改，回滚用的是这一刻的副本）。
// NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
SnapshotEntry registered_snapshot(const RegistryDb& reg, const std::string& world,
                                  const std::string& snapshot_id) {
    const std::vector<SnapshotEntry> list = reg.snapshots(world);
    const auto pos = std::ranges::find_if(
        list, [&snapshot_id](const SnapshotEntry& snap) { return snap.id == snapshot_id; });
    if (pos != list.end()) { return *pos; }
    throw WorldStoreError("snapshot_not_found", "snapshot '" + snapshot_id + "' not found");
}

// 世界 home = <data_dir>/worlds/<dir_id>，与 RegistryDb::create / remove 同一推导，只由服务端的
// 目录布局与注册表里的 dir_id 决定。snapshot_dir 不参与：validate_world 拒绝解析后越出世界目录的
// snapshot_dir，但 rollback_world 也可以不经校验直接调用，归档位置不能依赖它。
// 归档目录为 <世界 home>/archive。
// 归档操作（回滚建硬链接、列出、清理）之前先 fail closed，此时还什么都没建：
//   1. worlds/ 解析符号链接后仍在数据目录之下（require_worlds_root_inside，registry_corrupt）；
//   2. dir_id 是单独一级路径（手改过的 registry.db 可以把它改成 "../x"），世界 home 解析符号链接后
//      严格在 worlds/ 之下，并且恰好是解析后的 worlds/<dir_id>（与 validate_world 的布局校验同一条
//      规则，见 require_resolved_home），否则 snapshot_failed。只查「在 worlds/ 之下」不够：
//      worlds/<sim-1> -> worlds/<sim-2> 时，回滚、列出、清理 sim-1 实际作用在 sim-2 的库与 archive/
//      上，清理会删掉 sim-2 的回滚归档（回滚前旧库唯一剩下的名字）。与 RegistryDb::remove 是同一个
//      边界。
// 返回世界 home（未解析符号链接的路径）。
// NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
fs::path checked_world_home(const fs::path& data_dir, const std::string& world,
                            const std::string& dir_id) {
    const fs::path root = normalized(fs::absolute(data_dir));
    const fs::path worlds_root = root / "worlds";
    require_worlds_root_inside(root, worlds_root);
    require_plain_dir_id(dir_id, world, "snapshot_failed");
    const fs::path home = worlds_root / dir_id;
    require_inside_registry(worlds_root, home);
    require_resolved_home(worlds_root, dir_id, "snapshot_failed", world);
    return home;
}

// candidate 与 db_file 是不是同一个文件（同一 inode）。任一方不存在或 stat 失败都算「不是」。
bool is_live_alias(const fs::path& candidate, const fs::path& db_file) {
    std::error_code err;
    const bool same = fs::equivalent(candidate, db_file, err);
    return !err && same;
}

// 不跟随符号链接的 stat。文件不存在返回 not_found（libc++ 此时也会把 err 设成 ENOENT，
// 不能据此当成错误）；其余错误（EACCES、ELOOP 等）抛 snapshot_failed。
fs::file_status lstat_or_throw(const fs::path& path) {
    std::error_code err;
    const fs::file_status status = fs::symlink_status(path, err);
    if (err && status.type() != fs::file_type::not_found) {
        throw WorldStoreError("snapshot_failed",
                              "cannot stat " + path.string() + ": " + err.message());
    }
    return status;
}

constexpr std::string_view kPreRollback = ".pre-rollback-";
constexpr std::string_view kRollbackTag = ".rollback-";
constexpr std::string_view kPartialSuffix = ".partial";
constexpr std::size_t kPartialHexLength = 16;   // random_hex() 的长度

// "<库文件名>.pre-rollback-<YYYYMMDDTHHMMSSZ>-<4 hex>"：后缀与快照 id 同形。
bool looks_like_archive_name(std::string_view name) {
    const auto pos = name.rfind(kPreRollback);
    return pos != std::string_view::npos && pos > 0 &&
           looks_like_snapshot_id(name.substr(pos + kPreRollback.size()));
}

// 回滚的 staging 文件：库所在目录里的 .<库文件名>.rollback-<16 位小写 hex>.partial。
std::string rollback_partial_prefix(const fs::path& db_file) {
    return "." + db_file.filename().string() + std::string(kRollbackTag);
}

bool looks_like_rollback_partial(std::string_view name, const std::string& prefix) {
    if (name.size() != prefix.size() + kPartialHexLength + kPartialSuffix.size() ||
        !name.starts_with(prefix) || !name.ends_with(kPartialSuffix)) {
        return false;
    }
    return std::ranges::all_of(name.substr(prefix.size(), kPartialHexLength), [](char chr) {
        return (chr >= '0' && chr <= '9') || (chr >= 'a' && chr <= 'f');
    });
}

// 清扫上次回滚崩溃留下的 partial（见 world_store.hpp rollback_world 第 4a 步）。只认名字严格
// 符合模式的普通文件，跳过符号链接，每个删除都经 remove_within。返回删掉的个数；错误只记日志。
std::size_t sweep_rollback_partials(const fs::path& db_file) {
    const fs::path dir = db_file.parent_path();
    const std::string prefix = rollback_partial_prefix(db_file);
    std::vector<fs::path> victims;
    std::error_code err;
    fs::directory_iterator iter(dir, err);
    const fs::directory_iterator done;
    for (; !err && iter != done; iter.increment(err)) {
        const fs::directory_entry& item = *iter;
        std::error_code status_err;
        if (item.is_symlink(status_err) || !item.is_regular_file(status_err)) { continue; }
        if (looks_like_rollback_partial(item.path().filename().string(), prefix)) {
            victims.push_back(item.path());
        }
    }
    std::size_t swept = 0;
    for (const auto& victim : victims) {
        try {
            if (remove_within(dir, victim)) { ++swept; }
        } catch (const WorldStoreError& sweep_err) {
            std::cerr << "[world_store] cannot sweep " << victim.string() << ": "
                      << sweep_err.what() << "\n";
        }
    }
    if (swept > 0) {
        std::cerr << "[world_store] swept " << swept << " leftover rollback partial(s) in "
                  << dir.string() << "\n";
    }
    return swept;
}

// 摘掉 archive 里是现役库第二个名字的条目（见 world_store.hpp rollback_world 第 4a 步）。上次回滚
// 若死在建硬链接之后、原子替换之前，归档名和库是同一个 inode：它不是归档，内容随现役库一起变。
// 删掉它只少一个名字，库不受影响。只认名字符合归档模式的普通文件，跳过符号链接，每个删除都经
// remove_within。archive/ 解析后不在世界 home 之下时不扫（第 9 步会拒绝这次回滚）。错误只记日志。
// NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
std::size_t sweep_archive_aliases(const fs::path& home, const fs::path& archive,
                                  const fs::path& db_file) {
    std::error_code err;
    if (!fs::exists(fs::symlink_status(archive, err))) { return 0; }
    try {
        require_inside_registry(home, archive);
    } catch (const WorldStoreError& escape) {
        std::cerr << "[world_store] not sweeping " << archive.string() << ": " << escape.what()
                  << "\n";
        return 0;
    }
    std::vector<fs::path> victims;
    fs::directory_iterator iter(archive, err);
    const fs::directory_iterator done;
    for (; !err && iter != done; iter.increment(err)) {
        const fs::directory_entry& item = *iter;
        std::error_code status_err;
        if (item.is_symlink(status_err) || !item.is_regular_file(status_err)) { continue; }
        if (looks_like_archive_name(item.path().filename().string()) &&
            is_live_alias(item.path(), db_file)) {
            victims.push_back(item.path());
        }
    }
    std::size_t swept = 0;
    for (const auto& victim : victims) {
        try {
            if (remove_within(archive, victim)) { ++swept; }
        } catch (const WorldStoreError& sweep_err) {
            std::cerr << "[world_store] cannot remove " << victim.string()
                      << ", a second name of the live database: " << sweep_err.what() << "\n";
        }
    }
    if (swept > 0) {
        std::cerr << "[world_store] removed " << swept << " archive name(s) in " << archive.string()
                  << " that were second names of the live database\n";
    }
    return swept;
}

// 给现役库在 archive 目录里建一个硬链接（归档名）并返回它。库文件本身不动，库路径上始终有
// 一份完整的库。名字已被占用（同一秒内两次回滚）就重抽；其余失败（EXDEV、文件系统不支持
// 硬链接、权限）抛 snapshot_failed，此时什么都没建。
// EXDEV 只出现在一种配置里：数据目录在另一个卷，default 世界的库还在原卷（别的世界的库在
// worlds/<dir_id>/ 下，总与归档同卷）。不退回复制：复制要双倍磁盘和另一套磁盘
// 守卫，而这种配置今天没人部署。错误信息直接说明原因和办法。
// NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
fs::path link_into_archive(const fs::path& db_file, const fs::path& archive) {
    constexpr int kMaxAttempts = 16;
    const std::string db_name = db_file.filename().string();
    for (int attempt = 0; attempt < kMaxAttempts; ++attempt) {
        const fs::path link = archive / (db_name + std::string(kPreRollback) + compact_now() +
                                         "-" + random_hex().substr(0, 4));
        std::error_code err;
        fs::create_hard_link(db_file, link, err);
        if (!err) { return link; }
        if (err == std::errc::cross_device_link) {
            throw WorldStoreError(
                "snapshot_failed",
                "cannot link " + db_file.string() + " into " + archive.string() +
                    ": the archive directory and the database are on different file systems; "
                    "rollback keeps the replaced database as a hard link, so both must be on the "
                    "same volume: move the data directory (registry.db and worlds/) next to "
                    "the database");
        }
        if (err != std::errc::file_exists) {
            throw WorldStoreError("snapshot_failed", "cannot link " + db_file.string() + " into " +
                                                         archive.string() + ": " + err.message());
        }
    }
    throw WorldStoreError("snapshot_failed", "no free archive name in " + archive.string());
}

// 第 3 步：快照文件缺失或不是普通文件 → snapshot_corrupt。
void require_snapshot_file(const std::string& path) {
    std::error_code err;
    if (!fs::is_regular_file(path, err)) {
        throw WorldStoreError("snapshot_corrupt", "snapshot file " + path + " is missing");
    }
}

// 第 6 步：只读打开复制品做 integrity_check。打不开、不是 SQLite 或结果不是 "ok"，
// 一律 snapshot_corrupt。
void require_intact_copy(const fs::path& copy) {
    bool intact = false;
    try {
        const auto handle = open_read_only(copy.string());
        persistence::Stmt check(handle.get(), "PRAGMA integrity_check");
        intact = check.step_row() && column_text(check.raw(), 0) == "ok";
    } catch (const std::exception& ex) {
        throw WorldStoreError("snapshot_corrupt",
                              "cannot read snapshot copy " + copy.string() + ": " + ex.what());
    }
    if (!intact) {
        throw WorldStoreError("snapshot_corrupt", "integrity_check failed for " + copy.string());
    }
}

// 库旁边的 -wal / -journal 非空（或不是普通文件）：还有未合并进主文件的写入、写连接还开着，
// 或者有崩溃事务留下的热日志。此时主文件不是完整的库；残留的 WAL / 热日志还会被 SQLite
// 重放或回滚到恢复出来的库上，把它写坏。
void require_empty_sidecar(const fs::path& sidecar) {
    const auto status = lstat_or_throw(sidecar);
    if (!fs::exists(status)) { return; }
    std::error_code err;
    if (!fs::is_regular_file(status) || fs::file_size(sidecar, err) != 0 || err) {
        throw WorldStoreError("snapshot_failed",
                              sidecar.string() + " is not empty: the database has pending "
                              "writes, an open writer or a hot journal");
    }
}

// 库旁边的 -shm 只能不存在或是普通文件，大小不论：它是 wal-index，SQLite 按主文件加 WAL
// 重建，持久性从不依赖它。目录、符号链接之类不是 SQLite 留下的，第 10 步不该删它们（非空
// 目录也删不掉），所以在这里就拒绝，此时什么都还没改。
void require_regular_or_absent(const fs::path& sidecar) {
    const auto status = lstat_or_throw(sidecar);
    if (fs::exists(status) && !fs::is_regular_file(status)) {
        throw WorldStoreError("snapshot_failed", sidecar.string() +
                                                     " is not a regular file; refusing to "
                                                     "remove it");
    }
}

// 第 8 步：要被替换的必须是一个 SQLite 库（16 字节文件头）。default 世界的 db_path 不受
// validate_layout 约束，被篡改的注册表可以让它指向任意文件；不查就会把那个文件整个换掉、
// 挪进归档，purge 之后就没了。
void require_sqlite_header(const fs::path& db_file) {
    constexpr std::size_t kHeaderLength = 16;
    constexpr std::string_view kMagic{"SQLite format 3\0", kHeaderLength};
    std::array<char, kHeaderLength> head{};
    std::ifstream input(db_file, std::ios::binary);
    if (!input) {
        throw WorldStoreError("snapshot_failed", "cannot read " + db_file.string());
    }
    input.read(head.data(), static_cast<std::streamsize>(head.size()));
    if (!input || std::string_view(head.data(), head.size()) != kMagic) {
        throw WorldStoreError("snapshot_failed", db_file.string() +
                                                     " is not a SQLite database; refusing to "
                                                     "replace it");
    }
}

// 第 8 步末：探测别的连接（同进程或别的进程）。新开一条读写连接（不 CREATE），busy_timeout 为
// 0，PRAGMA locking_mode=EXCLUSIVE 后 BEGIN EXCLUSIVE：拿不到排他锁回 SQLITE_BUSY → world_in_use；
// 拿到了就 ROLLBACK 关掉，什么都没写。
// 它看得见哪些连接取决于库的日志模式（在生产库副本上实测）：
//                                  WAL        rollback-journal（delete）
//   闲置的只读连接（读过）           BUSY       看不见
//   闲置的读写连接（读过）           BUSY       看不见
//   开着的读事务                     BUSY       BUSY
//   打开了、从没读过                 看不见     看不见
// 恢复出来的库是 VACUUM INTO 的产物，引擎打开它之前一直是 delete 模式（例如回滚过、还没
// 激活过的非活动世界），这时闲置的连接全都看不见。
// 所以探测只是纵深防御。别的进程对 dashboard 管的库开写连接不在支持的使用方式之内；进程内的
// 安全靠 WorldManager 的租约、旧 Runtime 弱引用已失效的证明和遗留 Runtime 登记。
// 必须在 -wal / -journal 检查之后调用：那时两者都为空，这条连接不会把残留的 WAL 或热日志
// 重放进旧库。
void require_no_other_connection(const fs::path& db_file) {
    sqlite3* raw = nullptr;
    const std::string uri = "file:" + uri_escape_path(db_file.string());
    const int opened =
        sqlite3_open_v2(uri.c_str(), &raw, SQLITE_OPEN_READWRITE | SQLITE_OPEN_URI, nullptr);
    const persistence::SqliteHandle handle(raw);
    if (opened != SQLITE_OK) {
        throw WorldStoreError("snapshot_failed",
                              "cannot open " + db_file.string() + " to check for other "
                              "connections: " + (raw != nullptr ? sqlite3_errmsg(raw) : "alloc"));
    }
    sqlite3_busy_timeout(raw, 0);
    for (const char* sql : {"PRAGMA locking_mode=EXCLUSIVE", "BEGIN EXCLUSIVE", "ROLLBACK"}) {
        // 这条连接没开扩展结果码，sqlite3_exec 回的就是主结果码（SQLITE_BUSY / SQLITE_LOCKED）。
        const int code = sqlite3_exec(raw, sql, nullptr, nullptr, nullptr);
        if (code == SQLITE_OK) { continue; }
        const std::string why = sqlite3_errmsg(raw);
        if (code == SQLITE_BUSY || code == SQLITE_LOCKED) {
            throw WorldStoreError("world_in_use",
                                  db_file.string() + " is open in another connection (" + why +
                                      "); refusing to replace it");
        }
        throw WorldStoreError("snapshot_failed", "cannot check " + db_file.string() +
                                                     " for other connections: " + why);
    }
}

// 替换失败时摘掉刚建的归档名。库没被替换，归档名与库是同一个 inode，删掉它只少一个名字。
// 删不掉时它就是现役库的第二个名字：清理它无害，但不能用写连接打开它。返回追加到错误信息
// 里的说明。
std::string drop_archive_name(const fs::path& archived) {
    if (archived.empty()) { return {}; }   // 回滚前库不存在：没有建归档名
    std::string why;
    try {
        if (remove_within(archived.parent_path(), archived)) { return {}; }
        why = "it resolves outside " + archived.parent_path().string();
    } catch (const WorldStoreError& err) {
        why = err.what();
    }
    std::cerr << "[world_store] rollback could not remove archive name " << archived.string()
              << ", which is a second name of the live database: " << why << "\n";
    return "; " + archived.string() + " could not be removed (" + why +
           ") and is a second name of the live database: do not open it with a write connection";
}

// 回滚第 1–4 步通过之后已经确定的东西（注册表记录的副本：回滚全程用同一份路径）。
struct RollbackTarget {
    WorldRecord entry;
    SnapshotEntry snap;
    fs::path home;                // 世界 home（未解析符号链接的路径）
    std::int64_t snap_bytes = 0;
};

// 回滚第 1–4 步：只读检查，不清扫、不复制、不建目录。check_rollback（预检）与 rollback_world
// 共用这一份，顺序与错误码都一样（见 world_store.hpp rollback_world）。
// NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
RollbackTarget check_rollback_target(const RegistryDb& reg, const std::string& world,
                                     const std::string& snapshot_id, const RollbackOptions& opts) {
    // 1. 路径只从注册表取。向量在库文件之外的后端（zvec）恢复不出完整的世界（spec §12）。
    RollbackTarget target;
    target.entry = reg.get(world);   // world_not_found
    validate_world(reg, world);      // 裁定 R12：替换库、建归档之前先查注册表行与布局
    // 裁定 R31 M1：只读世界（含种子，seed 蕴含 read_only）的库不能被替换。与快照 id 无关，所以在找快照之前。
    if (target.entry.read_only || target.entry.seed) {
        throw WorldStoreError("world_read_only",
                              "world '" + world + "' is read-only and cannot be restored");
    }
    target.snap = registered_snapshot(reg, world, snapshot_id);
    require_sqlite_vectors(opts.vector_backend);
    // 2–3. 快照文件解析符号链接后必须仍在该世界的 snapshot_dir 之下，且是普通文件。
    require_inside_registry(target.entry.snapshot_dir, target.snap.path);
    require_snapshot_file(target.snap.path);
    // 4. 世界 home 由数据目录与 dir_id 推出，解析后必须在 worlds/ 之下（见 checked_world_home）；
    //    复制品与余量：库所在目录要有 2 × 快照大小。
    target.home = checked_world_home(reg.data_dir(), world, target.entry.dir_id);
    const fs::path db_dir = fs::path(target.entry.db_path).parent_path();
    target.snap_bytes = static_cast<std::int64_t>(fs::file_size(target.snap.path));
    const std::int64_t avail = opts.free_space ? opts.free_space(db_dir) : available_bytes(db_dir);
    if (avail < 2 * target.snap_bytes) {
        throw WorldStoreError("insufficient_disk",
                              "need " + std::to_string(2 * target.snap_bytes) + " bytes free in " +
                                  db_dir.string() + ", have " + std::to_string(avail));
    }
    return target;
}

}  // namespace

// NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
void check_rollback(const RegistryDb& reg, const std::string& world,
                    const std::string& snapshot_id, const RollbackOptions& opts) {
    try {
        (void)check_rollback_target(reg, world, snapshot_id, opts);
    } catch (const WorldStoreError&) {
        throw;
    } catch (const std::exception& ex) {
        throw WorldStoreError("snapshot_failed", ex.what());
    } catch (...) {
        throw WorldStoreError("snapshot_failed", "rollback check of '" + world + "' failed");
    }
}

// NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
RollbackResult rollback_world(const RegistryDb& reg, const std::string& world,
                              const std::string& snapshot_id, const RollbackOptions& opts) {
    fs::path partial;    // 第 5 步起非空：任何失败都删掉它
    RollbackResult result;
    try {
        // 1–4. 与 check_rollback 同一份检查。调用方即使预检过也要再查：预检之后状态可能变了。
        const RollbackTarget target = check_rollback_target(reg, world, snapshot_id, opts);
        const WorldRecord& entry = target.entry;
        const SnapshotEntry& snap = target.snap;
        const fs::path& home = target.home;
        const fs::path db_file = entry.db_path;
        const fs::path db_dir = db_file.parent_path();
        const fs::path wal = db_file.string() + "-wal";
        const fs::path shm = db_file.string() + "-shm";
        const fs::path journal = db_file.string() + "-journal";

        // 4a. 上次回滚崩溃留下的 partial，以及 archive 里是现役库第二个名字的条目。
        (void)sweep_rollback_partials(db_file);
        (void)sweep_archive_aliases(home, home / "archive", db_file);

        // 5. 复制到库所在目录的隐藏 partial（同目录，第 11 步的改名才是原子的）。库目录只读时
        //    在这里失败，目标库一个字节都没动：绝不直接往库路径上复制。
        partial = db_dir / (rollback_partial_prefix(db_file) + random_hex() +
                            std::string(kPartialSuffix));
        fs::copy_file(snap.path, partial, fs::copy_options::overwrite_existing);

        // 6–7. 校验的是要装上去的那份字节：复制之后快照文件怎么变都影响不到它。
        require_intact_copy(partial);
        verify_world_tenant(partial.string(), entry.tenant);
        const auto staged_bytes = static_cast<std::int64_t>(fs::file_size(partial));

        // 8. 库路径与附属文件的前置检查，什么都还没改。
        const auto db_status = lstat_or_throw(db_file);
        const bool db_exists = fs::exists(db_status);
        if (db_exists && !fs::is_regular_file(db_status)) {
            // 库路径不是普通文件（例如符号链接）：对符号链接建硬链接，得到什么因平台而异
            // （macOS 的 link() 跟随链接、链到目标文件；Linux 链的是符号链接本身），两种结果都
            // 不是我们要的归档：前者让链接目标多出一个名字，后者 list / purge 认不出（跳过符号
            // 链接）。所以直接拒绝。
            throw WorldStoreError("snapshot_failed",
                                  db_file.string() + " is not a regular file; refusing to replace");
        }
        require_empty_sidecar(wal);
        require_empty_sidecar(journal);
        require_regular_or_absent(shm);
        if (db_exists) {
            require_sqlite_header(db_file);
            // 恢复出来的库沿用现役库的权限位（例如收紧过的 0600），不随快照文件放宽。
            fs::permissions(partial, fs::status(db_file).permissions(), fs::perm_options::replace);
            // 最后一道检查，紧挨着建硬链接：还有别的连接开着就不动它（world_in_use）。
            require_no_other_connection(db_file);
        }

        // 9. 给现役库在 <世界 home>/archive 建硬链接。库路径不动。
        fs::path archived;   // 非空 = 归档名已建好，之后的失败要把它摘掉
        if (db_exists) {
            // 复制与校验可能花了几秒：建目录之前再查一遍世界 home，世界目录之外什么都不建。
            const fs::path archive =
                checked_world_home(reg.data_dir(), world, entry.dir_id) / "archive";
            fs::create_directories(archive);
            // archive/ 被换成指向外部的符号链接时拒绝：不在世界目录之外给现役库建名字。
            require_inside_registry(home, archive);
            archived = link_into_archive(db_file, archive);
        }

        // 10–11. 库路径上仍是旧库。失败时库原样不动，只需摘掉归档名；partial 由外层 catch 删。
        try {
            // 旁车属于旧库，不能挂在新库旁边，所以在替换之前删。替换随后失败时，旧库少了它们
            // 也照常可用，因为它们不带任何状态：-wal / -journal 在第 8 步已确认为空或不存在；
            // -shm 是 wal-index，SQLite 按主文件加 WAL 重建，不承载持久数据。删的时候也没有
            // 连接在用它们：第 8 步的探测，加上 Python 侧在回滚之前停线程、等租约、确认旧
            // Runtime 已回收。
            // 改成替换之后再删更糟：从 rename 到删完这段时间里，旧库的 -shm / -wal 挂在新库旁边，
            // 这时打开新库的连接会拿旧库的 wal-index 去读新库。
            for (const fs::path& sidecar : {wal, shm, journal}) { fs::remove(sidecar); }
            if (opts.before_replace) { opts.before_replace(partial); }
            // 原子替换：此前库路径是旧库，此后是新库；旧 inode 只剩归档这一个名字。
            fs::rename(partial, db_file);
        } catch (const std::exception& ex) {
            throw WorldStoreError("snapshot_failed", "cannot replace " + db_file.string() + ": " +
                                                         ex.what() + "; the database is unchanged" +
                                                         drop_archive_name(archived));
        } catch (...) {
            throw WorldStoreError("snapshot_failed", "cannot replace " + db_file.string() +
                                                         "; the database is unchanged" +
                                                         drop_archive_name(archived));
        }
        result.archive_path = archived.string();
        result.restored_bytes = staged_bytes;

        // 12. 库已替换：从这里起抛出的错误都带 replaced=true（裁定 R31 M5），码不变。在恢复后的库上算摘要（缺表时
        //     空串，裁定 R10）；条目带 digest 时必须相等，不等说明快照文件登记之后被改过（spec §7.2）。旧条目（digest
        //     为空）不比较。
        const std::string restored = "restored " + db_file.string() + " from snapshot '" + snap.id + "'";
        try {
            if (opts.after_replace_for_test) { opts.after_replace_for_test(); }
            result.digest = digest_of_file(db_file, entry.tenant);
        } catch (const WorldStoreError& replaced_err) {
            throw WorldStoreError::after_replace(replaced_err.code(), restored + ", but " +
                                                                       replaced_err.what());
        } catch (const std::exception& digest_err) {
            throw WorldStoreError::after_replace(
                "snapshot_failed", restored + ", but cannot compute its digest: " + digest_err.what());
        } catch (...) {
            throw WorldStoreError::after_replace("snapshot_failed",
                                                 restored + ", but cannot compute its digest");
        }
        if (!snap.digest.empty() && result.digest != snap.digest) {
            throw WorldStoreError::after_replace(
                "restore_digest_mismatch",
                restored + ", but its digest " + result.digest + " differs from the recorded " +
                    snap.digest + ": the snapshot file was changed after it was taken");
        }
    } catch (const WorldStoreError&) {
        std::error_code ignore;
        if (!partial.empty()) { fs::remove(partial, ignore); }
        throw;
    } catch (const std::exception& ex) {
        std::error_code ignore;
        if (!partial.empty()) { fs::remove(partial, ignore); }
        throw WorldStoreError("snapshot_failed", ex.what());
    } catch (...) {
        std::error_code ignore;
        if (!partial.empty()) { fs::remove(partial, ignore); }
        throw WorldStoreError("snapshot_failed", "rollback of '" + world + "' failed");
    }
    return result;
}

std::vector<ArchiveEntry> list_rollback_archives(const RegistryDb& reg, const std::string& world) {
    const WorldRecord entry = reg.get(world);   // world_not_found
    validate_world(reg, world);   // 裁定 R12（purge 经这里删文件）
    const fs::path home = checked_world_home(reg.data_dir(), world, entry.dir_id);
    const fs::path dir = home / "archive";
    if (!fs::exists(lstat_or_throw(dir))) { return {}; }
    // archive/ 本身指向外部时，逐个列出与 remove_within(archive, …) 都会以外部目录为根。
    require_inside_registry(home, dir);
    const fs::path db_file = entry.db_path;

    std::vector<ArchiveEntry> out;
    std::error_code err;
    fs::directory_iterator iter(dir, err);
    const fs::directory_iterator done;
    for (; !err && iter != done; iter.increment(err)) {
        const fs::directory_entry& item = *iter;
        std::error_code item_err;
        // 只认普通文件：符号链接（回滚从不创建）一概跳过，免得经它删到别处。
        if (item.is_symlink(item_err) || !item.is_regular_file(item_err)) { continue; }
        std::string name = item.path().filename().string();
        if (!looks_like_archive_name(name)) { continue; }
        // 现役库的第二个名字（回滚死在建硬链接之后、替换之前）不是归档：不列、不计、不清理，
        // 下一次回滚开头摘掉它（第 4a 步）。
        if (is_live_alias(item.path(), db_file)) { continue; }
        const auto size = item.file_size(item_err);
        if (item_err) { continue; }   // 列的同时被删了
        ArchiveEntry archive;
        archive.name = std::move(name);
        archive.size_bytes = static_cast<std::int64_t>(size);
        out.push_back(std::move(archive));
    }
    if (err) {
        throw WorldStoreError("snapshot_failed",
                              "cannot list " + dir.string() + ": " + err.message());
    }
    std::ranges::sort(out, std::ranges::greater{}, &ArchiveEntry::name);
    return out;
}

std::size_t purge_rollback_archives(const RegistryDb& reg, const std::string& world) {
    const auto listed = list_rollback_archives(reg, world);
    const fs::path dir = checked_world_home(reg.data_dir(), world, reg.get(world).dir_id) / "archive";
    std::size_t removed = 0;
    for (const auto& item : listed) {
        if (!remove_within(dir, dir / item.name)) {
            throw WorldStoreError("snapshot_failed", "archive " + item.name + " resolves outside " +
                                                         dir.string() + "; refusing to delete");
        }
        ++removed;
    }
    return removed;
}


// ---- 自动快照与会话清单（W4） -------------------------------------------------

namespace {

[[noreturn]] void invalid_auto(const std::string& detail) {
    throw WorldStoreError("invalid_auto_snapshot", detail);
}

// 列表里满足 pred 的条目中 created_at 最新的一份（按解析后的时间比较；解析不了的算最旧，
// 只有全都解析不了时才轮到它；同一时刻取列表里靠前的，即注册表里更新的那份）。没有返回 nullptr。
// 「解析不了的算最旧」是有意的取舍：真正最新的那份时间若坏了，判断会落到更旧的一份上。
// 只有手改过的注册表才会出现这种条目：take_snapshot 写进去的 created_at 不是 iso_now()，
// 就是校验过的 now_iso。
// listed 是该世界的快照（RegistryDb::snapshots 的顺序，最新登记的在前）；返回的指针指向 listed。
template <typename Pred>
const SnapshotEntry* newest_snapshot(const std::vector<SnapshotEntry>& listed, const Pred& pred) {
    const SnapshotEntry* best = nullptr;
    std::optional<std::chrono::sys_seconds> best_at;
    for (const auto& snap : listed) {
        if (!pred(snap)) { continue; }
        const auto stamp = parse_utc_seconds(snap.created_at);
        const bool newer = best == nullptr || (stamp && (!best_at || *stamp > *best_at));
        if (newer) {
            best = &snap;
            best_at = stamp;
        }
    }
    return best;
}

// 库里有没有这个会话的 unit（按租户）。库文件或表不存在算没有。
bool session_known(const WorldRecord& entry, const std::string& session_id) {
    std::error_code err;
    if (!fs::exists(entry.db_path, err)) { return false; }
    return with_read_only(entry.db_path, [&](sqlite3* dbh) {
        if (!table_exists(dbh, "source_unit_meta")) { return false; }
        persistence::Stmt query(dbh, "SELECT 1 FROM source_unit_meta "
                                     "WHERE tenant_id=?1 AND session_id=?2 LIMIT 1");
        query.bind(entry.tenant, session_id);
        return query.step_row();
    });
}

// 当前的内容标记；库文件不存在时为空串（与「没有内容」相同）。
std::string current_content_mark(const WorldRecord& entry) {
    std::error_code err;
    if (!fs::exists(entry.db_path, err)) { return {}; }
    return with_read_only(entry.db_path,
                          [&](sqlite3* dbh) { return content_mark_on(dbh, entry.tenant); });
}

AutoSnapshotResult skipped_because(std::string reason) {
    AutoSnapshotResult result;
    result.skipped = std::move(reason);
    return result;
}

// 自动快照拍之前的修剪（Codex I-3）。take_snapshot 吞掉修剪失败（手动快照有人在场，暂时多一份）；
// 自动快照无人值守，修剪卡在同一个旧条目上（路径越界、解析不了、删不掉）时每次触发都会多登记
// 一份，一直涨到磁盘守卫。所以先修剪一次：修剪抛了、而快照数仍 >= 保留份数，返回拒绝原因（非空），
// 调用方不拍。修剪成功（或本来就不超出）返回空串，照常拍，take_snapshot 自己的修剪随后把总数压回
// 保留份数。稳态（恰好等于保留份数、修剪无事可做）不受影响；卡住时自动触发的快照数至多停在保留份数 + 1。
//
// 不抛（Codex r2 R2-2）：apply_retention 逐个「先删文件、再摘注册项」，卡住的条目之前排着的正常 victim
// 已经删掉文件、摘掉注册项（已写入 registry.db）。返回拒绝原因，调用方计为失败。
std::string retention_refusal(RegistryDb& reg, const std::string& world) {
    std::string prune_error;
    try {
        (void)apply_retention(reg, world);
    } catch (const WorldStoreError& err) {
        prune_error = err.what();
    }
    if (prune_error.empty()) { return {}; }
    const std::size_t kept = prunable(reg.snapshots(world)).size();   // held 不占份数
    const int retention = reg.snapshot_retention();
    if (!std::cmp_greater_equal(kept, retention)) { return {}; }
    return "retention cannot prune the snapshots of '" + world + "' (" + std::to_string(kept) +
           " kept, retention " + std::to_string(retention) +
           "); refusing an auto snapshot: " + prune_error;
}

}  // namespace

AutoSnapshotResult take_auto_snapshot(RegistryDb& reg, const std::string& world,
                                      const AutoTrigger& trigger, const SnapshotOptions& base) {
    const WorldRecord entry = reg.get(world);   // world_not_found
    if (trigger.kind != "session" && trigger.kind != "interval") {
        invalid_auto("trigger kind must be 'session' or 'interval', got '" + trigger.kind + "'");
    }
    const auto now = parse_utc_seconds(trigger.now_iso);
    if (!now) { invalid_auto("now_iso is not YYYY-MM-DDTHH:MM:SSZ: '" + trigger.now_iso + "'"); }
    // 裁定 R12：第 5 步的修剪与拍快照都会删文件，先查注册表行与布局；违反时抛，不当成「拒绝拍」。
    validate_world(reg, world);
    // 向量在库文件之外的后端拍不出完整的世界：直接不拍，不算失败（用户裁定 2026-10-28）。
    if (base.vector_backend != "sqlite") { return skipped_because("unsupported_backend"); }

    // 配置在世界设置的 background.auto_snapshot 里（裁定 R10）。范围已由 settings_from_json 与
    // validate_world 查过（裁定 R11），这里再查一次是纵深防御。
    const AutoSnapshotConfig config =
        settings_from_json(entry.settings_json).background.auto_snapshot;
    require_valid_auto_snapshot(config);
    SnapshotOptions opts = base;
    opts.kind = "auto";
    opts.now_iso = trigger.now_iso;   // 与「间隔到没到」用同一个时钟：id 前缀与 created_at 都取它
    if (trigger.kind == "session") {
        if (!config.on_session) { return skipped_because("disabled"); }
        if (trigger.session_id.empty()) { invalid_auto("session trigger needs a session_id"); }
        if (session_known(entry, trigger.session_id)) { return skipped_because("known_session"); }
        // 调用方在写入之前触发：写入反复失败（契约拒绝、策略拒绝、SQLite 错误）时会话一直是新的，
        // 每次重试都会走到这里。同一会话已有内容相同的按会话快照就不再拍，否则几次重试就把更早
        // 会话的锚点和按时间的快照挤出保留策略。任意一份都算，不只看最新的 auto：两次重试之间
        // 拍了按时间的快照也不重复拍。两个空标记也算相同（新世界首个会话反复失败）。
        const std::string mark = current_content_mark(entry);
        if (std::ranges::any_of(reg.snapshots(world), [&](const SnapshotEntry& snap) {
                return snap.kind == "auto" && snap.reason == "session" &&
                       snap.session_id == trigger.session_id && snap.content_mark == mark;
            })) {
            return skipped_because("unchanged");
        }
        opts.reason = "session";
        opts.session_id = trigger.session_id;
    } else {
        if (config.interval_hours == 0) { return skipped_because("disabled"); }
        const std::string mark = current_content_mark(entry);
        if (mark.empty()) { return skipped_because("empty"); }
        // manual 快照不参与：按时间的自动快照只看上一份自动快照（任意 reason）。
        const std::vector<SnapshotEntry> listed = reg.snapshots(world);
        const SnapshotEntry* last = newest_snapshot(
            listed, [](const SnapshotEntry& snap) { return snap.kind == "auto"; });
        if (last != nullptr) {
            // 解析不了的 created_at、没有标记的旧条目：对应那一项按「需要拍」处理。
            // last 晚于 now（时钟回拨，或那份快照的时间来自跳到未来的时钟）也算「到了」：
            // 否则一份未来时间的条目会让按时间的快照一直停到真实时间追上它。标记照常比较，
            // 内容没变仍是 unchanged，最坏多拍一份。
            const auto last_at = parse_utc_seconds(last->created_at);
            if (last_at && *now >= *last_at &&
                *now - *last_at < std::chrono::hours{config.interval_hours}) {
                return skipped_because("not_due");
            }
            if (!last->content_mark.empty() && last->content_mark == mark) {
                return skipped_because("unchanged");
            }
        }
        opts.reason = "interval";
        opts.session_id.clear();
    }
    AutoSnapshotResult result;
    result.refused = retention_refusal(reg, world);
    if (!result.refused.empty()) { return result; }   // 注册表已部分修剪：调用方计为失败
    result.taken = take_snapshot(reg, world, opts);
    return result;
}

SessionList list_sessions(const RegistryDb& reg, const std::string& world, std::int64_t limit) {
    const WorldRecord entry = reg.get(world);   // world_not_found
    constexpr std::int64_t kMaxSessionRows = 1000;
    if (limit < 1 || limit > kMaxSessionRows) {
        invalid_auto("limit must be 1-1000, got " + std::to_string(limit));
    }
    SessionList out;
    std::error_code err;
    if (!fs::exists(entry.db_path, err)) { return out; }
    with_read_only(entry.db_path, [&](sqlite3* dbh) {
        if (!table_exists(dbh, "source_unit_meta")) { return; }
        // 一个读事务（这是独立的只读连接，不是引擎连接）：总数、无时间计数与各行出自
        // 同一个版本的库。
        persistence::Stmt(dbh, "BEGIN").step_done();
        {
            persistence::Stmt totals(dbh,
                "SELECT COUNT(DISTINCT session_id), COALESCE(SUM(observed_at IS NULL), 0) "
                "FROM source_unit_meta WHERE tenant_id=?1");
            totals.bind(entry.tenant);
            if (totals.step_row()) {
                out.total = sqlite3_column_int64(totals.raw(), 0);
                out.null_observed = sqlite3_column_int64(totals.raw(), 1);
            }
        }
        {
            // 设计稿 §7 的汇总。空串的 observed_at 与 NULL 同样不算时间（同 world_time_on）。
            persistence::Stmt rows(dbh,
                "SELECT session_id, COUNT(*) AS units, MIN(NULLIF(observed_at, '')) AS first_at, "
                "MAX(NULLIF(observed_at, '')) AS last_at, COUNT(DISTINCT speaker) AS speakers "
                "FROM source_unit_meta WHERE tenant_id=?1 GROUP BY session_id "
                "ORDER BY first_at IS NULL, first_at DESC, session_id LIMIT ?2");
            rows.bind(entry.tenant, limit);
            while (rows.step_row()) {
                SessionRow row;
                row.session_id = column_text(rows.raw(), 0);
                row.units = sqlite3_column_int64(rows.raw(), 1);
                row.first_at = column_text(rows.raw(), 2);
                row.last_at = column_text(rows.raw(), 3);
                row.speakers = sqlite3_column_int64(rows.raw(), 4);
                out.rows.push_back(std::move(row));
            }
        }
        persistence::Stmt(dbh, "COMMIT").step_done();
    });
    out.truncated = out.total > limit;

    // 锚点：每个会话最新的一份按会话自动快照（kind=auto、reason=session、session_id 相同）。
    const std::vector<SnapshotEntry> listed = reg.snapshots(world);
    for (auto& row : out.rows) {
        const SnapshotEntry* anchor = newest_snapshot(listed, [&row](const SnapshotEntry& snap) {
            return snap.kind == "auto" && snap.reason == "session" &&
                   snap.session_id == row.session_id;
        });
        if (anchor != nullptr) {
            row.anchor_snapshot_id = anchor->id;
            row.anchor_created_at = anchor->created_at;
        }
    }
    return out;
}

// ---- 分叉、种子与升级种子（spec §6.1、§6.2） ------------------------------------

namespace {

// 来源库 schema_migrations 的最高版本；表不存在算 0（与 SqliteAdapter::schema_version 同一规则）。
int schema_version_on(sqlite3* dbh) {
    if (!table_exists(dbh, "schema_migrations")) { return 0; }
    return static_cast<int>(single_int(dbh, "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"));
}

// 来源这条连接看得见的「有没有新提交」：data_version 在别的连接提交后变，total_changes 在这条连接
// 自己写了之后变（裁定 R11）。复制前后各读一次，不等就是复制途中有写入。
struct WriteMark {
    std::int64_t data_version = 0;
    std::int64_t own_changes = 0;
    bool operator==(const WriteMark&) const = default;
};

WriteMark write_mark(sqlite3* dbh) {
    return WriteMark{.data_version = single_int(dbh, "PRAGMA data_version"),
                     .own_changes = sqlite3_total_changes64(dbh),};
}

void require_current_schema(sqlite3* dbh, const std::string& source) {
    const int have = schema_version_on(dbh);
    const int want = persistence::MigrationRunner::latest_embedded_version();
    if (have < want) {
        throw WorldStoreError("world_schema_behind",
                              "source '" + source + "' is at schema version " +
                                  std::to_string(have) + ", this build needs " +
                                  std::to_string(want));
    }
}

void vacuum_into(sqlite3* dbh, const fs::path& target) {
    persistence::Stmt vacuum(dbh, "VACUUM INTO ?1");
    vacuum.bind(target.string());
    vacuum.step_done();
}

void require_intact(const fs::path& copy) {
    const auto handle = open_read_only(copy.string());
    persistence::Stmt check(handle.get(), "PRAGMA integrity_check");
    if (!(check.step_row() && column_text(check.raw(), 0) == "ok")) {
        throw WorldStoreError("snapshot_failed", "integrity_check failed for " + copy.string());
    }
}

// 分叉与升级种子复制出来的 partial，改名、登记之前的两项检查：integrity_check（snapshot_failed），然后
// 租户不变式（裁定 R29，与回滚第 6–7 步同一个函数 verify_world_tenant）：VACUUM INTO 复制整个文件，来源库里
// 别的租户的行也跟着过来，这样的世界每次打开都 world_tenant_mismatch，所以在这里就拒绝。
void require_intact_single_tenant(const fs::path& partial, const std::string& tenant) {
    require_intact(partial);
    verify_world_tenant(partial.string(), tenant);
}

// 非 WorldStoreError（SqliteError、filesystem_error 等）转成 snapshot_failed，消息带上 what。
template <typename Body>
auto as_world_store_error(const std::string& what, const Body& body) {
    try {
        return body();
    } catch (const WorldStoreError&) {
        throw;
    } catch (const std::exception& err) {
        throw WorldStoreError("snapshot_failed", what + ": " + err.what());
    }
}

// 分叉第 1 步（任何文件 I/O 之前）：新名、来源、来源的布局、新世界的设置。返回来源的记录与设置。
struct ForkPlan {
    WorldRecord source;
    std::string settings_json;
    WorldFlags flags;
};

ForkPlan plan_fork(const RegistryDb& reg, const std::string& source, const std::string& new_name,
                   const ForkOptions& opts) {
    if (!is_valid_world_name(new_name)) {
        throw WorldStoreError("invalid_world_name", "'" + new_name +
                                                        "' does not match [A-Za-z0-9][A-Za-z0-9._-]{0,127}");
    }
    try {
        (void)reg.get(new_name);
        throw WorldStoreError("world_exists", "world '" + new_name + "' exists");
    } catch (const WorldStoreError& err) {
        if (err.code() != "world_not_found") { throw; }
    }
    ForkPlan plan;
    plan.source = reg.get(source);   // world_not_found
    validate_world(reg, source);
    require_sqlite_vectors(opts.vector_backend);   // 裁定 R31 M4：与快照、回滚同一个判定
    plan.flags = WorldFlags{.seed = opts.seed, .read_only = opts.seed,
                            .reclaimable = opts.reclaimable,
                            .retention_group = opts.retention_group, .fork_of_world = source,
                            .fork_of_snapshot = opts.snapshot_id,};
    const WorldSettings merged = merge_settings(
        settings_from_json(plan.source.settings_json),
        opts.settings_overrides_json.empty() ? "{}" : opts.settings_overrides_json,
        plan.flags.read_only);
    plan.settings_json = settings_to_json(merged);
    return plan;
}

NewWorld new_world_of(const ForkPlan& plan, const std::string& new_name, const ForkOptions& opts) {
    return NewWorld{.name = new_name, .tenant = plan.source.tenant, .label = "",
                    .labels = opts.labels, .settings_json = plan.settings_json,
                    .now_iso = opts.now_iso.empty() ? iso_now() : opts.now_iso,
                    .flags = plan.flags, .provider_endpoints = opts.provider_endpoints,};
}

// SQLite 拒绝在这条连接上 VACUUM 的两种情况：连接开着事务，或还有没走完的语句。共用的引擎连接上
// 这是「别的调用正在用它」，与复制途中有写入同属瞬态，报 world_busy。
bool vacuum_refused_as_busy(const persistence::SqliteError& err) {
    const std::string_view text = err.what();
    return text.find("cannot VACUUM from within a transaction") != std::string_view::npos ||
           text.find("SQL statements in progress") != std::string_view::npos;
}

[[noreturn]] void source_busy(const std::string& source, const std::string& why) {
    throw WorldStoreError("world_busy", "source '" + source + "' " + why + "; retry");
}

// 分叉第 3 步：从 source_conn 复制到 db_file，返回摘要。见 world_store.hpp fork_world_from_connection。
// 调用方已查过 schema（第 2 步，require_current_schema）。
std::string copy_verified(persistence::Connection& source_conn, const std::string& source,
                          const std::string& tenant, const fs::path& db_file,
                          const ForkOptions& opts) {
    sqlite3* dbh = source_conn.raw();
    const fs::path partial = db_file.string() + ".partial";
    // 连接开着事务时 data_version 与摘要读到的是这个事务里的视图（可能含没提交的写入），写入标记也就
    // 不能说明「复制期间没人写」：在取标记之前拒绝。
    if (sqlite3_get_autocommit(dbh) == 0) { source_busy(source, "has a transaction open"); }
    const WriteMark before = write_mark(dbh);
    const std::string expected = memoryops::digest(source_conn, tenant);
    try {
        vacuum_into(dbh, partial);
    } catch (const persistence::SqliteError& err) {
        std::error_code ignore;
        fs::remove(partial, ignore);
        if (vacuum_refused_as_busy(err)) { source_busy(source, "is in use by another call"); }
        throw;
    }
    if (opts.after_copy_for_test) { opts.after_copy_for_test(partial); }
    if (write_mark(dbh) != before) {
        throw WorldStoreError("world_busy", "source '" + source +
                                                "' was written while it was being copied; retry");
    }
    require_intact_single_tenant(partial, tenant);
    const std::string copied = digest_of_file(partial, tenant);
    if (copied != expected) {
        throw WorldStoreError("fork_digest_mismatch",
                              "the copy of '" + source + "' has digest " + copied +
                                  ", the source has " + expected);
    }
    fs::rename(partial, db_file);
    return copied;
}

}  // namespace

ForkResult fork_world_from_connection(RegistryDb& reg, persistence::Connection& source_conn,
                                      const std::string& source, const std::string& new_name,
                                      const ForkOptions& opts) {
    if (!opts.snapshot_id.empty()) {
        throw std::invalid_argument(
            "fork_world_from_connection forks the open world itself; use fork_world for a snapshot");
    }
    const ForkPlan plan = plan_fork(reg, source, new_name, opts);
    if (source_conn.raw() == nullptr) {
        throw std::invalid_argument("fork source connection is closed");
    }
    ForkResult result;
    result.world = as_world_store_error("fork of '" + source + "'", [&] {
        require_current_schema(source_conn.raw(), source);   // 第 2 步：建目录之前
        return reg.create_populated(new_world_of(plan, new_name, opts), [&](const fs::path& db_file) {
            result.digest = copy_verified(source_conn, source, plan.source.tenant, db_file, opts);
        });
    });
    return result;
}

ForkResult fork_world(RegistryDb& reg, const std::string& source, const std::string& new_name,
                      const ForkOptions& opts) {
    const ForkPlan plan = plan_fork(reg, source, new_name, opts);
    fs::path from = plan.source.db_path;
    if (!opts.snapshot_id.empty()) {
        // 快照为来源：路径只从注册表取，解析后必须在来源的 snapshot_dir 之下（与回滚同一边界）。
        const SnapshotEntry snap = registered_snapshot(reg, source, opts.snapshot_id);
        require_inside_registry(plan.source.snapshot_dir, snap.path);
        require_snapshot_file(snap.path);
        from = snap.path;
    } else {
        std::error_code exists_err;
        if (!fs::exists(from, exists_err)) {
            throw WorldStoreError("snapshot_failed",
                                  "database file of '" + source + "' does not exist");
        }
    }
    ForkResult result;
    result.world = as_world_store_error("fork of '" + source + "'", [&] {
        auto conn = persistence::Connection::open_read_only(from);
        require_current_schema(conn.raw(), source);   // 第 2 步：建目录之前
        return reg.create_populated(new_world_of(plan, new_name, opts), [&](const fs::path& db_file) {
            result.digest = copy_verified(conn, source, plan.source.tenant, db_file, opts);
        });
    });
    return result;
}

// NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
ForkResult upgrade_seed(RegistryDb& reg, const std::string& source, const std::string& new_name,
                        const std::string& now_iso) {
    if (!reg.get(source).seed) {   // world_not_found
        throw WorldStoreError("world_not_seed", "world '" + source + "' is not a seed");
    }
    ForkOptions opts;
    opts.seed = true;
    opts.now_iso = now_iso;
    ForkPlan plan = plan_fork(reg, source, new_name, opts);
    opts.labels = plan.source.labels;
    NewWorld spec = new_world_of(plan, new_name, opts);
    spec.label = plan.source.label;
    ForkResult result;
    result.world = as_world_store_error("upgrade of seed '" + source + "'", [&] {
        auto conn = persistence::Connection::open_read_only(plan.source.db_path);
        return reg.create_populated(spec, [&](const fs::path& db_file) {
            const fs::path partial = db_file.string() + ".partial";
            vacuum_into(conn.raw(), partial);
            require_intact_single_tenant(partial, plan.source.tenant);
            {
                auto adapter = persistence::SqliteAdapter::open(partial);   // 跑迁移到最新
                adapter->close();                                            // WAL 合并进主文件
            }
            for (const char* suffix : {"-wal", "-journal"}) {
                std::error_code err;
                if (fs::exists(partial.string() + suffix, err) || err) {
                    throw WorldStoreError("snapshot_failed",
                                          "migrated copy of seed '" + source + "' left " +
                                              partial.filename().string() + suffix);
                }
            }
            {
                std::error_code shm_err;
                fs::remove(partial.string() + "-shm", shm_err);   // 不带状态（wal-index）
            }
            fs::rename(partial, db_file);
            result.digest = digest_of_file(db_file, plan.source.tenant);
        });
    });
    return result;
}

namespace internal {

std::string random_hex() { return governance::random_hex(); }
std::string iso_now() { return governance::iso_now(); }
std::string compact_now() { return governance::compact_now(); }
bool remove_within(const fs::path& base, const fs::path& target) {
    return governance::remove_within(base, target);
}
void require_worlds_root_inside(const fs::path& data_dir, const fs::path& worlds_root) {
    governance::require_worlds_root_inside(data_dir, worlds_root);
}
// NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
void require_resolved_home(const fs::path& worlds_root, const std::string& dir_id,
                           const std::string& code, const std::string& world) {
    governance::require_resolved_home(worlds_root, dir_id, code, world);
}
// NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
void require_plain_dir_id(const std::string& dir_id, const std::string& world,
                          const std::string& code) {
    governance::require_plain_dir_id(dir_id, world, code);
}
void require_valid_retention(int retention) { governance::require_valid_retention(retention); }
// NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
void check_world_shape(const std::string& name, const std::string& dir_id,
                       const std::string& db_path, const std::string& snapshot_dir,
                       bool is_default) {
    governance::check_world_shape(name, dir_id, db_path, snapshot_dir, is_default);
}
// NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
void check_world_layout(const fs::path& data_dir, const std::string& name,
                        const std::string& dir_id, const std::string& db_path,
                        const std::string& snapshot_dir, bool is_default) {
    governance::check_world_layout(data_dir, name, dir_id, db_path, snapshot_dir, is_default);
}
void check_auto_snapshot(const std::string& name, const AutoSnapshotConfig& config) {
    governance::check_auto_snapshot(name, config);
}
// NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
void check_snapshot_entry(const std::string& owner, const std::string& snapshot_dir,
                          const SnapshotEntry& snap) {
    governance::check_snapshot_entry(owner, snapshot_dir, snap);
}

}  // namespace internal

}  // namespace starling::governance
