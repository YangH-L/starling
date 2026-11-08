// 错误码目录：一处定义，每个码带「是否瞬态」与 HTTP 状态。HTTP 层与客户端都从这里取码
// （spec §13.2）。码本身是稳定的对外契约，只增不改。
#pragma once

#include <optional>
#include <string_view>
#include <vector>

namespace starling::governance {

struct ErrorInfo {
    std::string_view code;
    bool transient;
    int http_status;
};

// 未登记的码返回 nullopt。
[[nodiscard]] std::optional<ErrorInfo> lookup_error(std::string_view code);

// 全部码，顺序固定。
[[nodiscard]] const std::vector<ErrorInfo>& error_catalog();

}  // namespace starling::governance
