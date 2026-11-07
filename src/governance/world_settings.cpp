#include "starling/governance/world_settings.hpp"

#include <nlohmann/json.hpp>

#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <utility>

namespace starling::governance {

namespace {

using Json = nlohmann::json;

[[noreturn]] void fail(const std::string& path, const std::string& why) {
    throw WorldStoreError("invalid_settings", path + ": " + why);
}

std::string join(const std::string& prefix, const std::string& key) {
    return prefix.empty() ? key : prefix + "." + key;
}

void require_object(const Json& val, const std::string& path) {
    if (!val.is_object()) {
        fail(path.empty() ? "<root>" : path, "必须是 JSON 对象");
    }
}

void read_str(const Json& val, const std::string& path, std::string& out) {
    if (!val.is_string()) {
        fail(path, "必须是字符串");
    }
    out = val.get<std::string>();
}
void read_int(const Json& val, const std::string& path, std::int64_t& out) {
    if (!val.is_number_integer()) {
        fail(path, "必须是整数");
    }
    out = val.get<std::int64_t>();
}
void read_dbl(const Json& val, const std::string& path, double& out) {
    if (!val.is_number()) {
        fail(path, "必须是数字");
    }
    out = val.get<double>();
}
void read_bool(const Json& val, const std::string& path, bool& out) {
    if (!val.is_boolean()) {
        fail(path, "必须是布尔值");
    }
    out = val.get<bool>();
}

// 把 obj 里给出的叶子覆盖到 out 上；未给的保持不变。未知键报 invalid_settings。
void apply_endpoint(const Json& obj, const std::string& path, EndpointSettings& out) {
    require_object(obj, path);
    for (const auto& [key, val] : obj.items()) {
        const std::string sub = join(path, key);
        if (key == "provider") {
            read_str(val, sub, out.provider);
        } else if (key == "provider_name") {
            read_str(val, sub, out.provider_name);
        } else if (key == "base_url") {
            read_str(val, sub, out.base_url);
        } else if (key == "model") {
            read_str(val, sub, out.model);
        } else if (key == "thinking_type") {
            read_str(val, sub, out.thinking_type);
        } else if (key == "dim") {
            read_int(val, sub, out.dim);
        } else if (key == "max_tokens") {
            read_int(val, sub, out.max_tokens);
        } else if (key == "timeout_ms") {
            read_int(val, sub, out.timeout_ms);
        } else if (key == "max_retries") {
            std::int64_t num = 0;
            read_int(val, sub, num);
            out.max_retries = num;
        } else if (key == "enable_thinking") {
            bool flag = false;
            read_bool(val, sub, flag);
            out.enable_thinking = flag;
        } else {
            fail(sub, "未知键");
        }
    }
}

// null 删掉该 endpoint（裁定 R24：合并删不掉键，解绑角色要显式清除）；对象逐键覆盖。
void apply_optional_endpoint(const Json& obj, const std::string& path,
                             std::optional<EndpointSettings>& out) {
    if (obj.is_null()) {
        out.reset();
        return;
    }
    require_object(obj, path);
    EndpointSettings work = out.value_or(EndpointSettings{});
    apply_endpoint(obj, path, work);
    out = std::move(work);
}

void apply_source_lane(const Json& obj, const std::string& path, SourceLaneSettings& out) {
    require_object(obj, path);
    for (const auto& [key, val] : obj.items()) {
        const std::string sub = join(path, key);
        if (key == "enabled") {
            read_bool(val, sub, out.enabled);
        } else if (key == "render_time") {
            read_bool(val, sub, out.render_time);
        } else if (key == "tau_source") {
            read_dbl(val, sub, out.tau_source);
        } else if (key == "model") {
            read_str(val, sub, out.model);
        } else if (key == "dim") {
            read_int(val, sub, out.dim);
        } else if (key == "k_source") {
            read_int(val, sub, out.k_source);
        } else if (key == "chunk_byte_budget") {
            read_int(val, sub, out.chunk_byte_budget);
        } else {
            fail(sub, "未知键");
        }
    }
}

constexpr std::int64_t kMaxIntervalHours = 8760;   // 一年，与 world_store 的 set_auto_snapshot 相同

void apply_auto_snapshot(const Json& obj, const std::string& path, AutoSnapshotConfig& out) {
    require_object(obj, path);
    for (const auto& [key, val] : obj.items()) {
        const std::string sub = join(path, key);
        if (key == "on_session") {
            read_bool(val, sub, out.on_session);
        } else if (key == "interval_hours") {
            // 大于 int64 的无符号数直接按越界拒，不让 get<int64_t> 回绕成负数。
            if (val.is_number_unsigned() &&
                val.get<std::uint64_t>() > static_cast<std::uint64_t>(kMaxIntervalHours)) {
                throw WorldStoreError("invalid_auto_snapshot", sub + ": 必须在 0–8760 之间");
            }
            read_int(val, sub, out.interval_hours);
            // 裁定 R11：越界的值写不进注册表（每条写路径都经 merge_settings），否则 validate_world
            // 在启动时拒绝整个注册表。码与 set_auto_snapshot 相同。
            if (out.interval_hours < 0 || out.interval_hours > kMaxIntervalHours) {
                throw WorldStoreError("invalid_auto_snapshot",
                                      sub + ": 必须在 0–8760 之间，得到 " +
                                          std::to_string(out.interval_hours));
            }
        } else {
            fail(sub, "未知键");
        }
    }
}

void apply_background(const Json& obj, const std::string& path, BackgroundSettings& out) {
    require_object(obj, path);
    for (const auto& [key, val] : obj.items()) {
        const std::string sub = join(path, key);
        if (key == "tick_interval_s") {
            read_dbl(val, sub, out.tick_interval_s);
        } else if (key == "ingest") {
            read_bool(val, sub, out.ingest);
        } else if (key == "auto_snapshot") {
            apply_auto_snapshot(val, sub, out.auto_snapshot);
        } else {
            fail(sub, "未知键");
        }
    }
}

void apply_settings(const Json& root, WorldSettings& out) {
    require_object(root, "");
    for (const auto& [key, val] : root.items()) {
        if (key == "extraction") {
            apply_optional_endpoint(val, key, out.extraction);
        } else if (key == "embedding") {
            apply_optional_endpoint(val, key, out.embedding);
        } else if (key == "chat") {
            apply_optional_endpoint(val, key, out.chat);
        } else if (key == "consolidation") {
            apply_optional_endpoint(val, key, out.consolidation);
        } else if (key == "vector_backend") {
            std::string backend;
            read_str(val, key, backend);
            if (backend != "sqlite") {
                throw WorldStoreError("snapshot_unsupported_backend",
                                      "vector_backend 只支持 sqlite，收到 " + backend);
            }
            out.vector_backend = backend;
        } else if (key == "source_lane") {
            apply_source_lane(val, key, out.source_lane);
        } else if (key == "gist_thresholds") {
            if (val.is_null()) {   // 裁定 R24：null 重置为 {}（C++ 默认阈值）
                out.gist_thresholds.clear();
                continue;
            }
            require_object(val, key);
            for (const auto& [name, thr] : val.items()) {
                double num = 0.0;
                read_dbl(thr, join(key, name), num);
                out.gist_thresholds[name] = num;
            }
        } else if (key == "background") {
            apply_background(val, key, out.background);
        } else {
            fail(key, "未知键");
        }
    }
}

Json parse_object(const std::string& text, const std::string& what) {
    Json root = Json::parse(text, nullptr, false);
    if (root.is_discarded()) {
        fail(what, "不是合法 JSON");
    }
    require_object(root, what);
    return root;
}

// 往 JSON 对象写一个键；operator[] 只在这里用一次。
void put(Json& obj, const std::string& key, Json val) {
    obj.emplace(key, std::move(val));
}

Json endpoint_to_json(const EndpointSettings& endpoint) {
    Json obj = Json::object();
    put(obj, "provider", endpoint.provider);
    put(obj, "provider_name", endpoint.provider_name);
    put(obj, "base_url", endpoint.base_url);
    put(obj, "model", endpoint.model);
    put(obj, "dim", endpoint.dim);
    put(obj, "max_tokens", endpoint.max_tokens);
    put(obj, "timeout_ms", endpoint.timeout_ms);
    put(obj, "thinking_type", endpoint.thinking_type);
    if (endpoint.max_retries.has_value()) {
        put(obj, "max_retries", *endpoint.max_retries);
    }
    if (endpoint.enable_thinking.has_value()) {
        put(obj, "enable_thinking", *endpoint.enable_thinking);
    }
    return obj;
}

// 百分号编码：保留 [A-Za-z0-9_.~-]，其余每个字节编成 %XX（大写十六进制）。
std::string percent_encode(const std::string& text) {
    static constexpr std::string_view kHex = "0123456789ABCDEF";
    std::string out;
    for (const char chr : text) {
        const auto byte = static_cast<unsigned char>(chr);
        const bool keep = (byte >= 'A' && byte <= 'Z') || (byte >= 'a' && byte <= 'z') ||
                          (byte >= '0' && byte <= '9') || byte == '_' || byte == '.' ||
                          byte == '-' || byte == '~';
        if (keep) {
            out.push_back(chr);
        } else {
            out.push_back('%');
            out.push_back(kHex.at(byte >> 4U));
            out.push_back(kHex.at(byte & 0x0FU));
        }
    }
    return out;
}

}  // namespace

WorldSettings settings_from_json(const std::string& text) {
    WorldSettings out;
    apply_settings(parse_object(text, "settings"), out);
    return out;
}

std::string settings_to_json(const WorldSettings& settings) {
    Json root = Json::object();
    if (settings.extraction.has_value()) {
        put(root, "extraction", endpoint_to_json(*settings.extraction));
    }
    if (settings.embedding.has_value()) {
        put(root, "embedding", endpoint_to_json(*settings.embedding));
    }
    if (settings.chat.has_value()) {
        put(root, "chat", endpoint_to_json(*settings.chat));
    }
    if (settings.consolidation.has_value()) {
        put(root, "consolidation", endpoint_to_json(*settings.consolidation));
    }
    put(root, "vector_backend", settings.vector_backend);

    const SourceLaneSettings& lane = settings.source_lane;
    Json lane_json = Json::object();
    put(lane_json, "enabled", lane.enabled);
    put(lane_json, "render_time", lane.render_time);
    put(lane_json, "tau_source", lane.tau_source);
    put(lane_json, "model", lane.model);
    put(lane_json, "dim", lane.dim);
    put(lane_json, "k_source", lane.k_source);
    put(lane_json, "chunk_byte_budget", lane.chunk_byte_budget);
    put(root, "source_lane", std::move(lane_json));

    Json gist = Json::object();
    for (const auto& [name, thr] : settings.gist_thresholds) {
        put(gist, name, thr);
    }
    put(root, "gist_thresholds", std::move(gist));

    const BackgroundSettings& background = settings.background;
    Json snap = Json::object();
    put(snap, "on_session", background.auto_snapshot.on_session);
    put(snap, "interval_hours", background.auto_snapshot.interval_hours);
    Json bg_json = Json::object();
    put(bg_json, "tick_interval_s", background.tick_interval_s);
    put(bg_json, "ingest", background.ingest);
    put(bg_json, "auto_snapshot", std::move(snap));
    put(root, "background", std::move(bg_json));
    return root.dump();
}

// NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
bool settings_equal(const std::string& stored_json, const std::string& given_json) {
    return settings_from_json(stored_json) == settings_from_json(given_json);
}

WorldSettings merge_settings(const WorldSettings& base, const std::string& overrides_json,
                             bool read_only) {
    const Json overrides = parse_object(overrides_json, "overrides");
    WorldSettings out = base;  // 值拷贝；非法 overrides 在这里之后抛，base 不受影响
    apply_settings(overrides, out);
    if (read_only) {
        out.background = BackgroundSettings{
            .tick_interval_s = 0.0,
            .ingest = false,
            .auto_snapshot = {.on_session = false, .interval_hours = 0},
        };
    }
    return out;
}

WorldSettings replace_sections(const WorldSettings& base, const std::string& sections_json) {
    const Json sections = parse_object(sections_json, "sections");
    WorldSettings out = base;  // 值拷贝；非法 sections 在这里之后抛，base 不受影响
    // 先把出现的整段（background 除外）复位成默认值，再按同一套规则应用：没给的叶子就停在默认值上。
    const WorldSettings defaults;
    for (const auto& [key, val] : sections.items()) {
        (void)val;
        if (key == "extraction") {
            out.extraction.reset();
        } else if (key == "embedding") {
            out.embedding.reset();
        } else if (key == "chat") {
            out.chat.reset();
        } else if (key == "consolidation") {
            out.consolidation.reset();
        } else if (key == "gist_thresholds") {
            out.gist_thresholds.clear();
        } else if (key == "source_lane") {
            out.source_lane = defaults.source_lane;
        } else if (key == "vector_backend") {
            out.vector_backend = defaults.vector_backend;
        }
        // background 与未知键不复位：background 逐叶合并，未知键由 apply_settings 报 invalid_settings。
    }
    apply_settings(sections, out);
    return out;
}

void require_endpoint_unchanged(const EndpointSettings& world_endpoint,
                                const std::optional<ProviderEndpoint>& current,
                                std::string_view role) {
    if (world_endpoint.provider_name.empty()) {
        return;
    }
    // 裁定 R54：消息只点名 provider 与角色，不带地址（spec §16：错误响应与日志里没有地址）。
    const std::string who =
        "provider " + world_endpoint.provider_name + "（角色 " + std::string(role) + "）";
    if (!current.has_value()) {
        throw WorldStoreError("endpoint_changed", who + " 已不存在");
    }
    if (current->provider != world_endpoint.provider ||
        current->base_url != world_endpoint.base_url) {
        throw WorldStoreError("endpoint_changed", who + " 的种类或地址已改动");
    }
}

bool embedding_needs_reembed(const std::optional<EndpointSettings>& before,
                             const std::optional<EndpointSettings>& after) {
    if (!after.has_value()) {
        return false;
    }
    if (!before.has_value()) {
        return true;
    }
    return before->model != after->model || before->dim != after->dim;
}

std::string ingest_partition(bool is_default_world, const std::string& tenant,
                             const std::string& dir_id) {
    return is_default_world ? "tenant-" + percent_encode(tenant) : "world-" + dir_id;
}

}  // namespace starling::governance
