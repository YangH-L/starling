// test_converse_phases.cpp — converse 三相拆分(2026-07-05 锁外生成)。
// 钉住:prepare+generate+commit ≡ 单体 converse(parity)、generate 失败 →
// commit 干净无回复、写门中途关闭 → reply 保留 + remember_ok=false、
// commit 以 prepared 携带的 created_at 为权威(即使 params 两次传入漂移)。零网络。

#include "starling/memory/memory_ops.hpp"

#include "starling/embedding/embedding_adapter.hpp"
#include "starling/extractor/fake_llm_adapter.hpp"
#include "starling/governance/write_gate.hpp"
#include "starling/persistence/migration_runner.hpp"
#include "starling/persistence/sqlite_adapter.hpp"
#include "starling/retrieval/semantic_retriever.hpp"
#include "starling/runtime_health.hpp"
#include "starling/retrieval/source_retriever.hpp"
#include "starling/vector/source_vector_index.hpp"
#include "starling/vector/vector_index.hpp"

#include <nlohmann/json.hpp>

#include <gtest/gtest.h>

#include <memory>
#include <string>

namespace starling::memoryops {

namespace {

constexpr const char* kCannedJson =
    R"JSON([{"holder":"self","holder_perspective":"FIRST_PERSON","subject":"Bob","predicate":"responsible_for","object":"auth","modality":"BELIEVES","polarity":"POS","nesting_depth":0}])JSON";

std::unique_ptr<persistence::SqliteAdapter> make_adapter() {
    auto a = persistence::SqliteAdapter::open(":memory:");
    persistence::MigrationRunner(a->connection().raw()).migrate_to_latest();
    return a;
}

int row_count(persistence::Connection& conn, const char* sql) {
    sqlite3_stmt* raw = nullptr;
    EXPECT_EQ(sqlite3_prepare_v2(conn.raw(), sql, -1, &raw, nullptr), SQLITE_OK);
    persistence::StmtHandle h(raw);
    EXPECT_EQ(sqlite3_step(h.get()), SQLITE_ROW);
    return sqlite3_column_int(h.get(), 0);
}

ConverseParams conv_params(std::string_view message) {
    ConverseParams cp;
    cp.tenant_id          = "default";
    cp.holder_id          = "cog-self";
    cp.interlocutor       = "alice";
    cp.adapter_name       = "facade";
    cp.source_prefix      = "conv-";
    cp.created_at_iso8601 = "2026-07-05T10:00:00Z";
    cp.message            = std::string(message);
    cp.recall_k           = 6;
    return cp;
}

struct Fixture {   // 每条路径一套独立 :memory: 库(parity 需要隔离副作用)
    std::unique_ptr<persistence::SqliteAdapter> adapter = make_adapter();
    embedding::StubEmbeddingAdapter emb{8};
    vector::SqliteBlobVectorIndex idx;
    retrieval::SemanticRetriever semantic{*adapter, emb, idx};
    extractor::FakeLLMAdapter chat;
    extractor::FakeLLMAdapter extraction;
    Fixture() {
        chat.set_default_response(extractor::LLMResponse{.raw_xml = "Reply text.", .ok = true});
        extraction.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});
    }
};

void expect_parity(const ConverseOutcome& mono, const ConverseOutcome& phased) {
    EXPECT_EQ(mono.ok, phased.ok);
    EXPECT_EQ(mono.reply, phased.reply);
    EXPECT_EQ(mono.error, phased.error);
    EXPECT_EQ(mono.context_pack, phased.context_pack);
    EXPECT_EQ(mono.abstained, phased.abstained);
    EXPECT_EQ(mono.statement_ids.size(), phased.statement_ids.size());  // id 含随机成分,比数量
    EXPECT_EQ(mono.remember_ok, phased.remember_ok);
    EXPECT_EQ(mono.remember_error, phased.remember_error);
    EXPECT_EQ(mono.gen_total_tokens, phased.gen_total_tokens);
}

}  // namespace

TEST(ConversePhases, PhasedEqualsMonolithNonStreaming) {
    Fixture mono_fx;
    const auto mono = converse(*mono_fx.adapter, mono_fx.chat, mono_fx.extraction,
                               mono_fx.semantic, "", conv_params("hello"), {}, {});
    Fixture phased_fx;
    const auto prepared = converse_prepare(*phased_fx.adapter, phased_fx.semantic,
                                           conv_params("hello"));
    const auto gen_resp = phased_fx.chat.generate_stream(prepared.prompt, {});
    const auto phased = converse_commit(*phased_fx.adapter, phased_fx.extraction, "",
                                        conv_params("hello"), prepared, gen_resp);
    expect_parity(mono, phased);
    EXPECT_TRUE(phased.ok);
    EXPECT_TRUE(phased.remember_ok);
}

TEST(ConversePhases, PhasedEqualsMonolithStreaming) {
    // 流式:两条路径都挂 sink,delta 拼接 == 最终 reply,且 parity 保持。
    Fixture mono_fx;
    std::string mono_streamed;
    const auto mono = converse(*mono_fx.adapter, mono_fx.chat, mono_fx.extraction,
                               mono_fx.semantic, "", conv_params("hi"), {},
                               [&mono_streamed](std::string_view d) { mono_streamed += d; });
    Fixture phased_fx;
    const auto prepared = converse_prepare(*phased_fx.adapter, phased_fx.semantic,
                                           conv_params("hi"));
    std::string phased_streamed;
    const auto gen_resp = phased_fx.chat.generate_stream(
        prepared.prompt, [&phased_streamed](std::string_view d) { phased_streamed += d; });
    const auto phased = converse_commit(*phased_fx.adapter, phased_fx.extraction, "",
                                        conv_params("hi"), prepared, gen_resp);
    expect_parity(mono, phased);
    EXPECT_EQ(mono_streamed, mono.reply);
    EXPECT_EQ(phased_streamed, phased.reply);
}

TEST(ConversePhases, CommitOnGenerateFailureIsCleanNoReply) {
    Fixture fx;
    const auto prepared = converse_prepare(*fx.adapter, fx.semantic, conv_params("hello"));
    const int before = row_count(fx.adapter->connection(),
                                 "SELECT COUNT(*) FROM statements");
    extractor::LLMResponse failed;
    failed.ok = false;
    failed.error = "transport_error:SSL connect error";
    const auto out = converse_commit(*fx.adapter, fx.extraction, "",
                                     conv_params("hello"), prepared, failed);
    EXPECT_FALSE(out.ok);
    EXPECT_EQ(out.error, "transport_error:SSL connect error");
    EXPECT_TRUE(out.reply.empty());
    EXPECT_TRUE(out.statement_ids.empty());
    EXPECT_FALSE(out.remember_ok);
    EXPECT_EQ(out.context_pack, prepared.context_pack);   // 轨迹仍可回显
    EXPECT_EQ(row_count(fx.adapter->connection(),
                        "SELECT COUNT(*) FROM statements"), before);  // 零沉淀
}

TEST(ConversePhases, PrepareFailsFastWhenGateClosed) {
    Fixture fx;
    fx.adapter->set_write_admit([] { return false; });
    EXPECT_THROW(converse_prepare(*fx.adapter, fx.semantic, conv_params("x")),
                 governance::WriteGateRejected);
}

TEST(ConversePhases, CommitUsesPreparedCreatedAtWhenParamsDrift) {
    // 同刻不变式的类型强制:prepare 快照 created_at=T1;commit 调用时 params 却
    // 漂移到 T2(模拟未来直呼者对两段各传 now=None 产生的两个 wall-clock)。
    // 落库权威必须是 T1(prepared 携带的时刻),而非 commit 这次传入的 T2——
    // 否则幂等键的时间成分与召回种子(已在 prepare 用 T1 派生)会分叉。
    Fixture fx;
    ConverseParams params = conv_params("hello");
    params.created_at_iso8601 = "2026-07-05T10:00:00Z";   // T1
    const auto prepared = converse_prepare(*fx.adapter, fx.semantic, params);
    ASSERT_EQ(prepared.created_at_iso8601, "2026-07-05T10:00:00Z");

    const auto gen_resp = fx.chat.generate_stream(prepared.prompt, {});

    ConverseParams drifted = params;
    drifted.created_at_iso8601 = "2026-07-05T10:05:00Z";  // T2 ≠ T1 —— 调用方漂移

    const auto out1 = converse_commit(*fx.adapter, fx.extraction, "", drifted,
                                      prepared, gen_resp);
    ASSERT_TRUE(out1.ok);
    ASSERT_TRUE(out1.remember_ok);
    EXPECT_EQ(out1.statement_ids.size(), 1u);

    // 落库 created_at 以 prepared(T1)为权威,commit 调用时漂移的 T2 未生效。
    EXPECT_EQ(row_count(fx.adapter->connection(),
                        "SELECT COUNT(*) FROM engrams WHERE created_at = "
                        "'2026-07-05T10:00:00Z'"),
              1);
    EXPECT_EQ(row_count(fx.adapter->connection(),
                        "SELECT COUNT(*) FROM engrams WHERE created_at = "
                        "'2026-07-05T10:05:00Z'"),
              0);

    // 佐证:同一 prepared + gen_resp 再 commit 一次(params 仍是漂移的 T2),
    // 内容确定性幂等键命中——第二次是 idempotent 而非 accepted,statements/engrams
    // 行数都没有翻倍(gen_resp 是纯值,可安全重用)。
    const auto out2 = converse_commit(*fx.adapter, fx.extraction, "", drifted,
                                      prepared, gen_resp);
    EXPECT_TRUE(out2.remember_ok);
    EXPECT_TRUE(out2.statement_ids.empty());
    EXPECT_EQ(row_count(fx.adapter->connection(), "SELECT COUNT(*) FROM engrams"), 1);
    EXPECT_EQ(row_count(fx.adapter->connection(), "SELECT COUNT(*) FROM statements"), 1);
}

TEST(ConversePhases, GateClosingMidTurnKeepsReplyAndFlagsRememberError) {
    // 模拟「锁外生成期间翻 DRAINING」:prepare 时门开,commit 前关门。
    Fixture fx;
    const auto prepared = converse_prepare(*fx.adapter, fx.semantic, conv_params("hello"));
    const auto gen_resp = fx.chat.generate_stream(prepared.prompt, {});
    fx.adapter->set_write_admit([] { return false; });
    const auto out = converse_commit(*fx.adapter, fx.extraction, "",
                                     conv_params("hello"), prepared, gen_resp);
    EXPECT_TRUE(out.ok);                       // 失败语义 A:回复绝不丢
    EXPECT_EQ(out.reply, "Reply text.");
    EXPECT_FALSE(out.remember_ok);
    EXPECT_FALSE(out.remember_error.empty());  // WriteGateRejected e.what() 可观测
}

// 形态 S Task 11：生产对话路径也能开左脑 lane。converse_prepare 自己建
// PlannerQuery，所以 source_lane 必须经 ConverseParams 透传，否则评测与生产
// 用的不是同一份提示。
namespace {
struct SourceFixture {
    std::unique_ptr<persistence::SqliteAdapter> adapter = make_adapter();
    embedding::StubEmbeddingAdapter emb{8};
    vector::SqliteBlobVectorIndex idx;
    vector::SqliteSourceVectorIndex source_idx;
    retrieval::SemanticRetriever semantic{*adapter, emb, idx};
    embedding::EmbeddingWorker worker{*adapter, emb, idx, {}, &source_idx};

    void retain(const std::string& text) {
        const nlohmann::json turns = nlohmann::json::array({{
            {"speaker", "alice"}, {"text", text}, {"session_id", "s1"},
            {"turn_index", 0}, {"audience", {"alice", "cog-self"}},
            {"provenance", "source_time"}, {"turn_id", nullptr},
            {"observed_at", "2026-07-01T09:00:00Z"}}});
        retrieval::retain_source_turns(*adapter, "default", {"alice", "cog-self"},
                                       turns.dump(), "2026-07-01T09:00:00Z");
        worker.tick_one_batch(adapter->connection(), "2026-07-02T00:00:00Z");
    }
    retrieval::SourceLaneDeps deps() {
        retrieval::SourceLaneDeps out;
        out.index = &source_idx;
        out.embedder = &emb;
        out.dim = emb.dim();
        out.model = emb.model();
        out.tau_source = 0.5;
        return out;
    }
};
}  // namespace

TEST(ConversePhases, SourceLaneReachesTheProductionPrompt) {
    SourceFixture fx;
    const std::string secret = "the vault code is 4321";
    fx.retain(secret);
    auto params = conv_params(secret);
    params.source_lane = fx.deps();
    const auto prepared = converse_prepare(*fx.adapter, fx.semantic, params);
    EXPECT_NE(prepared.context_pack.find("[SOURCE]"), std::string::npos) << prepared.context_pack;
    // 原文进的是生产提示本身（围栏内），不只是 context_pack。
    EXPECT_NE(prepared.prompt.find(secret), std::string::npos);
    EXPECT_FALSE(prepared.abstained);
}

// 阶段 B 的失败机制：前提部分为真的题，原文召回了相关内容（相似度很高），
// 模型据此作答而不是指出缺失。修法不是调阈值（相似度区分不了"相关但无答案"），
// 而是在提示里把 [SOURCE] 的语义讲清：它是原话，不是答案；只有原话直接陈述了
// 答案才可据其作答，否则应说明缺少什么。
TEST(ConversePhases, SourceLaneAddsRawUtteranceGuidance) {
    SourceFixture fx;
    fx.retain("the vault code is 4321");
    auto with_lane = conv_params("the vault code is 4321");
    with_lane.source_lane = fx.deps();
    const auto prepared = converse_prepare(*fx.adapter, fx.semantic, with_lane);
    ASSERT_NE(prepared.context_pack.find("[SOURCE]"), std::string::npos);
    // 只在真有原文行时才加这段指引。
    EXPECT_NE(prepared.prompt.find("raw utterance"), std::string::npos) << prepared.prompt;
    EXPECT_NE(prepared.prompt.find("directly states"), std::string::npos) << prepared.prompt;
}

TEST(ConversePhases, NoSourceLinesMeansNoRawUtteranceGuidance) {
    SourceFixture fx;   // 无任何原文：即使传了 deps，也不该出现 SOURCE 指引
    auto params = conv_params("hello");
    params.source_lane = fx.deps();
    const auto prepared = converse_prepare(*fx.adapter, fx.semantic, params);
    EXPECT_EQ(prepared.context_pack.find("[SOURCE]"), std::string::npos);
    EXPECT_EQ(prepared.prompt.find("raw utterance"), std::string::npos) << prepared.prompt;
}

TEST(ConversePhases, NoSourceLaneLeavesPromptUnchanged) {
    SourceFixture fx;
    fx.retain("the vault code is 4321");
    const auto prepared = converse_prepare(*fx.adapter, fx.semantic,
                                           conv_params("the vault code is 4321"));
    EXPECT_EQ(prepared.context_pack.find("[SOURCE]"), std::string::npos);
    EXPECT_FALSE(prepared.grounded);
    // 无原文行：提示收尾与改动前逐字节一致。
    EXPECT_TRUE(prepared.prompt.ends_with("\nUser: the vault code is 4321\nAssistant:"))
        << prepared.prompt;
}


namespace {
void expect_prepared_equal(const ConversePrepared& a, const ConversePrepared& b) {
    EXPECT_EQ(a.context_pack, b.context_pack);
    EXPECT_EQ(a.prompt, b.prompt);
    EXPECT_EQ(a.abstained, b.abstained);
    EXPECT_EQ(a.grounded, b.grounded);
    EXPECT_EQ(a.created_at_iso8601, b.created_at_iso8601);
}
}

TEST(SummarySourceOverride, ExpandsByteBudgetOnlyForSummary) {
    SourceFixture fx;
    fx.retain("summarize the vault code is 4321");
    auto p = conv_params("summarize the vault code is 4321");
    p.source_lane = fx.deps();
    p.source_lane->chunk_byte_budget = 1;
    const auto base = converse_prepare(*fx.adapter, fx.semantic, p);
    EXPECT_EQ(base.context_pack.find("[SOURCE]"), std::string::npos);
    p.summary_source_override = SummarySourceOverride{100, 128000};
    const auto expanded = converse_prepare(*fx.adapter, fx.semantic, p);
    EXPECT_NE(expanded.context_pack.find("[SOURCE]"), std::string::npos);
    EXPECT_FALSE(expanded.grounded);
    EXPECT_EQ(expanded.prompt.find("<evidence> block"), std::string::npos);
    auto direct = p;
    direct.summary_source_override.reset();
    direct.source_lane->k_source = 100;
    direct.source_lane->chunk_byte_budget = 128000;
    expect_prepared_equal(expanded, converse_prepare(*fx.adapter, fx.semantic, direct));
    EXPECT_EQ(p.source_lane->chunk_byte_budget, 1); // caller deps are unchanged
    EXPECT_EQ(p.source_lane->k_source, 30);
}

TEST(SummarySourceOverride, ExpandsCandidateCount) {
    SourceFixture fx;
    fx.retain("summarize the vault code is 4321");
    fx.retain("the backup vault code is 5678");
    fx.retain("the spare vault code is 9012");
    auto p = conv_params("summarize the vault code is 4321");
    p.source_lane = fx.deps();
    p.source_lane->k_source = 1;
    const auto base = converse_prepare(*fx.adapter, fx.semantic, p);
    p.summary_source_override = SummarySourceOverride{3, 32000};
    const auto expanded = converse_prepare(*fx.adapter, fx.semantic, p);
    EXPECT_GT(expanded.context_pack.size(), base.context_pack.size());
    auto direct = p;
    direct.summary_source_override.reset();
    direct.source_lane->k_source = 3;
    expect_prepared_equal(expanded, converse_prepare(*fx.adapter, fx.semantic, direct));
}

TEST(SummarySourceOverride, NonSummaryIsByteIdentical) {
    SourceFixture fx;
    fx.retain("the vault code is 4321");
    auto p = conv_params("the vault code is 4321");
    p.source_lane = fx.deps();
    const auto base = converse_prepare(*fx.adapter, fx.semantic, p);
    ASSERT_NE(base.context_pack.find("[SOURCE]"), std::string::npos);
    ASSERT_TRUE(base.grounded);
    p.summary_source_override = SummarySourceOverride{100, 128000};
    expect_prepared_equal(base, converse_prepare(*fx.adapter, fx.semantic, p));
}

TEST(SummarySourceOverride, DisabledLaneAndEmptyOverrideAreByteIdentical) {
    SourceFixture fx;
    fx.retain("summarize the vault code is 4321");
    auto p = conv_params("summarize the vault code is 4321");
    EXPECT_FALSE(p.summary_source_override.has_value());
    const auto base = converse_prepare(*fx.adapter, fx.semantic, p);
    p.summary_source_override = SummarySourceOverride{100, 128000};
    expect_prepared_equal(base, converse_prepare(*fx.adapter, fx.semantic, p));
    p.source_lane = fx.deps();
    p.summary_source_override.reset();
    const auto lane_base = converse_prepare(*fx.adapter, fx.semantic, p);
    p.summary_source_override = SummarySourceOverride{30, 32000};
    expect_prepared_equal(lane_base, converse_prepare(*fx.adapter, fx.semantic, p));
}

TEST(SummarySourceOverride, RejectsInvalidValuesBeforeRecall) {
    SourceFixture fx;
    for (const auto& message : {"Summarize the vault", "What is the code?"}) {
        auto p = conv_params(message);
        for (const auto& bad : std::vector<SummarySourceOverride>{{0, 128000}, {-1, 128000},
                               {100, 0}, {100, -1}, {29, 128000}, {100, 31999}}) {
            p.source_lane = fx.deps();
            p.summary_source_override = bad;
            EXPECT_THROW(converse_prepare(*fx.adapter, fx.semantic, p), std::invalid_argument);
        }
        p.source_lane.reset();
        p.summary_source_override = SummarySourceOverride{0, 128000};
        EXPECT_THROW(converse_prepare(*fx.adapter, fx.semantic, p), std::invalid_argument);
    }
}

TEST(SummarySourceOverride, RejectsKSourceAboveKMax) {
    SourceFixture fx;
    for (const auto* message : {"Summarize the vault", "What is the code?"}) {
        auto p = conv_params(message);
        p.source_lane = fx.deps();
        p.summary_source_override = SummarySourceOverride{201, 128000};
        EXPECT_THROW(converse_prepare(*fx.adapter, fx.semantic, p), std::invalid_argument);
    }
}

TEST(SummarySourceOverride, ZeroKSourceCannotShrinkRecallK) {
    SourceFixture fx;
    for (const auto* message : {"Summarize the vault", "What is the code?"}) {
        auto p = conv_params(message);
        p.recall_k = 10;
        p.source_lane = fx.deps();
        p.source_lane->k_source = 0;
        p.summary_source_override = SummarySourceOverride{9, 128000};
        EXPECT_THROW(converse_prepare(*fx.adapter, fx.semantic, p), std::invalid_argument);
        p.summary_source_override = SummarySourceOverride{10, 128000};
        EXPECT_NO_THROW(converse_prepare(*fx.adapter, fx.semantic, p));
        EXPECT_EQ(p.source_lane->k_source, 0);
    }
}

TEST(SummarySourceOverride, ZeroKSourceUsesRecallKInsteadOfDefaultThirty) {
    SourceFixture fx;
    auto p = conv_params("Summarize the vault");
    p.recall_k = 6;
    p.source_lane = fx.deps();
    p.source_lane->k_source = 0;
    p.summary_source_override = SummarySourceOverride{6, 128000};
    EXPECT_NO_THROW(converse_prepare(*fx.adapter, fx.semantic, p));
}

// ── 先引用、后作答（弃答修复 v2）──────────────────────────────────────────────

TEST(GroundedAnswer, SourceLinesTurnOnEvidenceInstruction) {
    SourceFixture fx;
    fx.retain("the vault code is 4321");
    auto params = conv_params("the vault code is 4321");
    params.source_lane = fx.deps();
    const auto prepared = converse_prepare(*fx.adapter, fx.semantic, params);
    ASSERT_NE(prepared.context_pack.find("[SOURCE]"), std::string::npos);
    EXPECT_TRUE(prepared.grounded);
    // 指令在 User 轮之后（本轮输出格式），提示仍以 Assistant: 收尾。
    const auto user_at = prepared.prompt.find("\nUser: ");
    const auto instr_at = prepared.prompt.find("<evidence> block");
    ASSERT_NE(user_at, std::string::npos);
    ASSERT_NE(instr_at, std::string::npos);
    EXPECT_LT(user_at, instr_at);
    EXPECT_TRUE(prepared.prompt.ends_with("\nAssistant:"));
}

TEST(GroundedAnswer, NoSourceLinesNoEvidenceInstruction) {
    SourceFixture fx;   // 有 deps 但无原文
    auto params = conv_params("hello");
    params.source_lane = fx.deps();
    const auto prepared = converse_prepare(*fx.adapter, fx.semantic, params);
    EXPECT_FALSE(prepared.grounded);
    EXPECT_EQ(prepared.prompt.find("<evidence>"), std::string::npos);
}

TEST(GroundedAnswer, InstructionDoesNotEchoBenchmarkRefusalPhrasing) {
    // 防措辞拟合：指令不得教模型说评测 rubric 的固定拒答句式。
    const std::string instr(kGroundedAnswerInstruction);
    EXPECT_EQ(instr.find("no information related"), std::string::npos);
    EXPECT_EQ(instr.find("Based on the provided chat"), std::string::npos);
}

TEST(GroundedAnswer, StripRemovesLeadingBlockOnly) {
    EXPECT_EQ(strip_evidence_block("<evidence>\nX: none\n</evidence>\n\nNot recorded."),
              "Not recorded.");
    EXPECT_EQ(strip_evidence_block("  \n<evidence>a</evidence>reply"), "reply");
    // 正文里提到标签不受影响：只认最前面的块。
    EXPECT_EQ(strip_evidence_block("<evidence>a</evidence>use <evidence> tags"),
              "use <evidence> tags");
}

TEST(GroundedAnswer, StripKeepsReplyWhenNoBlock) {
    // 没写块：模型没遵从格式，但回复本身可用，原样返回。
    EXPECT_EQ(strip_evidence_block("Plain reply."), "Plain reply.");
    EXPECT_EQ(strip_evidence_block(""), "");
}

TEST(GroundedAnswer, StripReportsTruncatedBlockAsNoReply) {
    // 块没闭合 = 截断在摘录阶段：没有回复，绝不把半截摘录当回复返回。
    EXPECT_FALSE(strip_evidence_block("<evidence>\nthing: X\nlines: the user said").has_value());
    EXPECT_FALSE(strip_evidence_block("  <evidence>").has_value());
}

namespace {
std::string run_filter(const std::vector<std::string>& deltas) {
    std::string out;
    EvidenceStreamFilter filter([&out](std::string_view delta) { out.append(delta); });
    for (const auto& delta : deltas) {
        filter(delta);
    }
    filter.finish();
    return out;
}
}  // namespace

TEST(GroundedAnswer, StreamFilterHidesBlockAcrossTokenSplits) {
    const std::string full = "<evidence>\nthing: X\nlines: none\n</evidence>\n\nNot recorded.";
    // 逐字符切：开标签、闭标签、块后空白都跨 delta。
    std::vector<std::string> chars;
    for (const char ch : full) {
        chars.emplace_back(1, ch);
    }
    EXPECT_EQ(run_filter(chars), "Not recorded.");
    EXPECT_EQ(run_filter({full}), "Not recorded.");
    EXPECT_EQ(run_filter({"<evid", "ence>x</evi", "dence>", "\n", "\n", "Hi"}), "Hi");
    // 流式结果与非流式剥块一致。
    EXPECT_EQ(run_filter(chars), strip_evidence_block(full));
}

TEST(GroundedAnswer, StreamFilterPassesThroughWhenNoBlock) {
    EXPECT_EQ(run_filter({"Hel", "lo there"}), "Hello there");
    EXPECT_EQ(run_filter({"<", "b>bold</b>"}), "<b>bold</b>");   // 像标签但不是
}

TEST(GroundedAnswer, StreamFilterDropsTruncatedBlock) {
    // 截断在摘录阶段：流式也不泄漏半截摘录。
    EXPECT_EQ(run_filter({"<evid", "ence>\nthing: X\nlines: the user"}), "");
    // 开头比开标签还短、生成就结束了：不是证据块，原样放行。
    EXPECT_EQ(run_filter({"<ev"}), "<ev");
}

TEST(GroundedAnswer, CommitStripsBlockFromReplyAndMemory) {
    Fixture fx;
    ConversePrepared prepared;
    prepared.created_at_iso8601 = "2026-06-01T10:00:00Z";
    prepared.grounded = true;
    const extractor::LLMResponse gen{
        .raw_xml = "<evidence>\nthing: secret plan\nlines: none\n</evidence>\n\nNot recorded.",
        .ok = true};
    const auto outcome = converse_commit(*fx.adapter, fx.extraction, "extract",
                                         conv_params("what is the plan?"), prepared, gen);
    ASSERT_TRUE(outcome.ok);
    EXPECT_EQ(outcome.reply, "Not recorded.");
    // 沉淀进记忆的对话（engram 原文）里也不能有证据块，否则下一轮会把自己的
    // 摘抄当成助手说过的话再抽一遍。
    EXPECT_EQ(row_count(fx.adapter->connection(), "SELECT COUNT(*) FROM engrams"), 1);
    EXPECT_EQ(row_count(fx.adapter->connection(),
                        "SELECT COUNT(*) FROM engrams WHERE payload_inline LIKE '%<evidence>%'"),
              0);
    EXPECT_EQ(row_count(fx.adapter->connection(),
                        "SELECT COUNT(*) FROM engrams WHERE payload_inline LIKE '%Not recorded.%'"),
              1);
}

TEST(GroundedAnswer, CommitTreatsTruncatedBlockAsFailedTurn) {
    Fixture fx;
    ConversePrepared prepared;
    prepared.created_at_iso8601 = "2026-06-01T10:00:00Z";
    prepared.grounded = true;
    const extractor::LLMResponse gen{
        .raw_xml = "<evidence>\nthing: plan\nlines: the user said they would",
        .ok = true, .completion_tokens = 4096};
    const auto outcome = converse_commit(*fx.adapter, fx.extraction, "extract",
                                         conv_params("what is the plan?"), prepared, gen);
    EXPECT_FALSE(outcome.ok);
    EXPECT_EQ(outcome.error, "evidence_truncated");
    EXPECT_TRUE(outcome.reply.empty());
    EXPECT_EQ(outcome.gen_completion_tokens, 4096);   // 成本照记
    // 与 generate 失败同语义：什么都不沉淀。
    EXPECT_EQ(row_count(fx.adapter->connection(), "SELECT COUNT(*) FROM engrams"), 0);
}

TEST(GroundedAnswer, CommitLeavesUngroundedReplyUntouched) {
    Fixture fx;
    ConversePrepared prepared;
    prepared.created_at_iso8601 = "2026-06-01T10:00:00Z";
    const extractor::LLMResponse gen{.raw_xml = "<evidence>kept</evidence>as-is", .ok = true};
    const auto outcome = converse_commit(*fx.adapter, fx.extraction, "extract",
                                         conv_params("q"), prepared, gen);
    EXPECT_EQ(outcome.reply, "<evidence>kept</evidence>as-is");
}


// ── 总结类请求豁免先引用后作答 ────────────────────────────────────────────────

TEST(SummaryExempt, DetectsSummaryRequests) {
    EXPECT_TRUE(is_summary_request("Can you summarize my progress on the thesis?"));
    EXPECT_TRUE(is_summary_request("Give me a RECAP of what we discussed"));
    EXPECT_TRUE(is_summary_request("What's the big picture of my budget plans?"));
    EXPECT_TRUE(is_summary_request("sum up the trip planning so far"));
}

TEST(SummaryExempt, IgnoresOrdinaryQuestions) {
    EXPECT_FALSE(is_summary_request("When is my dentist appointment?"));
    EXPECT_FALSE(is_summary_request("Which option did I pick for the venue?"));
    EXPECT_FALSE(is_summary_request(""));
}

TEST(SummaryExempt, DetectsChineseSummaryRequests) {
    EXPECT_TRUE(is_summary_request("帮我总结一下这周的进展"));
    EXPECT_TRUE(is_summary_request("能概括下我们讨论的方案吗"));
    EXPECT_TRUE(is_summary_request("把预算相关的内容汇总给我"));
    EXPECT_TRUE(is_summary_request("回顾一下我们上次聊了什么"));
    EXPECT_TRUE(is_summary_request("幫我總結一下"));               // 繁体
}

TEST(SummaryExempt, IgnoresOrdinaryChineseQuestions) {
    EXPECT_FALSE(is_summary_request("我的牙医预约是哪天？"));
    EXPECT_FALSE(is_summary_request("这次回顾性研究用的是哪个数据集？"));   // 「回顾」作名词修饰
    EXPECT_FALSE(is_summary_request("我选的是哪个场地？"));
}

TEST(SummaryExempt, CaseFoldingLeavesNonAsciiBytesIntact) {
    // 西里尔字母在 UTF-8 locale 下会被 std::tolower 改写；ASCII 折叠必须保证
    // 非 ASCII 输入照常判定（这里没有关键词，应为 false，且不崩溃）。
    EXPECT_FALSE(is_summary_request("Сводка ÉTÉ"));
    EXPECT_TRUE(is_summary_request("ÉTÉ SUMMARY"));
}

TEST(SummaryExempt, SummaryRequestWithSourcesIsNotGrounded) {
    SourceFixture fx;
    fx.retain("summarize the vault code is 4321");
    auto params = conv_params("summarize the vault code is 4321");
    params.source_lane = fx.deps();
    const auto prepared = converse_prepare(*fx.adapter, fx.semantic, params);
    ASSERT_NE(prepared.context_pack.find("[SOURCE]"), std::string::npos);
    EXPECT_FALSE(prepared.grounded);
    EXPECT_EQ(prepared.prompt.find("<evidence>"), std::string::npos);
    // 豁免后提示与先引用后作答之前一致：原文指引仍在，结尾是普通的 Assistant:。
    EXPECT_NE(prepared.prompt.find("raw utterance"), std::string::npos);
    EXPECT_TRUE(prepared.prompt.ends_with("\nUser: summarize the vault code is 4321\nAssistant:"));
}

TEST(SummaryExempt, OrdinaryQuestionWithSourcesStaysGrounded) {
    SourceFixture fx;
    fx.retain("the vault code is 4321");
    auto params = conv_params("the vault code is 4321");
    params.source_lane = fx.deps();
    const auto prepared = converse_prepare(*fx.adapter, fx.semantic, params);
    ASSERT_NE(prepared.context_pack.find("[SOURCE]"), std::string::npos);
    EXPECT_TRUE(prepared.grounded);
}

}  // namespace starling::memoryops
