#pragma once

// registry_db.hpp — 记忆服务的世界注册表（SQLite，<data_dir>/registry.db）。
//
// 设计稿：docs/superpowers/specs/2026-11-03-memory-service-design.md §5。
// 世界、标签、快照条目、统计缓存都在这里。磁盘上的世界目录用 dir_id
// （16 位随机十六进制）命名，世界名只是注册表里的键，所以大小写不同的名字互不冲突。
// 本类只加表与方法；旧的 worlds.json 函数（world_store.hpp）由后续任务迁移。
//
// 线程安全：每个公开方法持内部互斥锁，连接以 FULLMUTEX 打开，可在多线程间共享。
// 错误一律抛 WorldStoreError（code 见 world_store.hpp）。

#include "starling/governance/world_store.hpp"

#include <cstdint>
#include <filesystem>
#include <map>
#include <memory>
#include <string>
#include <string_view>
#include <vector>

namespace starling::governance {

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

    RegistryDb(RegistryDb&&) noexcept;
    RegistryDb& operator=(RegistryDb&&) noexcept;
    RegistryDb(const RegistryDb&) = delete;
    RegistryDb& operator=(const RegistryDb&) = delete;
    ~RegistryDb();

    [[nodiscard]] std::string default_world() const;
    [[nodiscard]] WorldRecord get(const std::string& name) const;   // world_not_found
    [[nodiscard]] WorldPage list(const WorldQuery& query) const;    // 按 created_at 降序
    // invalid_world_name / world_exists。建 worlds/<dir_id>/ 与 snapshots/，不建库文件。
    WorldRecord create(const std::string& name, const std::string& label,
                       const std::map<std::string, std::string>& labels,
                       const std::string& settings_json, std::string_view now_iso);
    // default → world_protected；删注册项（含标签、快照条目）与 worlds/<dir_id>/。
    // dir_id 越出 worlds/ 或布局被符号链接篡改 → snapshot_failed，什么都不删。
    void remove(const std::string& name);
    // 已有标签 → labels_immutable
    void set_labels(const std::string& name, const std::map<std::string, std::string>& labels);
    void update_settings(const std::string& name, const std::string& settings_json);
    void update_stats(const std::string& name, std::int64_t holders, std::int64_t statements,
                      const std::string& last_write_at, std::string_view now_iso);
    // 快照条目（与 SnapshotEntry 同字段），最新在前（created_at 降序，id 降序兜底）
    [[nodiscard]] std::vector<SnapshotEntry> snapshots(const std::string& world) const;
    void put_snapshot(const std::string& world, const SnapshotEntry& entry);
    void drop_snapshot(const std::string& world, const std::string& snapshot_id);
    [[nodiscard]] int snapshot_retention() const;                   // 默认 5
    [[nodiscard]] const std::filesystem::path& data_dir() const;

private:
    struct Impl;
    explicit RegistryDb(std::unique_ptr<Impl> impl);
    std::unique_ptr<Impl> impl_;
};

}  // namespace starling::governance
