// 错误码目录：spec §13.2 的码与瞬态标志、HTTP 状态；src/governance 抛出的码与看板旧 _STATUS 表的键都要在册。
#include "starling/governance/error_catalog.hpp"

#include <gtest/gtest.h>

#include <set>
#include <string>
#include <string_view>

namespace {

using starling::governance::error_catalog;
using starling::governance::lookup_error;

struct Expected {
    std::string_view code;
    bool transient;
    int http_status;
};

constexpr Expected kSpecCodes[] = {
    {"world_not_found", false, 404}, {"world_exists", false, 409},
    {"invalid_world_name", false, 400}, {"world_held", false, 409},
    {"world_busy", true, 503}, {"world_read_only", false, 409},
    {"world_schema_behind", false, 409}, {"world_settings_mismatch", false, 409},
    {"labels_immutable", false, 409}, {"fork_digest_mismatch", false, 500},
    {"restore_digest_mismatch", false, 500}, {"snapshot_not_found", false, 404},
    {"snapshot_corrupt", false, 409}, {"insufficient_disk", false, 507},
    {"event_conflict", false, 409}, {"event_not_found", false, 404},
    {"recall_degraded", true, 503}, {"handle_closed", false, 409},
    {"session_expired", false, 401}, {"endpoint_changed", false, 409},
    {"endpoint_key_missing", false, 409}, {"protocol_mismatch", false, 426},
    {"data_dir_in_use", false, 409}, {"world_protected", false, 409},
    {"world_tenant_mismatch", false, 409}, {"world_in_use", false, 409},
    {"invalid_settings", false, 400}, {"invalid_labels", false, 400},
    {"invalid_world_query", false, 400}, {"invalid_auto_snapshot", false, 400},
    {"invalid_retention", false, 409}, {"invalid_snapshot_kind", false, 400},
    {"registry_corrupt", false, 500}, {"snapshot_failed", false, 500},
    {"snapshot_unsupported_backend", false, 409}, {"worlds_unavailable", true, 503},
    {"world_not_initialized", false, 409}, {"world_not_seed", false, 409},
    {"confirm_mismatch", false, 400}, {"rollback_mode_unsupported", false, 400},
    {"rollback_reopen_failed", false, 500}, {"world_open_failed", false, 500},
    // Task 4.5：客户端路由的请求错误（未知方法、参数形状不对）与抽取角色没绑定。
    {"invalid_request", false, 400}, {"llm_not_configured", false, 409},
    // 最后修复波 A5–A7：bearer token 错（401）、检视路由找不到行（404）、兜底的服务端错误（500）。
    {"unauthorized", false, 401}, {"not_found", false, 404}, {"internal_error", false, 500},
};

// src/governance/*.cpp 里以字面量给出的全部码：WorldStoreError("<code>" 与 WorldStoreError::after_replace("<code>"，
// 含参数跨行的调用（正则 WorldStoreError(::after_replace)?\(\s*"([a-z_]+)"）。以变量传码的几处（unresolvable、
// 布局校验）只传 registry_corrupt / snapshot_failed，两者已在表内。session_table.cpp 贡献 handle_closed、
// session_expired、world_held、world_in_use；registry_db.cpp 的 require_writable 贡献 world_held、world_read_only。
constexpr std::string_view kThrownBySrcGovernance[] = {
    "endpoint_changed", "endpoint_key_missing", "fork_digest_mismatch", "handle_closed",
    "insufficient_disk", "invalid_auto_snapshot", "invalid_labels", "invalid_retention", "invalid_settings",
    "invalid_snapshot_kind", "invalid_world_name", "invalid_world_query", "labels_immutable",
    "registry_corrupt", "restore_digest_mismatch", "session_expired", "snapshot_corrupt",
    "snapshot_failed", "snapshot_not_found", "snapshot_unsupported_backend", "world_busy",
    "world_exists", "world_held", "world_in_use", "world_not_found", "world_not_seed",
    "world_protected", "world_read_only", "world_schema_behind", "world_tenant_mismatch",
};

// 看板两张旧 _STATUS 表（routes/worlds.py、routes/world_scope.py）的键并集。
constexpr std::string_view kLegacyStatusKeys[] = {
    "invalid_world_name", "confirm_mismatch", "rollback_mode_unsupported", "invalid_auto_snapshot",
    "invalid_settings", "invalid_labels", "invalid_world_query", "world_not_found",
    "snapshot_not_found", "world_exists", "world_protected", "labels_immutable",
    "snapshot_unsupported_backend", "invalid_retention", "endpoint_changed",
    "world_tenant_mismatch", "snapshot_corrupt", "world_in_use", "insufficient_disk",
    "world_busy", "worlds_unavailable", "world_open_failed", "snapshot_failed",
    "registry_corrupt", "rollback_reopen_failed", "world_read_only", "world_schema_behind",
    "world_not_seed", "fork_digest_mismatch", "restore_digest_mismatch",
};

TEST(ErrorCatalog, EverySpecCodePresentWithTransientFlag) {
    for (const Expected& want : kSpecCodes) {
        const auto info = lookup_error(want.code);
        ASSERT_TRUE(info.has_value()) << want.code;
        EXPECT_EQ(info->transient, want.transient) << want.code;
        EXPECT_EQ(info->http_status, want.http_status) << want.code;
    }
    EXPECT_TRUE(lookup_error("world_busy")->transient);
    EXPECT_TRUE(lookup_error("recall_degraded")->transient);
    EXPECT_EQ(error_catalog().size(), std::size(kSpecCodes));
}

TEST(ErrorCatalog, EveryWorldStoreErrorCodeIsCataloged) {
    for (const std::string_view code : kThrownBySrcGovernance) {
        EXPECT_TRUE(lookup_error(code).has_value()) << code;
    }
    for (const std::string_view code : kLegacyStatusKeys) {
        EXPECT_TRUE(lookup_error(code).has_value()) << code;
    }
}

TEST(ErrorCatalog, CodesAreUniqueAndRetiredCodesAbsent) {
    std::set<std::string> seen;
    for (const auto& info : error_catalog()) {
        EXPECT_TRUE(seen.insert(std::string(info.code)).second) << info.code;
    }
    EXPECT_FALSE(lookup_error("world_switching").has_value());
    EXPECT_FALSE(lookup_error("ingest_busy").has_value());
    EXPECT_FALSE(lookup_error("no_such_code").has_value());
}

}  // namespace
