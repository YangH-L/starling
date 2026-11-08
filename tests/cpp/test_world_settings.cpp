// WorldSettings：往返、继承合并、只读关后台、非法输入、endpoint_changed、摄入分区名。
#include "starling/governance/world_settings.hpp"

#include <gtest/gtest.h>

#include <cstdint>
#include <optional>
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

// 裁定 R25：consolidation 是第四个可选 endpoint，形状与另外三个相同。
TEST(WorldSettings, ConsolidationEndpointRoundTripsAndMerges) {
    WorldSettings src;
    src.consolidation = make_endpoint("k");
    const std::string text = settings_to_json(src);
    EXPECT_NE(text.find(R"("consolidation":)"), std::string::npos);
    EXPECT_EQ(settings_from_json(text), src);
    EXPECT_FALSE(settings_from_json("{}").consolidation.has_value());
    EXPECT_EQ(settings_to_json(WorldSettings{}).find("consolidation"), std::string::npos);
    const WorldSettings merged =
        merge_settings(src, R"({"consolidation":{"model":"judge-2"}})", false);
    ASSERT_TRUE(merged.consolidation.has_value());
    EXPECT_EQ(merged.consolidation->model, "judge-2");
    EXPECT_EQ(merged.consolidation->base_url, "http://k");   // 没给的叶子从 base 复制
    EXPECT_EQ(error_code([] { settings_from_json(R"({"consolidation":{"nope":1}})"); }),
              "invalid_settings");
}

// 裁定 R24：overrides 里 endpoint 键为 null 删掉该 endpoint，gist_thresholds 为 null 重置为 {}。
// 合并本来删不掉键：解绑角色、清空阈值之后同步，旧值会留在注册表里，新世界还会继承它。
TEST(WorldSettings, NullClearsEndpointsAndGistThresholds) {
    WorldSettings base;
    base.extraction = make_endpoint("x");
    base.embedding = make_endpoint("e");
    base.chat = make_endpoint("c");
    base.consolidation = make_endpoint("k");
    base.gist_thresholds = {{"min_holders", 4.0}};
    for (const char* key : {"extraction", "embedding", "chat", "consolidation"}) {
        const WorldSettings merged =
            merge_settings(base, std::string(R"({")") + key + R"(":null})", false);
        const auto& cleared = std::string(key) == "extraction"  ? merged.extraction
                              : std::string(key) == "embedding" ? merged.embedding
                              : std::string(key) == "chat"      ? merged.chat
                                                                : merged.consolidation;
        EXPECT_FALSE(cleared.has_value()) << key;
        EXPECT_EQ(merged.gist_thresholds, base.gist_thresholds) << key;   // 其余不动
    }
    const WorldSettings reset = merge_settings(base, R"({"gist_thresholds":null})", false);
    EXPECT_TRUE(reset.gist_thresholds.empty());
    EXPECT_TRUE(reset.extraction.has_value());
    // 同一份 overrides 里先清后设不是一回事：null 只删，不与 {} 混用。
    const WorldSettings partial =
        merge_settings(base, R"({"gist_thresholds":{"min_confidence":0.7}})", false);
    EXPECT_EQ(partial.gist_thresholds.size(), 2U);   // 对象照旧逐键覆盖
    // 删掉之后往返仍然一致；本来就没有的 endpoint 再删一次无害。
    const WorldSettings twice = merge_settings(merge_settings(base, R"({"chat":null})", false),
                                               R"({"chat":null})", false);
    EXPECT_FALSE(twice.chat.has_value());
    EXPECT_EQ(settings_from_json(settings_to_json(twice)), twice);
    // null 只对这五个键有意义；别处的 null 仍是类型错误。
    for (const char* bad : {R"({"vector_backend":null})", R"({"source_lane":null})",
                            R"({"background":null})", R"({"extraction":{"model":null}})",
                            R"({"gist_thresholds":{"min_holders":null}})"}) {
        EXPECT_EQ(error_code([&] { (void)merge_settings(base, bad, false); }), "invalid_settings")
            << bad;
    }
    // 存着的设置里出现 null（手改的 registry.db）：settings_from_json 照样按清除处理，结果没有该键。
    EXPECT_FALSE(settings_from_json(R"({"chat":null})").chat.has_value());
}

// 裁定 R27：同步权威来源（默认世界跟 config）要整段替换，不能逐叶合并：合并删不掉叶子，改绑到另一个
// provider 之后旧 provider 的 thinking_type / max_tokens / base_url 会留下来。
TEST(WorldSettings, ReplaceSectionsDropsOldLeavesOfAReboundEndpoint) {
    WorldSettings base;
    base.extraction = make_endpoint("a");   // max_retries 3、enable_thinking false、thinking_type disabled
    const WorldSettings out = replace_sections(
        base, R"({"extraction":{"provider":"openai","provider_name":"b","model":"mb"}})");
    ASSERT_TRUE(out.extraction.has_value());
    EndpointSettings want;
    want.provider = "openai";
    want.provider_name = "b";
    want.model = "mb";
    EXPECT_EQ(*out.extraction, want);       // 其余叶子全是默认值：base_url 空、可选字段未设置
    EXPECT_EQ(out.extraction->base_url, "");
    EXPECT_FALSE(out.extraction->max_retries.has_value());
}

TEST(WorldSettings, ReplaceSectionsReplacesGistAndLaneWholesale) {
    WorldSettings base;
    base.gist_thresholds = {{"min_holders", 4.0}, {"min_confidence", 0.7}};
    base.source_lane.k_source = 77;
    base.source_lane.model = "old";
    const WorldSettings out = replace_sections(
        base, R"({"gist_thresholds":{"min_holders":4},"source_lane":{"model":"new"}})");
    EXPECT_EQ(out.gist_thresholds, (std::map<std::string, double>{{"min_holders", 4.0}}));
    EXPECT_EQ(out.source_lane.model, "new");
    EXPECT_EQ(out.source_lane.k_source, SourceLaneSettings{}.k_source);   // 没给的回默认值
}

TEST(WorldSettings, ReplaceSectionsKeepsBackgroundLeafMergedAndAbsentSectionsUntouched) {
    WorldSettings base;
    base.chat = make_endpoint("c");
    base.consolidation = make_endpoint("k");
    base.background = {.tick_interval_s = 30.0,
                       .ingest = false,
                       .auto_snapshot = {.on_session = true, .interval_hours = 6}};
    const WorldSettings out = replace_sections(
        base, R"({"background":{"tick_interval_s":12},"extraction":null})");
    EXPECT_EQ(out.background.tick_interval_s, 12.0);
    EXPECT_FALSE(out.background.ingest);                                  // 逐叶合并（R16）
    EXPECT_EQ(out.background.auto_snapshot,
              (AutoSnapshotConfig{.on_session = true, .interval_hours = 6}));
    EXPECT_EQ(out.chat, base.chat);                                       // 没出现的段原样
    EXPECT_EQ(out.consolidation, base.consolidation);
    EXPECT_FALSE(out.extraction.has_value());                            // null 照样清除（R24）
    const WorldSettings cleared = replace_sections(base, R"({"chat":null,"gist_thresholds":null})");
    EXPECT_FALSE(cleared.chat.has_value());
    EXPECT_TRUE(cleared.gist_thresholds.empty());
}

TEST(WorldSettings, ReplaceSectionsValidatesLikeMerge) {
    const WorldSettings base;
    EXPECT_EQ(error_code([&] { (void)replace_sections(base, R"({"vector_backend":"zvec"})"); }),
              "snapshot_unsupported_backend");
    EXPECT_EQ(error_code([&] { (void)replace_sections(base, R"({"extraction":{"bogus":1}})"); }),
              "invalid_settings");
    EXPECT_EQ(error_code([&] { (void)replace_sections(base, "not json"); }), "invalid_settings");
    EXPECT_EQ(error_code([&] { (void)replace_sections(base, with_interval(9000)); }),
              "invalid_auto_snapshot");
}

TEST(WorldSettings, EndpointChanged) {
    EndpointSettings ep;
    ep.provider = "openai";
    ep.provider_name = "p";
    ep.base_url = "http://a";
    const ProviderEndpoint same{.provider = "openai", .base_url = "http://a"};
    EXPECT_NO_THROW(require_endpoint_unchanged(ep, same, "extraction"));
    EXPECT_EQ(error_code([&] {
                  require_endpoint_unchanged(
                      ep, ProviderEndpoint{.provider = "openai", .base_url = "http://b"}, "extraction");
              }),
              "endpoint_changed");
    ep.provider_name.clear();
    EXPECT_NO_THROW(require_endpoint_unchanged(
        ep, ProviderEndpoint{.provider = "openai", .base_url = "http://b"}, "extraction"));
}

// 裁定 R51：base_url 为空表示「这个种类的默认地址」，所以种类一变，空串对空串也是另一家的地址。
TEST(WorldSettings, EndpointChangedWhenOnlyTheKindChangesWithAnEmptyBaseUrl) {
    EndpointSettings ep;
    ep.provider = "openai";
    ep.provider_name = "p";
    const ProviderEndpoint anthropic{.provider = "anthropic", .base_url = ""};
    EXPECT_EQ(error_code([&] { require_endpoint_unchanged(ep, anthropic, "extraction"); }),
              "endpoint_changed");
    ep.provider = "anthropic";
    EXPECT_NO_THROW(require_endpoint_unchanged(ep, anthropic, "extraction"));
    const ProviderEndpoint openai{.provider = "openai", .base_url = ""};
    EXPECT_EQ(error_code([&] { require_endpoint_unchanged(ep, openai, "extraction"); }),
              "endpoint_changed");
}

// provider 已被删掉（nullopt，相当于 {"", ""}）一律拒绝，连世界里记的也是空种类、空地址时也拒绝。
TEST(WorldSettings, EndpointChangedWhenTheProviderWasDeleted) {
    EndpointSettings ep;
    ep.provider_name = "p";
    EXPECT_EQ(error_code([&] { require_endpoint_unchanged(ep, std::nullopt, "embedding"); }),
              "endpoint_changed");
    ep.provider = "openai";
    ep.base_url = "http://a";
    EXPECT_EQ(error_code([&] { require_endpoint_unchanged(ep, std::nullopt, "embedding"); }),
              "endpoint_changed");
}

// 裁定 R100 I1（spec §8.3）：显式端点（provider_name 为空）没有服务密钥 → endpoint_key_missing；有密钥，或继承 provider 的
// 端点（不论有没有密钥）都不抛。消息点名角色、不带地址。
TEST(WorldSettings, EndpointKeyMissingOnlyForAnExplicitEndpointWithoutTheServiceKey) {
    EndpointSettings explicit_ep;
    explicit_ep.provider = "openai";
    explicit_ep.base_url = "http://client.example/v1";
    EXPECT_EQ(error_code([&] { require_endpoint_key(explicit_ep, false, "embedding"); }),
              "endpoint_key_missing");
    EXPECT_NO_THROW(require_endpoint_key(explicit_ep, true, "embedding"));
    EndpointSettings inherited = explicit_ep;
    inherited.provider_name = "p";
    EXPECT_NO_THROW(require_endpoint_key(inherited, false, "extraction"));
    EXPECT_NO_THROW(require_endpoint_key(inherited, true, "extraction"));
    try {
        require_endpoint_key(explicit_ep, false, "extraction");
        ADD_FAILURE() << "no throw";
    } catch (const WorldStoreError& err) {
        const std::string text = err.what();
        EXPECT_EQ(text.find("example"), std::string::npos) << text;
        EXPECT_NE(text.find("extraction"), std::string::npos) << text;
    }
}

// 裁定 R58：向量只在嵌入的模型或维度变了时作废；只换地址、种类不重嵌入；解绑不重嵌入。
TEST(WorldSettings, EmbeddingNeedsReembedOnlyWhenModelOrDimChanges) {
    EndpointSettings before;
    before.provider = "openai";
    before.provider_name = "p";
    before.base_url = "http://a";
    before.model = "e1";
    before.dim = 1024;
    EndpointSettings moved = before;
    moved.base_url = "http://b";
    moved.provider = "anthropic";
    EXPECT_FALSE(embedding_needs_reembed(before, moved));
    EXPECT_FALSE(embedding_needs_reembed(before, before));
    EndpointSettings model = moved;
    model.model = "e2";
    EXPECT_TRUE(embedding_needs_reembed(before, model));
    EndpointSettings dim = before;
    dim.dim = 512;
    EXPECT_TRUE(embedding_needs_reembed(before, dim));
    EXPECT_FALSE(embedding_needs_reembed(before, std::nullopt));
    EXPECT_FALSE(embedding_needs_reembed(std::nullopt, std::nullopt));
    EXPECT_TRUE(embedding_needs_reembed(std::nullopt, before));
}

// 裁定 R54：消息只点名 provider 与角色，不带地址（spec §16）。
TEST(WorldSettings, EndpointChangedMessageNamesNoAddress) {
    EndpointSettings ep;
    ep.provider = "openai";
    ep.provider_name = "p";
    ep.base_url = "http://old.example";
    for (const std::optional<ProviderEndpoint>& current :
         {std::optional<ProviderEndpoint>(
              ProviderEndpoint{.provider = "openai", .base_url = "http://new.example"}),
          std::optional<ProviderEndpoint>()}) {
        try {
            require_endpoint_unchanged(ep, current, "embedding");
            ADD_FAILURE() << "no throw";
        } catch (const WorldStoreError& err) {
            const std::string text = err.what();
            EXPECT_EQ(text.find("example"), std::string::npos) << text;
            EXPECT_NE(text.find("embedding"), std::string::npos) << text;
            EXPECT_NE(text.find('p'), std::string::npos) << text;
        }
    }
}

TEST(WorldSettings, IngestPartition) {
    EXPECT_EQ(ingest_partition(true, "default", "x"), "tenant-default");
    EXPECT_EQ(ingest_partition(true, "a/b", "x"), "tenant-a%2Fb");
    EXPECT_EQ(ingest_partition(false, "default", "3f9a"), "world-3f9a");
    // 与 Python quote(safe='') 逐字节一致
    EXPECT_EQ(ingest_partition(true, "租户", "x"), "tenant-%E7%A7%9F%E6%88%B7");
    EXPECT_EQ(ingest_partition(true, "a b~._-\xC3\xA9/", "x"), "tenant-a%20b~._-%C3%A9%2F");
}

// 裁定 R78：open(settings=…) 的比较。两边先经 settings_from_json 补齐默认值再比，所以键序、缺省的键、显式给出的默认值
// 都不影响结果；比的是存着的设置（调用方传注册表里的 settings_json），不是生效的。
TEST(WorldSettings, SettingsEqualComparesAfterFillingDefaults) {
    WorldSettings stored;
    stored.extraction = make_endpoint("x");
    stored.gist_thresholds = {{"min_holders", 3.0}};
    const std::string stored_json = settings_to_json(stored);
    EXPECT_TRUE(settings_equal(stored_json, stored_json));
    EXPECT_TRUE(settings_equal("{}", "{}"));
    EXPECT_TRUE(settings_equal("{}", R"({"vector_backend": "sqlite", "background": {"ingest": true}})"));
    EXPECT_TRUE(settings_equal(R"({"source_lane": {"dim": 1024, "enabled": true}})",
                               R"({"source_lane": {"enabled": true, "dim": 1024}})"));
    EXPECT_FALSE(settings_equal("{}", R"({"background": {"ingest": false}})"));
    EXPECT_FALSE(settings_equal(stored_json, "{}"));
    WorldSettings other = stored;
    other.extraction->model = "model-y";
    EXPECT_FALSE(settings_equal(stored_json, settings_to_json(other)));
}

// 修复轮 1 F6：引用 provider 的 endpoint（provider_name 非空）的 base_url 是服务端从 provider 表补进来的，客户端看不到
// （info 去掉了地址），比较时不看它；model 等其他字段照比。客户端显式给的 endpoint（provider_name 为空）的 base_url 是客户端
// 自己写的，照比。
TEST(WorldSettings, SettingsEqualIgnoresTheInheritedBaseUrlOnly) {
    WorldSettings stored;
    stored.extraction = make_endpoint("x");           // provider_name "prov-x"、base_url "http://x"
    const std::string stored_json = settings_to_json(stored);
    WorldSettings no_url = stored;
    no_url.extraction->base_url.clear();
    EXPECT_TRUE(settings_equal(stored_json, settings_to_json(no_url)));
    WorldSettings other_url = stored;
    other_url.extraction->base_url = "http://elsewhere";
    EXPECT_TRUE(settings_equal(stored_json, settings_to_json(other_url)));
    WorldSettings other_model = no_url;
    other_model.extraction->model = "model-y";
    EXPECT_FALSE(settings_equal(stored_json, settings_to_json(other_model)));
    WorldSettings other_provider = no_url;
    other_provider.extraction->provider_name = "prov-y";
    EXPECT_FALSE(settings_equal(stored_json, settings_to_json(other_provider)));

    WorldSettings explicit_ep;
    explicit_ep.embedding = make_endpoint("e");
    explicit_ep.embedding->provider_name.clear();     // 客户端显式给的端点
    const std::string explicit_json = settings_to_json(explicit_ep);
    WorldSettings explicit_other = explicit_ep;
    explicit_other.embedding->base_url = "http://elsewhere";
    EXPECT_FALSE(settings_equal(explicit_json, settings_to_json(explicit_other)));
}

TEST(WorldSettings, SettingsEqualRejectsInvalidGivenSettings) {
    EXPECT_EQ(error_code([] { (void)settings_equal("{}", R"({"api_key": "k"})"); }),
              "invalid_settings");
    EXPECT_EQ(error_code([] { (void)settings_equal("{}", "not json"); }), "invalid_settings");
}

}  // namespace
