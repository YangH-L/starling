#include "starling/governance/error_catalog.hpp"

namespace starling::governance {

namespace {

constexpr ErrorInfo entry(std::string_view code, bool transient, int http_status) {
    return ErrorInfo{.code = code, .transient = transient, .http_status = http_status};
}

}  // namespace

const std::vector<ErrorInfo>& error_catalog() {
    // 不含 world_switching、ingest_busy：它们随活动世界退役（spec §13.2 末段）。
    static const std::vector<ErrorInfo> kCatalog = {
        entry("world_not_found", false, 404),
        entry("world_exists", false, 409),
        entry("invalid_world_name", false, 400),
        entry("world_held", false, 409),
        entry("world_busy", true, 503),
        entry("world_read_only", false, 409),
        entry("world_schema_behind", false, 409),
        entry("world_settings_mismatch", false, 409),
        entry("labels_immutable", false, 409),
        entry("fork_digest_mismatch", false, 500),
        entry("restore_digest_mismatch", false, 500),
        entry("snapshot_not_found", false, 404),
        // 裁定 R42：以下三个沿用已发布的 409（测试与前端已钉住）。
        entry("snapshot_corrupt", false, 409),
        entry("insufficient_disk", false, 507),
        entry("event_conflict", false, 409),
        entry("event_not_found", false, 404),
        entry("recall_degraded", true, 503),
        entry("handle_closed", false, 409),
        entry("session_expired", false, 401),
        entry("endpoint_changed", false, 409),
        entry("endpoint_key_missing", false, 409),
        entry("protocol_mismatch", false, 426),
        entry("data_dir_in_use", false, 409),
        entry("world_protected", false, 409),
        entry("world_tenant_mismatch", false, 409),
        entry("world_in_use", false, 409),
        entry("invalid_settings", false, 400),
        entry("invalid_labels", false, 400),
        entry("invalid_world_query", false, 400),
        entry("invalid_auto_snapshot", false, 400),
        entry("invalid_retention", false, 409),
        entry("invalid_snapshot_kind", false, 400),
        entry("registry_corrupt", false, 500),
        entry("snapshot_failed", false, 500),
        entry("snapshot_unsupported_backend", false, 409),
        entry("worlds_unavailable", true, 503),
        entry("world_not_initialized", false, 409),
        // 裁定 R43：第 1~3 期已在用、计划表漏掉的码。
        entry("world_not_seed", false, 409),
        entry("confirm_mismatch", false, 400),
        entry("rollback_mode_unsupported", false, 400),
        entry("rollback_reopen_failed", false, 500),
        entry("world_open_failed", false, 500),
        // Task 4.5：客户端路由的请求错误（未知方法、未知管理操作、参数形状不对），与抽取角色没绑定（看板旧码，状态沿用）。
        entry("invalid_request", false, 400),
        entry("llm_not_configured", false, 409),
        // 最后修复波 A5–A7（裁定 R91(5)、R48、R100 M1）：bearer token 错或没带；检视路由按 id 找不到行；路由兜底的服务端错误。
        entry("unauthorized", false, 401),
        entry("not_found", false, 404),
        entry("internal_error", false, 500),
    };
    return kCatalog;
}

std::optional<ErrorInfo> lookup_error(std::string_view code) {
    for (const ErrorInfo& info : error_catalog()) {
        if (info.code == code) {
            return info;
        }
    }
    return std::nullopt;
}

}  // namespace starling::governance
