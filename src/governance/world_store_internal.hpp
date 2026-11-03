#pragma once

// world_store_internal.hpp — world_store.cpp 与 registry_db.cpp 共用的内部助手。
// 实现仍在 world_store.cpp 的匿名命名空间里，这里只导出同名转发，不改变其语义。
// 不属于公开 API，只供 src/governance 内部包含。

#include <filesystem>
#include <string>

namespace starling::governance::internal {

// 16 位随机小写十六进制。
[[nodiscard]] std::string random_hex();
// 2026-10-25T01:12:00Z
[[nodiscard]] std::string iso_now();
// target 解析符号链接与 ".." 之后严格位于 base 之下才递归删除；越界返回 false；出错抛 snapshot_failed。
bool remove_within(const std::filesystem::path& base, const std::filesystem::path& target);
// <registry_dir>/worlds 存在时必须仍解析在 registry_dir 之下，否则抛 registry_corrupt。
void require_worlds_root_inside(const std::filesystem::path& registry_dir,
                                const std::filesystem::path& worlds_root);
// worlds_root/<name> 解析后必须恰好是解析后的 worlds_root 下的 <name>，否则抛 code。
void require_resolved_home(const std::filesystem::path& worlds_root, const std::string& name,
                           const std::string& code);

}  // namespace starling::governance::internal
