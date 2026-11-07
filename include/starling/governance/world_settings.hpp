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
#include <string_view>

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
    std::optional<EndpointSettings> consolidation;  // 裁定 R25：离线回放里判 NORM gist 的模型
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
// 清除（裁定 R24）：extraction / embedding / chat / consolidation 的值为 JSON null 时删掉该 endpoint，
// gist_thresholds 为 null 时重置为 {}。只有这五个键收 null，其余位置的 null 仍是 invalid_settings。
WorldSettings settings_from_json(const std::string& text);
// 序列化：可选字段（max_retries / enable_thinking / 四个 endpoint）未设置时不写出。
std::string settings_to_json(const WorldSettings& settings);

// 两份设置是否相同（裁定 R78，客户端 open(settings=…) 用）：两边各经 settings_from_json 补齐默认值后逐字段比较，所以键序、
// 缺省的键与显式给出的默认值都不影响结果。stored_json 是注册表里存着的设置（不是按打开方式算的生效设置）。任一边非法时抛
// settings_from_json 的错误（invalid_settings 等）。
[[nodiscard]] bool settings_equal(const std::string& stored_json, const std::string& given_json);

// overrides 是部分设置（JSON 对象，只含要覆盖的顶层键与子键）；没给的叶子从 base 复制。
// 合并本身删不掉键，所以要「解绑一个角色」「清空阈值」时 overrides 给 null（规则同 settings_from_json）：
// {"extraction": null} 删掉 base 里的 extraction，{"gist_thresholds": null} 把阈值重置为 {}。
// read_only=true 时 background 强制全关（tick 0、ingest false、auto_snapshot {false,0}）。
// 非法 overrides 在返回前抛出（与 settings_from_json 同一套规则，含 interval_hours 的范围；只读时
// 也先校验再强制关后台）；base 不被修改。
WorldSettings merge_settings(const WorldSettings& base, const std::string& overrides_json,
                             bool read_only);

// 整段替换（裁定 R27）：给权威来源同步用（默认世界跟 config，见 RegistryDb::replace_world_sections）。
// sections 里出现的顶层段 extraction / embedding / chat / consolidation / gist_thresholds / source_lane /
// vector_backend 整段换成给出的值（没给的叶子取默认值，不从 base 复制），null 照样清除（同 merge_settings）；
// background 仍逐叶合并（config 不带 auto_snapshot、ingest，裁定 R16）；sections 里没出现的段原样保留。
// 校验规则与 settings_from_json 相同，非法时在返回前抛出，base 不被修改。
WorldSettings replace_sections(const WorldSettings& base, const std::string& sections_json);

// ProviderEndpoint（一个 provider 此刻的端点身份，裁定 R51）定义在 world_store.hpp：分叉的 ForkOptions 带着它。

// world 里的 provider_name 非空时核对该 provider 此刻的端点（current）：provider 已不存在（nullopt，相当于
// {"", ""}，一律拒绝）、或种类与 base_url 任一与 world 记的不同 → 抛 endpoint_changed。provider_name 为空
// （客户端显式给的）永不抛。role（extraction / embedding / chat / consolidation）只用于消息：消息只点名
// provider 与角色，不带地址（裁定 R54，spec §16）。
void require_endpoint_unchanged(const EndpointSettings& world_endpoint,
                                const std::optional<ProviderEndpoint>& current,
                                std::string_view role);

// 嵌入 endpoint 从 before 换成 after 时，世界里已有的向量是否作废、要重嵌入（裁定 R58）。向量的空间由嵌入的模型与维度决定，
// 与地址、密钥、种类无关，所以：after 为空（角色解绑，例如 provider 被删掉）→ false，向量留着（召回降级到重新绑定为止）；
// before 为空、after 不为空 → true（之前是占位嵌入）；两者都在时 model 或 dim 任一不同 → true，只换地址 → false。
bool embedding_needs_reembed(const std::optional<EndpointSettings>& before,
                             const std::optional<EndpointSettings>& after);

// 摄入分区目录名：默认世界 "tenant-" + 百分号编码(tenant)，其余 "world-" + dir_id。
// 编码与 Python urllib.parse.quote(tenant, safe='') 逐字节一致。
std::string ingest_partition(bool is_default_world, const std::string& tenant,
                             const std::string& dir_id);

}  // namespace starling::governance
