// Context Pack 8 标签判定(13_retrieval.md §核心算法-3)。
// 优先级(首中即停):TODO > CONFLICT > COMMON > INFERRED > HEARSAY > BELIEF > FACT。
// ABSTAIN 不走 classify——由 abstention gate 注入整包。
#include "starling/retrieval/context_pack.hpp"

#include <gtest/gtest.h>

namespace starling::retrieval {

namespace {
StatementRow row(const char* id, const char* holder, const char* modality,
                 double conf, const char* evidence_count = "two") {
    StatementRow r;
    r.id = id; r.holder_id = holder; r.subject_id = "Bob";
    r.predicate = "responsible_for"; r.object_value = "auth";
    r.modality = modality; r.confidence = conf;
    r.consolidation_state = "consolidated";
    // 单证据 → evidence_json 一个元素;双证据 → 两个。
    r.evidence_json = (std::string(evidence_count) == "one")
        ? R"([{"engram_id":"e1"}])"
        : R"([{"engram_id":"e1"},{"engram_id":"e2"}])";
    return r;
}
}  // namespace

TEST(ContextPack, ClassifyPrecedence) {
    PackContext ctx;
    ctx.querier = "cog-self";
    ctx.todo_ids.insert("t1");
    ctx.conflict_ids.insert("c1");
    ctx.common_ids.insert("g1");

    EXPECT_EQ(classify(row("t1", "cog-self", "COMMITS", 0.9), ctx), ContextPackLabel::TODO);
    EXPECT_EQ(classify(row("c1", "cog-self", "KNOWS", 0.9), ctx), ContextPackLabel::CONFLICT);
    EXPECT_EQ(classify(row("g1", "cog-self", "KNOWS", 0.9), ctx), ContextPackLabel::COMMON);
    // provenance≠user_input → INFERRED。
    EXPECT_EQ(classify_with_provenance(row("i1", "cog-self", "BELIEVES", 0.7), ctx,
                                       "tom_inferred"),
              ContextPackLabel::INFERRED);
    // 他者单证据 → HEARSAY。
    EXPECT_EQ(classify(row("h1", "Alice", "BELIEVES", 0.6, "one"), ctx),
              ContextPackLabel::HEARSAY);
    // 他者多证据 → BELIEF;自我低置信 BELIEVES → BELIEF。
    EXPECT_EQ(classify(row("b1", "Alice", "KNOWS", 0.9), ctx), ContextPackLabel::BELIEF);
    EXPECT_EQ(classify(row("b2", "cog-self", "BELIEVES", 0.5), ctx), ContextPackLabel::BELIEF);
    // 默认:自我高置信 → FACT。
    EXPECT_EQ(classify(row("f1", "cog-self", "KNOWS", 0.95), ctx), ContextPackLabel::FACT);
}

TEST(ContextPack, RenderShape) {
    PackContext ctx; ctx.querier = "cog-self";
    auto r = row("f1", "cog-self", "KNOWS", 0.95);
    std::vector<PackEntry> entries{
        {ContextPackLabel::FACT, r.id, render_line(r, ContextPackLabel::FACT)}};
    const std::string pack = render_pack(entries, "");
    EXPECT_NE(pack.find("[FACT]"), std::string::npos);
    EXPECT_NE(pack.find("Bob responsible_for auth"), std::string::npos);
    // 拒答包:单 ABSTAIN 行带 reason。
    const std::string ab = render_pack({}, "low_score");
    EXPECT_NE(ab.find("[ABSTAIN]"), std::string::npos);
    EXPECT_NE(ab.find("low_score"), std::string::npos);
}

TEST(ContextPack, RenderPreservesNegativeRelation) {
    auto r = row("preference", "Claudette", "desires", 0.7);
    r.subject_id = "permanence";
    r.predicate = "prefers";
    r.object_value = "permanence";
    r.polarity = "neg";
    EXPECT_EQ(render_line(r, ContextPackLabel::FACT),
              "[FACT] NOT (permanence prefers permanence) (conf 0.70, holder Claudette)");
}

TEST(ContextPack, RenderDistinguishesAllStoredPolarities) {
    auto r = row("claim", "Alice", "believes", 0.6);
    r.polarity = "pos";
    const auto positive = render_line(r, ContextPackLabel::BELIEF);
    EXPECT_EQ(positive, "[BELIEF] Bob responsible_for auth (conf 0.60, holder Alice)");
    r.polarity = "neg";
    EXPECT_EQ(render_line(r, ContextPackLabel::BELIEF),
              "[BELIEF] NOT (Bob responsible_for auth) (conf 0.60, holder Alice)");
    r.polarity = "unknown";
    EXPECT_EQ(render_line(r, ContextPackLabel::BELIEF),
              "[BELIEF] UNKNOWN (Bob responsible_for auth) (conf 0.60, holder Alice)");
    r.polarity.clear();
    EXPECT_EQ(render_line(r, ContextPackLabel::BELIEF), positive);
}

TEST(ContextPack, RenderDoesNotRewriteNegationInsideObject) {
    auto r = row("preference", "Claudette", "desires", 0.7);
    r.subject_id = "street";
    r.predicate = "prefers";
    r.object_value = "no more bulk";
    r.polarity = "neg";
    EXPECT_EQ(render_line(r, ContextPackLabel::FACT),
              "[FACT] NOT (street prefers no more bulk) (conf 0.70, holder Claudette)");
}


TEST(ContextPack, Pr1aProductionEvidenceKeysCountReferencesOncePerEntry) {
    PackContext ctx; ctx.querier = "cog-self";
    auto r = row("evidence", "Alice", "knows", 0.9);
    r.evidence_json = R"([{"engram_ref":"e1","content_hash":"abc","status":"active"},{"engram_ref":"e2","content_hash":"def","status":"active"}])";
    EXPECT_EQ(classify(r, ctx), ContextPackLabel::BELIEF);
    r.evidence_json = R"([{"engram_id":"e1"},{"engram_ref":"e2"}])";
    EXPECT_EQ(classify(r, ctx), ContextPackLabel::BELIEF);
    // Two aliases for a single reference still describe only one evidence item.
    r.evidence_json = R"([{"engram_id":"e1","engram_ref":"e1"}])";
    EXPECT_EQ(classify(r, ctx), ContextPackLabel::HEARSAY);
    // Strings/nested metadata that mention the keys are not evidence items.
    r.evidence_json = R"([{"note":"engram_id engram_id","metadata":{"engram_ref":"nested"}}])";
    EXPECT_EQ(classify(r, ctx), ContextPackLabel::HEARSAY);
    for (const auto* invalid : {"broken", "null", "{}", "[null,3]"}) {
        r.evidence_json = invalid;
        EXPECT_EQ(classify(r, ctx), ContextPackLabel::HEARSAY);
    }
}

}  // namespace starling::retrieval
