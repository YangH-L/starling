#include "starling/governance/error_catalog.hpp"

namespace starling::governance {

const std::vector<ErrorInfo>& error_catalog() {
    // 不含 world_switching、ingest_busy：它们随活动世界退役（spec §13.2 末段）。
    static const std::vector<ErrorInfo> kCatalog = {
        {"world_not_found", false, 404},
        {"world_exists", false, 409},
        {"invalid_world_name", false, 400},
        {"world_held", false, 409},
        {"world_busy", true, 503},
        {"world_read_only", false, 409},
        {"world_schema_behind", false, 409},
        {"world_settings_mismatch", false, 409},
        {"labels_immutable", false, 409},
        {"fork_digest_mismatch", false, 500},
        {"restore_digest_mismatch", false, 500},
        {"snapshot_not_found", false, 404},
        // 裁定 R42：以下三个沿用已发布的 409（测试与前端已钉住）。
        {"snapshot_corrupt", false, 409},
        {"insufficient_disk", false, 507},
        {"event_conflict", false, 409},
        {"event_not_found", false, 404},
        {"recall_degraded", true, 503},
        {"handle_closed", false, 409},
        {"session_expired", false, 401},
        {"endpoint_changed", false, 409},
        {"endpoint_key_missing", false, 409},
        {"protocol_mismatch", false, 426},
        {"data_dir_in_use", false, 409},
        {"world_protected", false, 409},
        {"world_tenant_mismatch", false, 409},
        {"world_in_use", false, 409},
        {"invalid_settings", false, 400},
        {"invalid_labels", false, 400},
        {"invalid_world_query", false, 400},
        {"invalid_auto_snapshot", false, 400},
        {"invalid_retention", false, 409},
        {"invalid_snapshot_kind", false, 400},
        {"registry_corrupt", false, 500},
        {"snapshot_failed", false, 500},
        {"snapshot_unsupported_backend", false, 409},
        {"worlds_unavailable", true, 503},
        {"world_not_initialized", false, 409},
        // 裁定 R43：第 1~3 期已在用、计划表漏掉的码。
        {"world_not_seed", false, 409},
        {"confirm_mismatch", false, 400},
        {"rollback_mode_unsupported", false, 400},
        {"rollback_reopen_failed", false, 500},
        {"world_open_failed", false, 500},
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
