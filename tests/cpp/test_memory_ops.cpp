// test_memory_ops.cpp — remember/tick_all 管线编排(2026-06-11 自 Python 归位)。
// 钉住编排语义本身:幂等键内容确定性、先 engram 后抽取、仅 accepted/idempotent
// 才抽取、tick 三连的返回形状。LLM 用 FakeLLMAdapter,零网络。

#include "starling/memory/memory_ops.hpp"

#include "starling/embedding/embedding_adapter.hpp"
#include "starling/extractor/fake_llm_adapter.hpp"
#include "starling/governance/write_gate.hpp"
#include "starling/persistence/migration_runner.hpp"
#include "starling/persistence/sqlite_adapter.hpp"
#include "starling/persistence/stmt.hpp"
#include "starling/runtime_health.hpp"
#include "starling/vector/vector_index.hpp"

#include <gtest/gtest.h>
#include <sqlite3.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <map>
#include <memory>
#include <stdexcept>
#include <string>
#include <string_view>
#include <system_error>
#include <vector>

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

RememberParams params(std::string_view text) {
    RememberParams p;
    p.tenant_id          = "default";
    p.holder_id          = "cog-self";
    p.adapter_name       = "facade";
    p.source_prefix      = "mem-";
    p.created_at_iso8601 = "2026-06-11T10:00:00Z";
    p.payload.assign(text.begin(), text.end());
    return p;
}

class HolderSelectiveLLMAdapter final : public extractor::LLMAdapter {
public:
    extractor::LLMResponse extract(std::string_view prompt,
                                   std::string_view) override {
        if (prompt.find("Bad") != std::string_view::npos) {
            return {.raw_xml = "", .ok = false, .error = "forced holder failure"};
        }
        return {.raw_xml = "[]", .ok = true};
    }
};

}  // namespace

TEST(MemoryOps, RememberThenIdempotentRerun) {
    auto a = make_adapter();
    extractor::FakeLLMAdapter llm;
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});

    const auto r1 = remember(*a, llm, /*prompt_template=*/"", params("Bob owns auth"));
    EXPECT_EQ(r1.outcome, "accepted");
    EXPECT_FALSE(r1.engram_ref.empty());
    EXPECT_EQ(r1.statement_ids.size(), 1u);
    EXPECT_EQ(row_count(a->connection(), "SELECT COUNT(*) FROM engrams"), 1);
    EXPECT_EQ(row_count(a->connection(), "SELECT COUNT(*) FROM statements"), 1);

    // 同文本重忆:engram 幂等命中(内容确定性 sha256 键),抽取 noop,
    // 不重复入库——管线顺序规则与幂等键派生都在被测函数内。
    const auto r2 = remember(*a, llm, "", params("Bob owns auth"));
    EXPECT_EQ(r2.outcome, "idempotent");
    EXPECT_EQ(r2.engram_ref, r1.engram_ref);
    EXPECT_TRUE(r2.statement_ids.empty());
    EXPECT_EQ(row_count(a->connection(), "SELECT COUNT(*) FROM engrams"), 1);
    EXPECT_EQ(row_count(a->connection(), "SELECT COUNT(*) FROM statements"), 1);
}

TEST(MemoryOps, TickAllAdvancesEmbeddingAndReturnsShape) {
    auto a = make_adapter();
    extractor::FakeLLMAdapter llm;
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});
    ASSERT_EQ(remember(*a, llm, "", params("Bob owns auth")).outcome, "accepted");

    embedding::StubEmbeddingAdapter emb(8);
    vector::SqliteBlobVectorIndex idx;
    embedding::EmbeddingWorker worker(*a, emb, idx);
    prospective::PolicyEngine policy(*a);

    const auto t = tick_all(*a, worker, policy, "2026-06-11T10:05:00Z");
    EXPECT_EQ(t.embedded, 1);   // 刚写入的语句被嵌入
    EXPECT_EQ(t.fired, 0);
    EXPECT_EQ(t.broken, 0);
    EXPECT_EQ(t.auto_withdrawn, 0);
    EXPECT_EQ(row_count(a->connection(), "SELECT COUNT(*) FROM statement_vectors"), 1);
}

TEST(MemoryOps, HolderBatchIsolatesFailureAndContinues) {
    auto adapter = make_adapter();
    HolderSelectiveLLMAdapter llm;
    const RememberPrompts prompts{
        .belief = "BELIEF::{convo}",
        .episodic = "EPISODIC::{passage}",
        .general_fact = "GENERAL::{self}::{convo}",
    };
    const auto make_params = [](std::string holder, std::string text) {
        RememberParams p;
        p.tenant_id = "default";
        p.holder_id = std::move(holder);
        p.adapter_name = "r35-test";
        p.source_prefix = "r35-test";
        p.created_at_iso8601 = "2026-09-21T00:00:00Z";
        p.payload.assign(text.begin(), text.end());
        return p;
    };

    const auto results = remember_holders(
        *adapter, llm, prompts,
        std::vector<RememberParams>{make_params("Bad", "Bad source"),
                                    make_params("Good", "Good source")},
        extractor::ValidationPolicy{});
    ASSERT_EQ(results.size(), 2u);
    EXPECT_EQ(results[0].holder_id, "Bad");
    EXPECT_TRUE(results[0].extraction_failed);
    EXPECT_EQ(results[0].failure_category, "transport_failure");
    EXPECT_EQ(results[0].failure_detail, "forced holder failure");
    EXPECT_FALSE(results[0].receipt.empty());
    EXPECT_EQ(results[1].holder_id, "Good");
    EXPECT_FALSE(results[1].extraction_failed);
    EXPECT_EQ(results[1].outcome, "accepted");
    EXPECT_EQ(row_count(adapter->connection(), "SELECT COUNT(*) FROM engrams"), 2);
}

// ── P2.o 运行时闭环 ──────────────────────────────────────────────────────────
// 根因回归:生产语句写经 StatementWriter 不经 Bus::write,挂在 Bus::write 尾部
// 的订阅者泵在 remember 路径永不运行(投影/信念/再巩固/在线回放全部缺席)。
// 泵宿主归位 memoryops::remember 后,以下行为必须成立。

TEST(MemoryOps, RememberRunsSubscriberPump) {
    auto a = make_adapter();
    extractor::FakeLLMAdapter llm;
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});

    ASSERT_EQ(remember(*a, llm, "", params("Bob owns auth")).outcome, "accepted");

    // 泵的 replay_online 订阅者跑过:在线触发计数 +1。
    EXPECT_EQ(row_count(a->connection(),
        "SELECT online_trigger_counter FROM replay_scheduler_state WHERE id=1"), 1);
    // 出生 salience = 中性 affect 公式值(≈0.0144),必须超过采样器 w_min,
    // 否则该语句永远不会被 Replay 采样巩固(根因之二回归)。
    EXPECT_EQ(row_count(a->connection(),
        "SELECT COUNT(*) FROM statements WHERE salience > 0.01"), 1);
}

TEST(MemoryOps, ThirdRememberTriggersOnlineConsolidation) {
    auto a = make_adapter();
    extractor::FakeLLMAdapter llm;
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});

    ASSERT_EQ(remember(*a, llm, "", params("text one")).outcome, "accepted");
    ASSERT_EQ(remember(*a, llm, "", params("text two")).outcome, "accepted");
    ASSERT_EQ(remember(*a, llm, "", params("text three")).outcome, "accepted");

    // 第 3 次写触发在线采样窗(kOnlineTrigger=3):计数归零 + 至少一条语句
    // volatile→consolidated(op_compress 一触即晋升)。
    EXPECT_EQ(row_count(a->connection(),
        "SELECT online_trigger_counter FROM replay_scheduler_state WHERE id=1"), 0);
    EXPECT_GE(row_count(a->connection(),
        "SELECT COUNT(*) FROM statements WHERE consolidation_state='consolidated'"), 1);
}

TEST(MemoryOps, TickAllConsolidatesProjectsAndDispatches) {
    auto a = make_adapter();
    extractor::FakeLLMAdapter llm;
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});
    ASSERT_EQ(remember(*a, llm, "", params("Bob owns auth")).outcome, "accepted");
    ASSERT_GE(row_count(a->connection(),
        "SELECT COUNT(*) FROM statements WHERE consolidation_state='volatile'"), 1);

    embedding::StubEmbeddingAdapter emb(8);
    vector::SqliteBlobVectorIndex idx;
    embedding::EmbeddingWorker worker(*a, emb, idx);
    prospective::PolicyEngine policy(*a);

    const auto t = tick_all(*a, worker, policy, "2026-06-11T10:05:00Z");

    // idle 批扫掉积压 volatile:写→读闭环的巩固半边。
    EXPECT_GE(t.replay_sampled, 1);
    EXPECT_GE(t.consolidated, 1);
    EXPECT_EQ(row_count(a->connection(),
        "SELECT COUNT(*) FROM statements WHERE consolidation_state='volatile'"), 0);
    // 出箱收敛:in_process Accept-all 消费者把 pending 全部标 delivered。
    EXPECT_GE(t.dispatched, 1);
    EXPECT_EQ(row_count(a->connection(),
        "SELECT COUNT(*) FROM bus_events WHERE dispatch_status='pending'"), 0);
    // 投影兜底批不抛且计数非负(remember 路径的事件已被泵内 PM 消费)。
    EXPECT_GE(t.projected, 0);
}

TEST(MemoryOps, TickAllRecordsStageTimings) {
    auto a = make_adapter();
    extractor::FakeLLMAdapter llm;
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});
    ASSERT_EQ(remember(*a, llm, "", params("Bob owns auth")).outcome, "accepted");

    embedding::StubEmbeddingAdapter emb(8);
    vector::SqliteBlobVectorIndex idx;
    embedding::EmbeddingWorker worker(*a, emb, idx);
    prospective::PolicyEngine policy(*a);

    const auto t = tick_all(*a, worker, policy, "2026-06-11T10:05:00Z");

    ASSERT_EQ(t.stage_timings_ms.size(), 9U);
    EXPECT_EQ(t.stage_timings_ms[0].stage, "embed");
    EXPECT_EQ(t.stage_timings_ms[1].stage, "policy");
    EXPECT_EQ(t.stage_timings_ms[2].stage, "common_ground");
    EXPECT_EQ(t.stage_timings_ms[3].stage, "replay_oscillation_guard");
    EXPECT_EQ(t.stage_timings_ms[4].stage, "replay_ttl_sweep");
    EXPECT_EQ(t.stage_timings_ms[5].stage, "replay_idle");
    EXPECT_EQ(t.stage_timings_ms[6].stage, "persona");
    EXPECT_EQ(t.stage_timings_ms[7].stage, "projection");
    EXPECT_EQ(t.stage_timings_ms[8].stage, "outbox");
    for (const auto& timing : t.stage_timings_ms) {
        EXPECT_GE(timing.duration_ms, 0);
    }
}

// ── P3.c live-wiring LW.2: health-gated load-shedding ───────────────────────

// Helper: does stages_skipped contain the given label?
static bool skipped(const TickOutcome& t, const std::string& label) {
    return std::find(t.stages_skipped.begin(), t.stages_skipped.end(), label)
           != t.stages_skipped.end();
}

// Helper: does stage_timings_ms contain the given label?
static bool timed(const TickOutcome& t, const std::string& label) {
    for (const auto& st : t.stage_timings_ms) {
        if (st.stage == label) {
            return true;
        }
    }
    return false;
}

// READY (regression): default param keeps all 9 stages running, stages_skipped
// empty, timings all 9.
TEST(MemoryOps, TickAllReadyRunsAllStages) {
    auto a = make_adapter();
    extractor::FakeLLMAdapter llm;
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});
    ASSERT_EQ(remember(*a, llm, "", params("Alice reads code")).outcome, "accepted");

    embedding::StubEmbeddingAdapter emb(8);
    vector::SqliteBlobVectorIndex idx;
    embedding::EmbeddingWorker worker(*a, emb, idx);
    prospective::PolicyEngine policy(*a);

    // Explicit READY — same as default-param path.
    const auto t = tick_all(*a, worker, policy, "2026-06-11T10:05:00Z",
                            RuntimeHealth::READY);

    EXPECT_TRUE(t.stages_skipped.empty());
    ASSERT_EQ(t.stage_timings_ms.size(), 9U);
    // embed ran: the pending-embedding row got embedded.
    EXPECT_EQ(t.embedded, 1);
}

// 裁定 R54:skip_embed 只跳过 Embed 阶段(stages_skipped 只有 "embed"),其余 8 个阶段照常跑。
TEST(MemoryOps, TickAllSkipEmbedSkipsOnlyTheEmbedStage) {
    auto a = make_adapter();
    extractor::FakeLLMAdapter llm;
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});
    ASSERT_EQ(remember(*a, llm, "", params("Dana reviews code")).outcome, "accepted");

    embedding::StubEmbeddingAdapter emb(8);
    vector::SqliteBlobVectorIndex idx;
    embedding::EmbeddingWorker worker(*a, emb, idx);
    prospective::PolicyEngine policy(*a);

    const auto outcome = tick_all(*a, worker, policy, "2026-06-11T10:05:00Z",
                                  RuntimeHealth::READY, /*skip_embed=*/true);

    EXPECT_EQ(outcome.embedded, 0);
    EXPECT_EQ(row_count(a->connection(), "SELECT COUNT(*) FROM statement_vectors"), 0);
    ASSERT_EQ(outcome.stages_skipped.size(), 1U);
    EXPECT_EQ(outcome.stages_skipped.front(), "embed");
    EXPECT_EQ(outcome.stage_timings_ms.size(), 8U);
    EXPECT_FALSE(timed(outcome, "embed"));
    EXPECT_TRUE(timed(outcome, "policy"));
    EXPECT_TRUE(timed(outcome, "outbox"));
    EXPECT_GE(outcome.dispatched, 1);        // 出箱照常收敛
}

// DEGRADED causality: soft stages are NOT run (embed effect absent); critical
// stages DO run (outbox drains). stages_skipped == 5 soft labels. Timings only
// for the 4 critical stages.
TEST(MemoryOps, TickAllDegradedShedsEmbedLeavesRowUnembedded) {
    auto a = make_adapter();
    extractor::FakeLLMAdapter llm;
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});
    // Seed a statement that needs embedding so the embed stage would do real work.
    ASSERT_EQ(remember(*a, llm, "", params("Charlie deploys daily")).outcome, "accepted");

    embedding::StubEmbeddingAdapter emb(8);
    vector::SqliteBlobVectorIndex idx;
    embedding::EmbeddingWorker worker(*a, emb, idx);
    prospective::PolicyEngine policy(*a);

    const auto t = tick_all(*a, worker, policy, "2026-06-11T10:05:00Z",
                            RuntimeHealth::DEGRADED);

    // Causality: soft stage effect absent — row remains un-embedded.
    EXPECT_EQ(t.embedded, 0);
    EXPECT_EQ(row_count(a->connection(),
        "SELECT COUNT(*) FROM statement_vectors"), 0);

    // stages_skipped == the 5 soft labels (order matches tick order).
    ASSERT_EQ(t.stages_skipped.size(), 5U);
    EXPECT_TRUE(skipped(t, "embed"));
    EXPECT_TRUE(skipped(t, "common_ground"));
    EXPECT_TRUE(skipped(t, "replay_idle"));
    EXPECT_TRUE(skipped(t, "persona"));
    EXPECT_TRUE(skipped(t, "projection"));

    // Timings only for the 4 critical stages.
    EXPECT_EQ(t.stage_timings_ms.size(), 4U);
    EXPECT_TRUE(timed(t, "policy"));
    EXPECT_TRUE(timed(t, "replay_oscillation_guard"));
    EXPECT_TRUE(timed(t, "replay_ttl_sweep"));
    EXPECT_TRUE(timed(t, "outbox"));

    // Critical stages ran: outbox dispatched pending bus events (bus_events seeded
    // by remember's subscriber pump; Accept-all consumer marks them delivered).
    EXPECT_EQ(row_count(a->connection(),
        "SELECT COUNT(*) FROM bus_events WHERE dispatch_status='pending'"), 0);
}

// DRAINING: only outbox runs; 8 stages skipped, outbox NOT skipped; outbox
// still drains pending delivery.
TEST(MemoryOps, TickAllDrainingKeepsOnlyOutbox) {
    auto a = make_adapter();
    extractor::FakeLLMAdapter llm;
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});
    ASSERT_EQ(remember(*a, llm, "", params("Dave ships features")).outcome, "accepted");

    embedding::StubEmbeddingAdapter emb(8);
    vector::SqliteBlobVectorIndex idx;
    embedding::EmbeddingWorker worker(*a, emb, idx);
    prospective::PolicyEngine policy(*a);

    const auto t = tick_all(*a, worker, policy, "2026-06-11T10:05:00Z",
                            RuntimeHealth::DRAINING);

    // 8 stages skipped; outbox is NOT in the skip list.
    ASSERT_EQ(t.stages_skipped.size(), 8U);
    EXPECT_FALSE(skipped(t, "outbox"));
    EXPECT_TRUE(skipped(t, "embed"));
    EXPECT_TRUE(skipped(t, "policy"));
    EXPECT_TRUE(skipped(t, "common_ground"));
    EXPECT_TRUE(skipped(t, "replay_oscillation_guard"));
    EXPECT_TRUE(skipped(t, "replay_ttl_sweep"));
    EXPECT_TRUE(skipped(t, "replay_idle"));
    EXPECT_TRUE(skipped(t, "persona"));
    EXPECT_TRUE(skipped(t, "projection"));

    // Only outbox timed.
    ASSERT_EQ(t.stage_timings_ms.size(), 1U);
    EXPECT_EQ(t.stage_timings_ms[0].stage, "outbox");

    // Outbox still drains (causality: critical delivery preserved under DRAINING).
    EXPECT_EQ(row_count(a->connection(),
        "SELECT COUNT(*) FROM bus_events WHERE dispatch_status='pending'"), 0);
}

// UNREADY: all 9 stages skipped; no timings; no side effects.
TEST(MemoryOps, TickAllUnreadySkipsAllStages) {
    auto a = make_adapter();
    extractor::FakeLLMAdapter llm;
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});
    ASSERT_EQ(remember(*a, llm, "", params("Eve reviews PRs")).outcome, "accepted");

    embedding::StubEmbeddingAdapter emb(8);
    vector::SqliteBlobVectorIndex idx;
    embedding::EmbeddingWorker worker(*a, emb, idx);
    prospective::PolicyEngine policy(*a);

    const auto t = tick_all(*a, worker, policy, "2026-06-11T10:05:00Z",
                            RuntimeHealth::UNREADY);

    ASSERT_EQ(t.stages_skipped.size(), 9U);
    EXPECT_TRUE(t.stage_timings_ms.empty());
    EXPECT_EQ(t.embedded, 0);
}

// ── embed_pending:只跑嵌入阶段,循环排空待嵌入语句 ───────────────────────────
// 钉:积压超过一批(WorkerConfig::batch_size=32)时一次调用排空;某批嵌入 0 条即停,
// 只剩失败行时不在同一次调用里反复重试;一批里有成功有失败仍继续下一批。

namespace {

std::string many_claims_json(int count) {
    std::string json = "[";
    for (int idx = 0; idx < count; ++idx) {
        if (idx > 0) json += ",";
        json += R"({"holder":"self","holder_perspective":"FIRST_PERSON","subject":"Sub)" +
                std::to_string(idx) +
                R"(","predicate":"responsible_for","object":"area)" + std::to_string(idx) +
                R"(","modality":"BELIEVES","polarity":"POS","nesting_depth":0})";
    }
    return json + "]";
}

// 一次 remember、抽取响应带 count 条互不相同的主张 → count 条待嵌入语句。
void seed_pending_statements(persistence::SqliteAdapter& adapter, int count) {
    extractor::FakeLLMAdapter llm;
    llm.set_default_response(
        extractor::LLMResponse{.raw_xml = many_claims_json(count), .ok = true});
    const auto result = remember(adapter, llm, "", params("many claims"));
    ASSERT_EQ(result.outcome, "accepted");
    ASSERT_EQ(result.statement_ids.size(), static_cast<std::size_t>(count));
}

// 失败脚本嵌入器。AlwaysDown:批量与逐行都抛可重试错误(端点整体不可用)。
// OneRowFails:第一次批量调用抛永久错误,逼 worker 退到逐行,逐行的第一条抛错,
// 其余成功;之后的批量调用都成功。与 SQLite 扫描顺序无关。
class ScriptedEmbedder final : public embedding::EmbeddingAdapter {
public:
    enum class Mode { AlwaysDown, OneRowFails };
    explicit ScriptedEmbedder(Mode mode) : mode_(mode) {}

    embedding::EmbeddingResult embed(std::string_view text) override {
        if (mode_ == Mode::AlwaysDown) throw embedding::EmbeddingError("down");
        if (!row_failed_) {
            row_failed_ = true;
            throw embedding::EmbeddingError("one bad row");
        }
        return inner_.embed(text);
    }
    std::vector<embedding::EmbeddingResult> embed_batch(
            const std::vector<std::string>& texts) override {
        ++batch_calls;
        if (mode_ == Mode::AlwaysDown) throw embedding::EmbeddingError("down");
        if (batch_calls == 1) throw std::runtime_error("permanent_batch");
        std::vector<embedding::EmbeddingResult> out;
        for (const auto& text : texts) out.push_back(inner_.embed(text));
        return out;
    }
    int dim() const override { return inner_.dim(); }
    std::string model() const override { return inner_.model(); }

    int batch_calls = 0;

private:
    Mode mode_;
    bool row_failed_ = false;
    embedding::StubEmbeddingAdapter inner_{8};
};

}  // namespace

TEST(MemoryOps, EmbedPendingDrainsAllBatches) {
    auto a = make_adapter();
    constexpr int kStatements = 40;   // 超过 WorkerConfig::batch_size 默认 32
    ASSERT_GT(kStatements, embedding::WorkerConfig().batch_size);
    ASSERT_NO_FATAL_FAILURE(seed_pending_statements(*a, kStatements));

    embedding::StubEmbeddingAdapter emb(8);
    vector::SqliteBlobVectorIndex idx;
    embedding::EmbeddingWorker worker(*a, emb, idx);

    const auto out = embed_pending(*a, worker, "2026-06-11T10:05:00Z");
    EXPECT_EQ(out.embedded, kStatements);
    EXPECT_EQ(out.failed, 0);
    EXPECT_EQ(row_count(a->connection(),
        "SELECT COUNT(*) FROM statement_vectors WHERE status='embedded'"), kStatements);

    // 排空后再调是空操作。
    const auto again = embed_pending(*a, worker, "2026-06-11T10:05:01Z");
    EXPECT_EQ(again.embedded, 0);
    EXPECT_EQ(again.failed, 0);
}

TEST(MemoryOps, EmbedPendingStopsAfterOneBatchWhenNothingEmbeds) {
    auto a = make_adapter();
    ASSERT_NO_FATAL_FAILURE(seed_pending_statements(*a, 40));

    ScriptedEmbedder emb(ScriptedEmbedder::Mode::AlwaysDown);
    vector::SqliteBlobVectorIndex idx;
    embedding::EmbeddingWorker worker(*a, emb, idx);

    const auto out = embed_pending(*a, worker, "2026-06-11T10:05:00Z");
    EXPECT_EQ(out.embedded, 0);
    EXPECT_EQ(out.failed, embedding::WorkerConfig().batch_size);   // 只有第一批
    EXPECT_EQ(emb.batch_calls, 1);                                 // 没有再扫第二批
    // 失败行只被尝试一次:本次调用没有在同一次里重试它们。
    EXPECT_EQ(row_count(a->connection(),
        "SELECT MAX(retry_count) FROM statement_vectors WHERE status='failed'"), 1);
}

TEST(MemoryOps, EmbedPendingContinuesPastBatchWithPartialFailure) {
    auto a = make_adapter();
    ASSERT_NO_FATAL_FAILURE(seed_pending_statements(*a, 40));

    ScriptedEmbedder emb(ScriptedEmbedder::Mode::OneRowFails);
    vector::SqliteBlobVectorIndex idx;
    embedding::EmbeddingWorker worker(*a, emb, idx);

    // 第一批 31 成功 1 失败(embedded>0,继续);第二批把剩余 8 条与那条失败行一起嵌完;
    // 第三批无事可做(embedded==0,停)。
    const auto out = embed_pending(*a, worker, "2026-06-11T10:05:00Z");
    EXPECT_EQ(out.embedded, 40);
    EXPECT_EQ(out.failed, 1);
    EXPECT_EQ(row_count(a->connection(),
        "SELECT COUNT(*) FROM statement_vectors WHERE status='embedded'"), 40);
}

// ── 设置页「危险区」:clear_tenant ───────────────────────────────────────────
// 钉:单租户物理清空(保留 self 认知体)、其他租户零影响、无悬空外键、写门、
// 新增租户作用域表必须纳入删除清单、清空后同文本重新 remember 是全新 accepted。

namespace {

RememberParams tenant_params(std::string tenant, std::string_view text) {
    RememberParams scoped = params(text);
    scoped.tenant_id = std::move(tenant);
    return scoped;
}

std::int64_t count_rows(persistence::Connection& conn, const std::string& sql) {
    persistence::Stmt query(conn.raw(), sql.c_str());
    EXPECT_TRUE(query.step_row()) << sql;
    return sqlite3_column_int64(query.raw(), 0);
}

// (表名, 租户列名):清单必须覆盖的全部租户作用域表(视图不算)。
std::vector<std::pair<std::string, std::string>> tenant_scoped_tables(
        persistence::Connection& conn) {
    persistence::Stmt query(conn.raw(),
        "SELECT m.name, p.name FROM sqlite_master m, pragma_table_info(m.name) p "
        "WHERE m.type='table' AND p.name IN ('tenant_id','window_tenant_id') "
        "ORDER BY m.name");
    std::vector<std::pair<std::string, std::string>> tables;
    while (query.step_row()) {
        // NOLINTBEGIN(cppcoreguidelines-pro-type-reinterpret-cast)
        tables.emplace_back(reinterpret_cast<const char*>(sqlite3_column_text(query.raw(), 0)),
                            reinterpret_cast<const char*>(sqlite3_column_text(query.raw(), 1)));
        // NOLINTEND(cppcoreguidelines-pro-type-reinterpret-cast)
    }
    return tables;
}

void seed_cognizer(persistence::Connection& conn, std::string_view tenant,
                   std::string_view cognizer_id, std::string_view kind) {
    persistence::Stmt insert(conn.raw(),
        "INSERT INTO cognizers(id,tenant_id,kind,canonical_name,canonical_name_normalized,"
        "external_id,created_at,last_seen_at) "
        "VALUES(?1,?2,?3,?1,?1,?1,'2026-06-11T10:00:00Z','2026-06-11T10:00:00Z')");
    insert.bind(cognizer_id, tenant, kind);
    insert.step_done();
}

// remember + 一次 tick:让该租户有 engram/语句/事件/向量/投影/抽取审计行。
void populate_tenant(persistence::SqliteAdapter& adapter, const std::string& tenant) {
    extractor::FakeLLMAdapter llm;
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});
    ASSERT_EQ(remember(adapter, llm, "", tenant_params(tenant, "Bob owns auth")).outcome,
              "accepted");
    embedding::StubEmbeddingAdapter emb(8);
    vector::SqliteBlobVectorIndex index;
    embedding::EmbeddingWorker worker(adapter, emb, index);
    prospective::PolicyEngine policy(adapter);
    (void)tick_all(adapter, worker, policy, "2026-06-11T10:05:00Z");
}

}  // namespace

TEST(ClearTenant, RemovesTenantRowsKeepsSelfAndOtherTenants) {
    auto adapter = make_adapter();
    populate_tenant(*adapter, "default");
    populate_tenant(*adapter, "other");
    auto& conn = adapter->connection();
    seed_cognizer(conn, "default", "cog-self", "self");
    seed_cognizer(conn, "default", "cog-bob", "human");
    seed_cognizer(conn, "other", "cog-carol", "human");
    ASSERT_GE(count_rows(conn,
        "SELECT COUNT(*) FROM statement_vectors WHERE tenant_id='default'"), 1);
    const auto other_statements =
        count_rows(conn, "SELECT COUNT(*) FROM statements WHERE tenant_id='other'");
    ASSERT_GE(other_statements, 1);

    const auto out = clear_tenant(*adapter, "default");

    EXPECT_GT(out.total, 0);
    EXPECT_GE(out.deleted.at("statements"), 1);
    EXPECT_GE(out.deleted.at("engrams"), 1);
    EXPECT_EQ(out.deleted.at("cognizers"), 1);   // 只删 cog-bob
    for (const auto& [table, column] : tenant_scoped_tables(conn)) {
        std::string sql = "SELECT COUNT(*) FROM " + table + " WHERE " + column + "='default'";
        if (table == "cognizers") { sql += " AND kind<>'self'"; }
        EXPECT_EQ(count_rows(conn, sql), 0) << table;
    }
    EXPECT_EQ(count_rows(conn,
        "SELECT COUNT(*) FROM cognizers WHERE tenant_id='default' AND kind='self'"), 1);
    // 无租户列的子表:跟着父行走,不留孤儿。
    EXPECT_EQ(count_rows(conn,
        "SELECT COUNT(*) FROM extraction_attempt a WHERE NOT EXISTS "
        "(SELECT 1 FROM governance_pipeline_run r WHERE r.id=a.pipeline_run_id)"), 0);
    // 其他租户零影响。
    EXPECT_EQ(count_rows(conn, "SELECT COUNT(*) FROM statements WHERE tenant_id='other'"),
              other_statements);
    EXPECT_GE(count_rows(conn, "SELECT COUNT(*) FROM engrams WHERE tenant_id='other'"), 1);
    EXPECT_GE(count_rows(conn, "SELECT COUNT(*) FROM extraction_attempt"), 1);
    EXPECT_EQ(count_rows(conn, "SELECT COUNT(*) FROM cognizers WHERE tenant_id='other'"), 1);
    // 全库无悬空外键。
    persistence::Stmt fk_check(conn.raw(), "PRAGMA foreign_key_check");
    EXPECT_FALSE(fk_check.step_row());
}

void seed_ledger(persistence::Connection& conn, std::string_view batch_id,
                 const char* tenant /* nullptr = NULL */) {
    persistence::Stmt insert(conn.raw(),
        "INSERT INTO replay_ledger(replay_batch_id,mode,sampled_count,ops_applied_json,"
        "started_at,tenant_id) VALUES(?1,'idle',1,'{}','2026-06-11T10:00:00Z',?2)");
    if (tenant != nullptr) {
        insert.bind(batch_id, std::string_view(tenant));
    } else {
        insert.bind(batch_id, nullptr);
    }
    insert.step_done();
}

TEST(ClearTenant, ReplayLedgerDeletesOwnRowsKeepsOtherTenants) {
    auto adapter = make_adapter();
    populate_tenant(*adapter, "default");
    populate_tenant(*adapter, "other");
    auto& conn = adapter->connection();
    seed_ledger(conn, "b-default", "default");
    seed_ledger(conn, "b-other", "other");
    seed_ledger(conn, "b-legacy", nullptr);   // 迁移前的旧行 / 跨租户批次
    // populate_tenant 里的 tick_all 自己也会写单租户账本行(归 default),所以期望值取
    // 清空前 default 名下的实际行数,而不是假设账本一开始是空的。
    const auto own_rows =
        count_rows(conn, "SELECT COUNT(*) FROM replay_ledger WHERE tenant_id='default'");
    ASSERT_GE(own_rows, 1);

    const auto out = clear_tenant(*adapter, "default");

    EXPECT_EQ(out.deleted.at("replay_ledger"), own_rows);
    EXPECT_EQ(count_rows(conn, "SELECT COUNT(*) FROM replay_ledger WHERE tenant_id='default'"), 0);
    EXPECT_EQ(count_rows(conn, "SELECT COUNT(*) FROM replay_ledger WHERE replay_batch_id='b-default'"), 0);
    EXPECT_EQ(count_rows(conn, "SELECT COUNT(*) FROM replay_ledger WHERE replay_batch_id='b-other'"), 1);
    // 另一个租户还有数据时,无法归属的 NULL 行可能是它的,不能删。
    EXPECT_EQ(count_rows(conn, "SELECT COUNT(*) FROM replay_ledger WHERE replay_batch_id='b-legacy'"), 1);
}

TEST(ClearTenant, ReplayLedgerNullRowsGoWhenNoOtherTenantHasData) {
    auto adapter = make_adapter();
    populate_tenant(*adapter, "default");
    auto& conn = adapter->connection();
    seed_ledger(conn, "b-default", "default");
    seed_ledger(conn, "b-legacy", nullptr);
    const auto all_rows = count_rows(conn, "SELECT COUNT(*) FROM replay_ledger");
    ASSERT_GE(all_rows, 2);   // 两条手工行 + tick_all 自己写的行

    const auto out = clear_tenant(*adapter, "default");

    // 库里只剩这一个租户:NULL 行只可能是它的,一并删除,不留残余梦境日志。
    EXPECT_EQ(out.deleted.at("replay_ledger"), all_rows);
    EXPECT_EQ(count_rows(conn, "SELECT COUNT(*) FROM replay_ledger"), 0);
}

TEST(ClearTenant, CoversEveryTenantScopedTable) {
    // 新迁移加了租户作用域表却没进 clear_tenant 的删除清单 → 这里红。
    auto adapter = make_adapter();
    const auto out = clear_tenant(*adapter, "default");
    EXPECT_EQ(out.total, 0);
    const auto tables = tenant_scoped_tables(adapter->connection());
    ASSERT_FALSE(tables.empty());
    for (const auto& [table, column] : tables) {
        EXPECT_EQ(out.deleted.count(table), 1U) << "clear_tenant misses " << table;
    }
    EXPECT_EQ(out.deleted.count("extraction_attempt"), 1U);
    EXPECT_EQ(out.deleted.count("idempotency_inbox"), 1U);
}

TEST(ClearTenant, RejectedWhenGateClosedOrTenantEmpty) {
    auto adapter = make_adapter();
    populate_tenant(*adapter, "default");
    auto& conn = adapter->connection();
    const auto before = count_rows(conn, "SELECT COUNT(*) FROM statements");

    EXPECT_THROW((void)clear_tenant(*adapter, ""), std::invalid_argument);
    adapter->set_write_admit([] { return false; });
    EXPECT_THROW((void)clear_tenant(*adapter, "default"), governance::WriteGateRejected);
    EXPECT_EQ(count_rows(conn, "SELECT COUNT(*) FROM statements"), before);   // 零写
}

TEST(ClearTenant, RememberStartsFreshAfterClear) {
    auto adapter = make_adapter();
    extractor::FakeLLMAdapter llm;
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});
    ASSERT_EQ(remember(*adapter, llm, "", params("Bob owns auth")).outcome, "accepted");

    (void)clear_tenant(*adapter, "default");

    // 旧 engram 已删:同文本不再判 idempotent,而是全新入库并重新抽取。
    const auto again = remember(*adapter, llm, "", params("Bob owns auth"));
    EXPECT_EQ(again.outcome, "accepted");
    EXPECT_FALSE(again.statement_ids.empty());
    EXPECT_GE(count_rows(adapter->connection(),
        "SELECT COUNT(*) FROM statements WHERE tenant_id='default'"), 1);
}

// ── backup_to:VACUUM INTO 加只读 integrity_check ────────────────────────────

namespace {

namespace fs = std::filesystem;

// 每个用例一个独立的临时目录,析构时删除。
class BackupDir {
public:
    BackupDir() {
        static std::atomic<int> seq{0};
        path_ = fs::temp_directory_path() /
                ("starling-backup-" + std::to_string(::getpid()) + "-" + std::to_string(++seq));
        fs::remove_all(path_);
        fs::create_directories(path_);
    }
    ~BackupDir() { std::error_code err; fs::remove_all(path_, err); }
    BackupDir(const BackupDir&) = delete;
    BackupDir& operator=(const BackupDir&) = delete;
    BackupDir(BackupDir&&) = delete;
    BackupDir& operator=(BackupDir&&) = delete;
    [[nodiscard]] const fs::path& path() const { return path_; }
private:
    fs::path path_;
};

std::unique_ptr<persistence::SqliteAdapter> open_file_adapter(const fs::path& file) {
    auto a = persistence::SqliteAdapter::open(file);
    persistence::MigrationRunner(a->connection().raw()).migrate_to_latest();
    return a;
}

// 只读打开一个库文件,取第一行第一列的文本。副本不带 -wal,所以必须用只读连接看:
// 读写连接会把 journal_mode 改回 WAL,还会在旁边建 -wal/-shm。
std::string read_only_scalar(const fs::path& file, const char* sql) {
    sqlite3* raw = nullptr;
    EXPECT_EQ(sqlite3_open_v2(file.string().c_str(), &raw, SQLITE_OPEN_READONLY, nullptr),
              SQLITE_OK);
    persistence::SqliteHandle handle(raw);
    persistence::Stmt query(handle.get(), sql);
    if (!query.step_row()) { return {}; }
    // NOLINTNEXTLINE(cppcoreguidelines-pro-type-reinterpret-cast)
    const auto* text = reinterpret_cast<const char*>(sqlite3_column_text(query.raw(), 0));
    return text != nullptr ? text : "";
}

std::string slurp(const fs::path& file) {
    std::ifstream input(file, std::ios::binary);
    return {std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>()};
}

void spit(const fs::path& file, std::string_view bytes) {
    std::ofstream out(file, std::ios::binary | std::ios::trunc);
    out.write(bytes.data(), static_cast<std::streamsize>(bytes.size()));
}

void remember_bob(persistence::SqliteAdapter& a, extractor::FakeLLMAdapter& llm,
                  std::string_view text) {
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});
    ASSERT_EQ(remember(a, llm, "", params(text)).outcome, "accepted");
}

}  // namespace

TEST(BackupTo, CopyHoldsWalContentAndIsSelfContained) {
    const BackupDir dir;
    auto a = open_file_adapter(dir.path() / "src.db");
    extractor::FakeLLMAdapter llm;
    remember_bob(*a, llm, "Bob owns auth");
    // 源库是 WAL 模式、连接还开着:新写的内容可能只在 -wal 里,副本必须把它带上。
    ASSERT_TRUE(fs::exists(dir.path() / "src.db-wal"));

    backup_to(*a, dir.path() / "copy.db");

    const auto copy = dir.path() / "copy.db";
    EXPECT_EQ(read_only_scalar(copy, "PRAGMA integrity_check"), "ok");
    EXPECT_EQ(read_only_scalar(copy, "PRAGMA journal_mode"), "delete");   // 不是 wal
    EXPECT_FALSE(fs::exists(dir.path() / "copy.db-wal"));
    EXPECT_FALSE(fs::exists(dir.path() / "copy.db-shm"));
    EXPECT_FALSE(fs::exists(dir.path() / "copy.db.partial"));
    EXPECT_EQ(read_only_scalar(copy, "SELECT COUNT(*) FROM statements"),
              std::to_string(row_count(a->connection(), "SELECT COUNT(*) FROM statements")));
    EXPECT_EQ(read_only_scalar(copy, "SELECT COUNT(*) FROM engrams"), "1");
    EXPECT_EQ(read_only_scalar(copy, "SELECT COUNT(*) FROM schema_migrations"),
              std::to_string(row_count(a->connection(), "SELECT COUNT(*) FROM schema_migrations")));

    // 备份是只读副本操作:源库照常可写。
    remember_bob(*a, llm, "Carol owns billing");
    EXPECT_EQ(row_count(a->connection(), "SELECT COUNT(*) FROM engrams"), 2);
    EXPECT_EQ(read_only_scalar(copy, "SELECT COUNT(*) FROM engrams"), "1");   // 副本不跟着变
}

TEST(BackupTo, WorksOnInMemoryLibrary) {
    const BackupDir dir;
    auto a = make_adapter();
    extractor::FakeLLMAdapter llm;
    remember_bob(*a, llm, "Bob owns auth");

    backup_to(*a, dir.path() / "mem-copy.db");

    EXPECT_EQ(read_only_scalar(dir.path() / "mem-copy.db", "PRAGMA integrity_check"), "ok");
    EXPECT_EQ(read_only_scalar(dir.path() / "mem-copy.db", "SELECT COUNT(*) FROM engrams"), "1");
}

TEST(BackupTo, RefusesExistingDestinationAndTouchesNothing) {
    const BackupDir dir;
    auto a = open_file_adapter(dir.path() / "src.db");
    const auto dest = dir.path() / "copy.db";
    spit(dest, "x");

    try {
        backup_to(*a, dest);
        FAIL() << "expected filesystem_error";
    } catch (const fs::filesystem_error& e) {
        EXPECT_EQ(e.code(), std::errc::file_exists);
        EXPECT_EQ(e.path1(), dest);
    }
    EXPECT_EQ(slurp(dest), "x");
    EXPECT_FALSE(fs::exists(dir.path() / "copy.db.partial"));
}

TEST(BackupTo, DanglingSymlinkCountsAsExistingDestination) {
    // exists() 会跟着链接走,悬空链接上得到 false;rename 却会越过链接写到它指向的位置。
    const BackupDir dir;
    auto a = open_file_adapter(dir.path() / "src.db");
    const auto dest = dir.path() / "copy.db";
    fs::create_symlink(dir.path() / "nowhere.db", dest);

    EXPECT_THROW(backup_to(*a, dest), fs::filesystem_error);
    EXPECT_TRUE(fs::is_symlink(dest));
    EXPECT_FALSE(fs::exists(dir.path() / "nowhere.db"));
    EXPECT_FALSE(fs::exists(dir.path() / "copy.db.partial"));
}

TEST(BackupTo, ReplacesStalePartialLeftByAnEarlierCrash) {
    const BackupDir dir;
    auto a = open_file_adapter(dir.path() / "src.db");
    const auto dest = dir.path() / "copy.db";
    spit(dir.path() / "copy.db.partial", "garbage from a crashed backup");

    backup_to(*a, dest);

    EXPECT_EQ(read_only_scalar(dest, "PRAGMA integrity_check"), "ok");
    EXPECT_FALSE(fs::exists(dir.path() / "copy.db.partial"));
}

TEST(BackupTo, MissingParentDirectoryFailsAndCreatesNothing) {
    const BackupDir dir;
    auto a = open_file_adapter(dir.path() / "src.db");

    EXPECT_THROW(backup_to(*a, dir.path() / "nope" / "copy.db"), persistence::SqliteError);
    EXPECT_FALSE(fs::exists(dir.path() / "nope"));   // 不代建上级目录
}

TEST(BackupTo, RejectsEmptyPath) {
    auto a = make_adapter();
    EXPECT_THROW(backup_to(*a, fs::path{}), std::invalid_argument);
}

TEST(BackupTo, OpenTransactionFailsWithoutLeavingPartial) {
    // VACUUM 不能在事务里跑。调用方持着写事务时备份要失败,而不是写出半截副本。
    const BackupDir dir;
    auto a = open_file_adapter(dir.path() / "src.db");
    const auto dest = dir.path() / "copy.db";

    {
        persistence::TransactionGuard txn(a->connection());
        EXPECT_THROW(backup_to(*a, dest), persistence::SqliteError);
    }
    EXPECT_FALSE(fs::exists(dest));
    EXPECT_FALSE(fs::exists(dir.path() / "copy.db.partial"));

    backup_to(*a, dest);   // 事务结束后同一个目标可以再试
    EXPECT_EQ(read_only_scalar(dest, "PRAGMA integrity_check"), "ok");
}

TEST(BackupTo, IntegrityCheckFailureRemovesPartialAndLeavesNoDestination) {
    // 造一份"打得开、能 VACUUM INTO、但 integrity_check 不通过"的源库:先在表里塞一行 NULL,
    // 再把 sqlite_master 里的列定义改成 NOT NULL。违例要靠副本上的 PRAGMA integrity_check
    // 才报得出来("NULL value in side.val")。
    //
    // 造完必须关掉这条连接、让 adapter 重新打开:同一条连接的内存里还是改之前的表定义,
    // 与 sqlite_master 里的新定义不一致,VACUUM INTO 就不走整页直拷(两边表定义相同才走),
    // 改成真的逐行插入,在插入时撞上 NOT NULL 约束,抛 SqliteError 而不是走到完整性检查。
    // 也不用改索引定义的办法:VACUUM INTO 按改过的定义重建索引,副本反而自洽。
    //
    // 也不用 CHECK 约束违例:只读连接上的 integrity_check 看不见它(读写连接才报
    // "CHECK constraint failed"),backup_to 的只读完整性检查对这一类违例是盲的,见
    // memory_ops.cpp 里 integrity_ok 的注释。NOT NULL 违例两种连接都报得出来。
    const BackupDir dir;
    const auto src = dir.path() / "src.db";
    { auto seed = open_file_adapter(src); }   // 建库、跑完迁移,然后关掉
    {
        sqlite3* raw = nullptr;
        ASSERT_EQ(sqlite3_open(src.string().c_str(), &raw), SQLITE_OK);
        const persistence::SqliteHandle handle(raw);
        sqlite3_db_config(raw, SQLITE_DBCONFIG_DEFENSIVE, 0, nullptr);
        char* err = nullptr;
        ASSERT_EQ(sqlite3_exec(
                      raw,
                      "CREATE TABLE side(id INTEGER PRIMARY KEY, val TEXT);"
                      "INSERT INTO side(val) VALUES (NULL);"
                      "PRAGMA writable_schema=ON;"
                      "UPDATE sqlite_master SET sql="
                      "'CREATE TABLE side(id INTEGER PRIMARY KEY, val TEXT NOT NULL)' "
                      "WHERE name='side';",
                      nullptr, nullptr, &err),
                  SQLITE_OK) << (err != nullptr ? err : "");
        sqlite3_free(err);
    }
    auto a = open_file_adapter(src);
    ASSERT_EQ(read_only_scalar(src, "SELECT COUNT(*) FROM pragma_integrity_check "
                                    "WHERE integrity_check='ok'"), "0")
        << "要的是 integrity_check 不通过的源库";
    const auto dest = dir.path() / "copy.db";

    try {
        backup_to(*a, dest);
        FAIL() << "expected runtime_error";
    } catch (const persistence::SqliteError& e) {
        FAIL() << "VACUUM INTO itself failed, the integrity_check branch was not reached: "
               << e.what();
    } catch (const std::runtime_error& e) {
        EXPECT_STREQ(e.what(), "backup integrity_check failed");
    }
    EXPECT_FALSE(fs::exists(dest));
    EXPECT_FALSE(fs::exists(dir.path() / "copy.db.partial"));
    // 源库不受影响:仍可读。
    EXPECT_EQ(row_count(a->connection(), "SELECT COUNT(*) FROM side"), 1);
}

TEST(BackupTo, CopyOfCopyIsStable) {
    // 副本再备份一次:内容(行数、迁移记录)不漂,便于 rise 把起点副本当新起点用。
    const BackupDir dir;
    auto a = open_file_adapter(dir.path() / "src.db");
    extractor::FakeLLMAdapter llm;
    remember_bob(*a, llm, "Bob owns auth");
    backup_to(*a, dir.path() / "copy1.db");

    auto b = open_file_adapter(dir.path() / "copy1.db");
    backup_to(*b, dir.path() / "copy2.db");

    for (const char* sql : {"SELECT COUNT(*) FROM engrams", "SELECT COUNT(*) FROM statements",
                            "SELECT COUNT(*) FROM schema_migrations"}) {
        EXPECT_EQ(read_only_scalar(dir.path() / "copy1.db", sql),
                  read_only_scalar(dir.path() / "copy2.db", sql)) << sql;
    }
}

}  // namespace starling::memoryops
