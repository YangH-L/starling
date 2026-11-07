// 构建信息（spec §13.4，裁定 R88）：build_commit() 是构建时 `git rev-parse HEAD` 的结果，拿不到时是 "unknown"。
#include "starling/build_info.hpp"

#include <gtest/gtest.h>

#include <algorithm>
#include <string>

namespace {

TEST(BuildInfo, CommitIsUnknownOrFortyLowercaseHex) {
    const char* raw = starling::build_commit();
    ASSERT_NE(raw, nullptr);
    const std::string commit(raw);
    ASSERT_FALSE(commit.empty());
    if (commit == "unknown") { return; }
    EXPECT_EQ(commit.size(), 40U) << commit;
    EXPECT_TRUE(std::ranges::all_of(commit, [](char chr) {
        return (chr >= '0' && chr <= '9') || (chr >= 'a' && chr <= 'f');
    })) << commit;
}

}  // namespace
