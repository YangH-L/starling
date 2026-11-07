#include "starling/build_info.hpp"

namespace starling {

const char* build_commit() noexcept {
    // 内容是一个字符串字面量，由 cmake/StarlingBuildInfo.cmake 在每次构建时生成。
    return
#include "starling/build_commit.inc"
        ;
}

}  // namespace starling
