// WorldSettings：往返、继承合并、只读关后台、非法输入、endpoint_changed、摄入分区名。
#include "starling/governance/world_settings.hpp"

#include <gtest/gtest.h>

#include <cstdint>
#include <string>

namespace {

using namespace starling::governance;

EndpointSettings make_endpoint(const std::string& tag) {
    EndpointSettings ep;
    ep.provider = "openai";
    ep.provider_name = "prov-" + tag;
    ep.base_url = "http://" + tag;
    ep.model = "model-" + tag;
    ep.dim = 768;
    ep.max_tokens = 4096;
    ep.timeout_ms = 90000;
    ep.max_retries = 3;
    ep.enable_thinking = false;
    ep.thinking_type = "disabled";
    return ep;
}

template <typename Fn>
std::string error_code(Fn&& fn) {
    try {
        fn();
    } catch (const WorldStoreError& err) {
        return err.code();
    }
    return "";
}

TEST(WorldSettings, RoundTripAllFields) {
    WorldSettings src;
    src.extraction = make_endpoint("x");
    src.embedding = make_endpoint("e");
    src.chat = make_endpoint("c");
    src.chat->max_retries.reset();  // 可选字段缺省也要往返
    src.chat->enable_thinking = true;
    src.source_lane = {.enabled = false,
                       .render_time = true,
                       .tau_source = 0.7,
                       .model = "other-model",
                       .dim = 512,
                       .k_source = 60,
                       .chunk_byte_budget = 16000};
    src.gist_thresholds = {{"a", 0.25}, {"b", 0.75}};
    src.background = {.tick_interval_s = 12.5,
                      .ingest = false,
                      .auto_snapshot = {.on_session = true, .interval_hours = 6}};
    const std::string text = settings_to_json(src);
    EXPECT_EQ(settings_from_json(text), src);
    EXPECT_EQ(settings_to_json(settings_from_json(text)), text);
}

TEST(WorldSettings, EmptyObjectIsDefaults) {
    const WorldSettings got = settings_from_json("{}");
    EXPECT_EQ(got, WorldSettings{});
    EXPECT_FALSE(got.extraction.has_value());
    EXPECT_FALSE(got.embedding.has_value());
    EXPECT_FALSE(got.chat.has_value());
    EXPECT_EQ(got.vector_backend, "sqlite");
    EXPECT_EQ(settings_from_json(settings_to_json(WorldSettings{})), WorldSettings{});
}

TEST(WorldSettings, MergeCopiesMissingKeysFromBase) {
    WorldSettings base;
    base.extraction = make_endpoint("a");
    base.extraction->model = "m1";
    WorldSettings merged = merge_settings(base, R"({"extraction":{"model":"m2"}})", false);
    ASSERT_TRUE(merged.extraction.has_value());
    EXPECT_EQ(merged.extraction->model, "m2");
    EXPECT_EQ(merged.extraction->base_url, "http://a");
    EXPECT_EQ(base.extraction->model, "m1");  // base 不被改

    EXPECT_EQ(merge_settings(base, "{}", false), base);

    // 值拷贝：之后改 base 不影响已合并结果
    base.extraction->base_url = "http://changed";
    base.background.ingest = false;
    EXPECT_EQ(merged.extraction->base_url, "http://a");
    EXPECT_TRUE(merged.background.ingest);
}

TEST(WorldSettings, MergeNestedAndNewEndpoint) {
    const WorldSettings base;
    const WorldSettings merged = merge_settings(
        base,
        R"({"chat":{"model":"cm"},"source_lane":{"k_source":99},)"
        R"("gist_thresholds":{"k":0.5},"background":{"auto_snapshot":{"interval_hours":3}}})",
        false);
    ASSERT_TRUE(merged.chat.has_value());
    EXPECT_EQ(merged.chat->model, "cm");
    EXPECT_EQ(merged.chat->base_url, "");
    EXPECT_EQ(merged.source_lane.k_source, 99);
    EXPECT_EQ(merged.source_lane.dim, 1024);
    EXPECT_EQ(merged.gist_thresholds.at("k"), 0.5);
    EXPECT_EQ(merged.background.auto_snapshot.interval_hours, 3);
    EXPECT_FALSE(merged.background.auto_snapshot.on_session);
    EXPECT_FALSE(merged.extraction.has_value());
}

TEST(WorldSettings, GistThresholdsOverrideReplacesIndividualKeys) {
    WorldSettings base;
    base.gist_thresholds = {{"a", 0.1}, {"b", 0.2}};
    const WorldSettings merged = merge_settings(base, R"({"gist_thresholds":{"b":0.9,"c":0.3}})", false);
    EXPECT_EQ(merged.gist_thresholds.size(), 3U);
    EXPECT_EQ(merged.gist_thresholds.at("a"), 0.1);
    EXPECT_EQ(merged.gist_thresholds.at("b"), 0.9);
    EXPECT_EQ(merged.gist_thresholds.at("c"), 0.3);
}

TEST(WorldSettings, ReadOnlyForcesBackgroundOff) {
    const WorldSettings merged =
        merge_settings(WorldSettings{}, R"({"background":{"tick_interval_s":5}})", true);
    EXPECT_EQ(merged.background.tick_interval_s, 0.0);
    EXPECT_FALSE(merged.background.ingest);
    EXPECT_EQ(merged.background.auto_snapshot, (AutoSnapshotConfig{.on_session = false, .interval_hours = 0}));
}

TEST(WorldSettings, RejectsZvecAndUnknownKeys) {
    EXPECT_EQ(error_code([] { settings_from_json(R"({"vector_backend":"zvec"})"); }),
              "snapshot_unsupported_backend");
    EXPECT_EQ(error_code([] { settings_from_json(R"({"foo":1})"); }), "invalid_settings");
    EXPECT_EQ(error_code([] { merge_settings(WorldSettings{}, R"({"vector_backend":"zvec"})", false); }),
              "snapshot_unsupported_backend");
    EXPECT_EQ(error_code([] { merge_settings(WorldSettings{}, R"({"foo":1})", false); }),
              "invalid_settings");
    EXPECT_EQ(error_code([] { merge_settings(WorldSettings{}, R"({"chat":{"bar":1}})", false); }),
              "invalid_settings");
}

TEST(WorldSettings, RejectsWrongTypesNamingPath) {
    try {
        settings_from_json(R"({"extraction":{"dim":"big"}})");
        FAIL() << "应抛 invalid_settings";
    } catch (const WorldStoreError& err) {
        EXPECT_EQ(err.code(), "invalid_settings");
        EXPECT_NE(std::string(err.what()).find("extraction.dim"), std::string::npos);
    }
    EXPECT_EQ(error_code([] { settings_from_json("not json"); }), "invalid_settings");
    EXPECT_EQ(error_code([] { settings_from_json("[]"); }), "invalid_settings");
    EXPECT_EQ(error_code([] { settings_from_json(R"({"background":{"ingest":1}})"); }),
              "invalid_settings");
    EXPECT_EQ(error_code([] { merge_settings(WorldSettings{}, R"({"source_lane":{"enabled":"yes"}})", false); }),
              "invalid_settings");
}

// 裁定 R11：background.auto_snapshot.interval_hours 只收 0–8760，越界抛 invalid_auto_snapshot。
// settings_from_json 与 merge_settings 都查（每条写路径都经 merge_settings），所以 registry.db 里存不进
// validate_world 会拒的值，服务不会因为一次设置更新而起不来。
std::string with_interval(std::int64_t hours) {
    return R"({"background":{"auto_snapshot":{"interval_hours":)" + std::to_string(hours) + "}}}";
}

TEST(WorldSettings, FromJsonRejectsIntervalOutOfRange) {
    for (const std::int64_t bad : {std::int64_t{8761}, std::int64_t{-1}}) {
        EXPECT_EQ(error_code([&] { (void)settings_from_json(with_interval(bad)); }),
                  "invalid_auto_snapshot") << bad;
    }
    for (const std::int64_t fine : {std::int64_t{0}, std::int64_t{8760}}) {
        EXPECT_EQ(settings_from_json(with_interval(fine)).background.auto_snapshot.interval_hours,
                  fine);
    }
}

TEST(WorldSettings, MergeRejectsIntervalOutOfRangeAndLeavesBaseAlone) {
    WorldSettings base;
    base.background.auto_snapshot = {.on_session = true, .interval_hours = 6};
    for (const std::int64_t bad : {std::int64_t{8761}, std::int64_t{-1}}) {
        EXPECT_EQ(error_code([&] { (void)merge_settings(base, with_interval(bad), false); }),
                  "invalid_auto_snapshot") << bad;
        // 只读世界会把 background 强制关掉，越界的 overrides 照样拒（不因随后被覆盖就放行）。
        EXPECT_EQ(error_code([&] { (void)merge_settings(base, with_interval(bad), true); }),
                  "invalid_auto_snapshot") << bad;
    }
    EXPECT_EQ(base.background.auto_snapshot.interval_hours, 6);
    for (const std::int64_t fine : {std::int64_t{0}, std::int64_t{8760}}) {
        EXPECT_EQ(merge_settings(base, with_interval(fine), false)
                      .background.auto_snapshot.interval_hours, fine);
    }
}

TEST(WorldSettings, EndpointChanged) {
    EndpointSettings ep;
    ep.provider_name = "p";
    ep.base_url = "http://a";
    EXPECT_NO_THROW(require_endpoint_unchanged(ep, "http://a"));
    EXPECT_EQ(error_code([&] { require_endpoint_unchanged(ep, "http://b"); }), "endpoint_changed");
    ep.provider_name.clear();
    EXPECT_NO_THROW(require_endpoint_unchanged(ep, "http://b"));
}

TEST(WorldSettings, IngestPartition) {
    EXPECT_EQ(ingest_partition(true, "default", "x"), "tenant-default");
    EXPECT_EQ(ingest_partition(true, "a/b", "x"), "tenant-a%2Fb");
    EXPECT_EQ(ingest_partition(false, "default", "3f9a"), "world-3f9a");
    // 与 Python quote(safe='') 逐字节一致
    EXPECT_EQ(ingest_partition(true, "租户", "x"), "tenant-%E7%A7%9F%E6%88%B7");
    EXPECT_EQ(ingest_partition(true, "a b~._-\xC3\xA9/", "x"), "tenant-a%20b~._-%C3%A9%2F");
}

}  // namespace
