#pragma once

// registry_db.hpp — 记忆服务的世界注册表（SQLite，<data_dir>/registry.db）。
//
// 设计稿：docs/superpowers/specs/2026-11-03-memory-service-design.md §5。
// 世界、标签、快照条目、统计缓存都在这里。磁盘上的世界目录用 dir_id
// （16 位随机十六进制）命名，世界名只是注册表里的键，所以大小写不同的名字互不冲突。
// 快照、回滚、自动快照、会话清单、归档（world_store.hpp）都读写这里；旧的 worlds.json 只剩
// import_worlds_json 一次性导入。
//
// 线程安全：每个公开方法持内部互斥锁，连接以 FULLMUTEX 打开，可在多线程间共享。这把锁只保护注册表
// 行：快照、回滚、修剪等多步操作在两次注册表调用之间不持它，调用方必须按世界串行化这些操作
// （原 WorldManager 的管理锁，见 world_store.hpp 文件头）。
// 错误一律抛 WorldStoreError（code 见 world_store.hpp）。

#include "starling/governance/world_settings.hpp"
#include "starling/governance/world_store.hpp"

#include <cstdint>
#include <filesystem>
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace starling::governance {

// {provider 名: 该 provider 此刻的种类与 base_url}：客户端写设置时核对继承来的 endpoint 用（裁定 R40、R51）。
// 表里没有的名字 = provider 已被删掉。
using ProviderEndpoints = std::map<std::string, ProviderEndpoint>;

struct WorldRecord {
    std::string name, dir_id, tenant, db_path, snapshot_dir, label, created_at;
    bool seed = false, read_only = false, reclaimable = false;  // 第 3、6 期才写入，本期恒为 false
    std::string retention_group, fork_of_world, fork_of_snapshot;  // 同上，本期为空
    std::string settings_json;                                    // 不透明文本，Task 3 定模式
    std::map<std::string, std::string> labels;
    std::int64_t holders = 0, statements = 0;                     // 统计缓存
    std::string last_write_at, stats_at;
};

struct WorldPage {
    std::vector<WorldRecord> worlds;
    std::int64_t total = 0;
};

struct WorldQuery {
    std::map<std::string, std::string> labels;   // 全部匹配（AND）
    std::int64_t offset = 0, limit = 50;         // limit 1–500
};

class RegistryDb {
public:
    // 打开或创建 <data_dir>/registry.db（WAL）。不存在时建表，并登记默认世界：
    //   name="default", tenant=default_tenant, db_path=default_db_path（原地，不受布局约束），
    //   dir_id="default", label="主世界", settings_json="{}"。
    // 文件打不开或不是合法的注册表：registry_corrupt。
    static RegistryDb open(const std::filesystem::path& data_dir,
                           const std::filesystem::path& default_db_path,
                           const std::string& default_tenant);

    // 服务启动的首次打开（裁定 R14）：<data_dir>/worlds.json 存在而 registry.db 不存在时先
    // import_worlds_json(data_dir)（两者都看 lstat），再 open，最后 validate_registry。
    // registry.db 已在时不导入。导入、open、校验的错误原样抛出。
    static RegistryDb open_or_import(const std::filesystem::path& data_dir,
                                     const std::filesystem::path& default_db_path,
                                     const std::string& default_tenant);

    RegistryDb(RegistryDb&&) noexcept;
    RegistryDb& operator=(RegistryDb&&) noexcept;
    RegistryDb(const RegistryDb&) = delete;
    RegistryDb& operator=(const RegistryDb&) = delete;
    ~RegistryDb();

    [[nodiscard]] std::string default_world() const;
    [[nodiscard]] WorldRecord get(const std::string& name) const;   // world_not_found
    [[nodiscard]] WorldPage list(const WorldQuery& query) const;    // 顺序见 registry_db.cpp list
    // invalid_world_name / world_exists。建 worlds/<dir_id>/ 与 snapshots/，不建库文件。
    WorldRecord create(const std::string& name, const std::string& label,
                       const std::map<std::string, std::string>& labels,
                       const std::string& settings_json, std::string_view now_iso);
    // 建世界并继承设置（裁定 R2），整个「读默认世界设置 → merge_settings(overrides) → 插入」在一次
    // 持锁内完成，并发的 update_world_settings("default", ...) 不会插进中间。新世界的设置是默认世界当时
    // 设置的值拷贝，之后改默认世界不影响它。overrides_json 空串当 "{}"，非法按 settings_from_json 的码抛
    // （invalid_settings 等），此时什么都不写。其余错误同 create。
    // 与 create 不同，它还初始化新世界的库文件（裁定 R34）：经持久层打开 worlds/<dir_id>/world.db 一次
    // （迁移建表）、随即关闭，然后才写注册项，所以新世界一登记就能按空库检视。不建引擎。初始化失败抛
    // snapshot_failed，世界目录删掉、注册项不写。初始化在注册表锁内（约十几毫秒）。
    //
    // provider_endpoints（裁定 R40、R51）：客户端写设置时给出 {provider 名: 该 provider 此刻的种类与 base_url}
    // （starling.json 的 provider 注册表）。给出时，合并结果里每个 provider_name 非空的 endpoint 都按
    // require_endpoint_unchanged 对表里该 provider 的种类与地址核对（表里没有这个名字 = provider 已被删掉，
    // 同样拒绝），任何一个不同就抛 endpoint_changed、什么都不写。合并、核对、写在同一次持锁内：同步默认世界设置（replace_world_sections）取同一把锁，插不进
    // 中间。不给（nullopt）时不核对：内部调用方（自动快照配置等）用，它们不改 endpoint。
    WorldRecord create_world(const std::string& name, const std::string& label,
                             const std::map<std::string, std::string>& labels,
                             const std::string& overrides_json, std::string_view now_iso,
                             const std::optional<ProviderEndpoints>& provider_endpoints = std::nullopt);
    // 把 overrides 合并到世界当前设置上（只读世界后台强制关）并写回，读—合并—写在一次持锁内，
    // 并发更新同一世界不会丢合并。返回合并后的 settings_json。world_not_found；overrides 非法时什么都不写。
    // provider_endpoints 同 create_world（裁定 R40、R51）：给出时在写之前于同一次持锁内核对，endpoint_changed 时
    // 什么都不写。
    std::string update_world_settings(
        const std::string& name, const std::string& overrides_json,
        const std::optional<ProviderEndpoints>& provider_endpoints = std::nullopt);
    // 重新确认端点（裁定 R53）：current 是 {provider 名: 该 provider 此刻的 endpoint}（与 config.world_settings()
    // 建 endpoint 同一套字段，不含密钥）。在一次持锁内，世界设置里每个 provider_name 非空的角色（extraction /
    // embedding / chat / consolidation）整段换成 current 里同名 provider 的 endpoint，provider_name 保持不变；
    // current 里没有这个名字（provider 已被删掉）的角色删掉（null，与默认世界删 provider 时一致）。provider_name
    // 为空的角色（客户端显式给的）与没设的角色不动。只读世界照样强制关后台。返回新的 settings_json。
    // world_not_found。不核对地址（这一步就是认下 provider 此刻的地址）；默认世界由调用方拒绝（它的端点归 config）。
    std::string reconfirm_world_endpoints(const std::string& name,
                                          const std::map<std::string, EndpointSettings>& current);
    // 权威来源同步（裁定 R27）：world_settings.hpp replace_sections（出现的段整段替换、background 逐叶合并、
    // null 清除），读—替换—写在一次持锁内。只读世界同样强制关后台。返回新的 settings_json。world_not_found；
    // sections 非法时什么都不写。
    std::string replace_world_sections(const std::string& name, const std::string& sections_json);
    // default → world_protected；删注册项（含标签、快照条目）与 worlds/<dir_id>/。
    // dir_id 越出 worlds/ 或布局被符号链接篡改 → snapshot_failed，什么都不删。
    void remove(const std::string& name);
    // 已有标签 → labels_immutable
    void set_labels(const std::string& name, const std::map<std::string, std::string>& labels);
    void update_settings(const std::string& name, const std::string& settings_json);
    void update_stats(const std::string& name, std::int64_t holders, std::int64_t statements,
                      const std::string& last_write_at, std::string_view now_iso);
    // 快照条目（与 SnapshotEntry 同字段），最新在前：按登记顺序，最后 put_snapshot 的在最前
    // （与 worlds.json 时代「插到列表开头」一致；不按 created_at，同一秒的几份才不会乱序）。
    [[nodiscard]] std::vector<SnapshotEntry> snapshots(const std::string& world) const;
    // 登记一份快照，成为该世界最新的一份。撞 (world, id) 或其他 SQLite 错误抛 snapshot_failed。
    void put_snapshot(const std::string& world, const SnapshotEntry& entry);
    void drop_snapshot(const std::string& world, const std::string& snapshot_id);
    [[nodiscard]] int snapshot_retention() const;                   // 默认 5
    [[nodiscard]] const std::filesystem::path& data_dir() const;

private:
    struct Impl;
    explicit RegistryDb(std::unique_ptr<Impl> impl);
    std::unique_ptr<Impl> impl_;
};

// 旧注册表 worlds.json 一次性导入（设计稿 §5.4）。在 <data_dir>/registry.db 不存在、
// <data_dir>/worlds.json 存在时调用（两者都看 lstat：悬空的符号链接也算「存在」）：
//   1. registry.db 已存在 → 什么都不做，返回（不读、不碰 worlds.json）；worlds.json 不存在 → 同样返回。
//   2. 读 worlds.json，按旧注册表的规则解析与校验（JSON 结构、字段类型、默认世界与快照条目、
//      auto_snapshot 形状，以及布局规则：世界目录 = <data_dir>/worlds/<世界名>，解析符号链接之后
//      判断，与 world_store.hpp validate_world 同一份代码）。旧格式的 "active" 键照常解析、忽略
//      （活动世界的概念已去掉）。默认世界的名字必须是 "default"（RegistryDb 只认这个名字）。
//   3. 删掉上次崩溃留下的 registry.db.importing 及其 -wal / -shm / -journal，在 registry.db.importing
//      里建库，一个事务写入全部世界与快照，关闭。每个世界 dir_id = 世界名（默认世界 "default"），
//      settings_json = settings_to_json(默认设置，background.auto_snapshot 取原值；原注册表缺这个字段
//      时补旧的默认值：默认世界 {false, 24}，其余 {true, 0})，导入的世界保持原来的自动快照行为
//      （新设置的默认值是 {false, 24}，不写明的话非默认世界会从按会话变成按时间）。默认世界的
//      db_path 照 worlds.json 原样保留（不在 worlds/ 布局之内，今天是 ~/.starling/dashboard.db）。
//   4. rename(registry.db.importing, registry.db)。
//   5. rename(worlds.json, worlds.json.imported-<compact_now>)（名字被占用就加 -<4 hex> 重试）。
// 第 4 步之前任何失败：删掉 registry.db.importing 与旁车，worlds.json 逐字节不变，抛
// registry_corrupt（原码不是 registry_corrupt 时写在消息里，例如 invalid_retention）。
// 第 5 步失败：registry.db 已就位、可以用，worlds.json 留在原处；抛 registry_corrupt 说明情况
// （下次调用见到 registry.db 就什么都不做，留着的 worlds.json 无害）。
// 不加锁：调用方保证同一个数据目录只有一个进程在导入（服务启动时、打开 RegistryDb 之前调用）。
void import_worlds_json(const std::filesystem::path& data_dir);

}  // namespace starling::governance
