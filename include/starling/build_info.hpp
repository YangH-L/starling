#pragma once

// 构建信息（spec §13.4，裁定 R88）。服务的 info() 报出它：version 取 version.hpp 的 STARLING_VERSION_STRING，commit 取这里。

namespace starling {

// 构建时 `git rev-parse HEAD` 的结果（40 位小写十六进制）；构建时不在 git 仓库里、或没有 git 时为 "unknown"。从不为空。
// 每次构建都重新取（cmake/StarlingBuildInfo.cmake），提交变了才重编 src/build_info.cpp。
[[nodiscard]] const char* build_commit() noexcept;

}  // namespace starling
