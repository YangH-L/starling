#pragma once

// world_store_internal.hpp — world_store.cpp、registry_db.cpp 与 registry_import.cpp 共用的内部助手。
// 实现都在 world_store.cpp：validate_world 查 registry.db 的记录、导入前查 worlds.json 的条目，
// 用的是这里同一组布局规则。不属于公开 API，只供 src/governance 内部包含。

#include "starling/governance/world_store.hpp"

#include <filesystem>
#include <string>

namespace starling::governance::internal {

// 16 位随机小写十六进制。
[[nodiscard]] std::string random_hex();
// 2026-10-25T01:12:00Z
[[nodiscard]] std::string iso_now();
// 20261025T011200Z
[[nodiscard]] std::string compact_now();
// target 解析符号链接与 ".." 之后严格位于 base 之下才递归删除；越界返回 false；出错抛 snapshot_failed。
bool remove_within(const std::filesystem::path& base, const std::filesystem::path& target);
// <data_dir>/worlds 存在时必须仍解析在 data_dir 之下，否则抛 registry_corrupt。
void require_worlds_root_inside(const std::filesystem::path& data_dir,
                                const std::filesystem::path& worlds_root);
// worlds_root/<dir_id> 解析后必须恰好是解析后的 worlds_root 下的 <dir_id>，否则抛 code。
// 消息点名 world（世界名）；world 为空时点名 dir_id。
void require_resolved_home(const std::filesystem::path& worlds_root, const std::string& dir_id,
                           const std::string& code, const std::string& world = {});
// dir_id 必须是单独一级路径：非空、不是 "." / ".."、不含 '/' 与 '\\'，否则抛 code。
void require_plain_dir_id(const std::string& dir_id, const std::string& world,
                          const std::string& code);
// snapshot_retention < 2 抛 invalid_retention。
void require_valid_retention(int retention);

// ---- 注册表不变式（validate_world 与 import_worlds_json 共用，违反抛 registry_corrupt） ----

// 与目录布局无关的一层：非 default 世界的名字合法、dir_id 是单独一级路径、两个路径都是绝对路径。
void check_world_shape(const std::string& name, const std::string& dir_id,
                       const std::string& db_path, const std::string& snapshot_dir,
                       bool is_default);
// 布局规则 1–3（解析符号链接之后判断，见 world_store.hpp validate_world）。世界目录为
// <data_dir>/worlds/<dir_id>；先查 <data_dir>/worlds 本身仍在 data_dir 之下。
void check_world_layout(const std::filesystem::path& data_dir, const std::string& name,
                        const std::string& dir_id, const std::string& db_path,
                        const std::string& snapshot_dir, bool is_default);
// interval_hours 不在 0–8760 时抛 registry_corrupt（消息点名世界）。
void check_auto_snapshot(const std::string& name, const AutoSnapshotConfig& config);
// 一份快照条目：kind 是 manual / auto、kind 与 reason 配对、path 字面在 snapshot_dir 之下。
void check_snapshot_entry(const std::string& owner, const std::string& snapshot_dir,
                          const SnapshotEntry& snap);

}  // namespace starling::governance::internal
