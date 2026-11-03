#pragma once

// world_settings.hpp — 每个世界的设置（纯数据 + JSON 逻辑）。
//
// 设计稿：docs/superpowers/specs/2026-11-03-memory-service-design.md §8。
// 解析/序列化、继承合并、只读强制关后台、endpoint_changed 判定、摄入分区名。
// 错误一律抛 WorldStoreError（world_store.hpp）。

#include "starling/governance/world_store.hpp"

#include <cstdint>
#include <map>
#include <optional>
#include <string>

namespace starling::governance {

struct EndpointSettings {
    std::string provider;       // "openai" | "anthropic"
    std::string provider_name;  // 引用 starling.json 的 provider；客户端显式给的为空
    std::string base_url;
    std::string model;
    std::int64_t dim = 0;
    std::int64_t max_tokens = 0;
    std::int64_t timeout_ms = 0;
    std::optional<std::int64_t> max_retries;
    std::optional<bool> enable_thinking;
    std::string thinking_type;  // "" | "disabled" | ...
    bool operator==(const EndpointSettings&) const = default;
};

struct BackgroundSettings {
    double tick_interval_s = 30.0;
    bool ingest = true;
    AutoSnapshotConfig auto_snapshot{.on_session = false, .interval_hours = 24};
    bool operator==(const BackgroundSettings&) const = default;
};

struct SourceLaneSettings {
    bool enabled = true;
    bool render_time = false;
    double tau_source = 0.56;
    std::string model = "text-embedding-v4";
    std::int64_t dim = 1024;
    std::int64_t k_source = 30;
    std::int64_t chunk_byte_budget = 32000;
    bool operator==(const SourceLaneSettings&) const = default;
};

struct WorldSettings {
    std::optional<EndpointSettings> extraction;
    std::optional<EndpointSettings> embedding;
    std::optional<EndpointSettings> chat;
    std::string vector_backend = "sqlite";
    SourceLaneSettings source_lane;
    std::map<std::string, double> gist_thresholds;
    BackgroundSettings background;
    bool operator==(const WorldSettings&) const = default;
};

// 解析：未知键、类型错误、非法 JSON 报 invalid_settings（消息带字段路径）；
// vector_backend 不是 "sqlite" 报 snapshot_unsupported_backend；background.auto_snapshot.interval_hours
// 不在 0–8760 报 invalid_auto_snapshot（与 set_auto_snapshot 同码；validate_world 拒绝的值因此存不进
// 注册表）。"{}" 得到全默认值。
WorldSettings settings_from_json(const std::string& text);
// 序列化：可选字段（max_retries / enable_thinking / 三个 endpoint）未设置时不写出。
std::string settings_to_json(const WorldSettings& settings);

// overrides 是部分设置（JSON 对象，只含要覆盖的顶层键与子键）；没给的叶子从 base 复制。
// read_only=true 时 background 强制全关（tick 0、ingest false、auto_snapshot {false,0}）。
// 非法 overrides 在返回前抛出（与 settings_from_json 同一套规则，含 interval_hours 的范围；只读时
// 也先校验再强制关后台）；base 不被修改。
WorldSettings merge_settings(const WorldSettings& base, const std::string& overrides_json,
                             bool read_only);

// 新世界的设置（裁定 R2）：默认世界当前设置 + overrides（空串当 "{}"）按 merge_settings 合并（只读 false），
// 序列化后返回，供 RegistryDb::create 的 settings_json。是值拷贝：此后改默认世界不影响已建的世界。
// 默认世界的 settings_json 解析不了、overrides 非法都按 settings_from_json 的码抛，什么都不写。
std::string settings_for_new_world(const RegistryDb& reg, const std::string& overrides_json);

// 把 overrides 合并到世界 name 当前的设置上（该世界只读时后台强制关），写回 registry.db，返回合并后的
// settings_json。世界不存在抛 world_not_found；overrides 非法时什么都不写。
std::string update_world_settings(RegistryDb& reg, const std::string& name,
                                  const std::string& overrides_json);

// world 里的 provider_name 非空，且 current_provider_base_url != world 记的 base_url
// → 抛 endpoint_changed。provider_name 为空（客户端显式给的）永不抛。
void require_endpoint_unchanged(const EndpointSettings& world_endpoint,
                                const std::string& current_provider_base_url);

// 摄入分区目录名：默认世界 "tenant-" + 百分号编码(tenant)，其余 "world-" + dir_id。
// 编码与 Python urllib.parse.quote(tenant, safe='') 逐字节一致。
std::string ingest_partition(bool is_default_world, const std::string& tenant,
                             const std::string& dir_id);

}  // namespace starling::governance
