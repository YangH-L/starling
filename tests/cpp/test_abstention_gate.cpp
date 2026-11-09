// Abstention Gate 四条件(13_retrieval.md §Abstention 触发条件)。
// 结构化 reason,绝不模糊;条件优先级:frontier > only_recanted > conflict > low_score。
#include "starling/retrieval/abstention.hpp"

#include <gtest/gtest.h>

namespace starling::retrieval {

TEST(AbstentionGate, FourConditionsAndPriority) {
    AbstentionInput in;
    in.any_candidates = true; in.max_score = 0.9;
    EXPECT_EQ(evaluate_abstention(in), "");                       // 不拒答

    in.max_score = 0.1;
    EXPECT_EQ(evaluate_abstention(in), "low_score");              // τ=0.25 默认

    AbstentionConfig loose; loose.tau_recall = 0.05;
    EXPECT_EQ(evaluate_abstention(in, loose), "");                // 阈值可配

    in.unresolved_conflict = true;
    EXPECT_EQ(evaluate_abstention(in), "conflict_unresolved");    // 优先于 low_score

    in.only_recanted_evidence = true;
    EXPECT_EQ(evaluate_abstention(in), "only_recanted");

    in.frontier_denied = true;
    EXPECT_EQ(evaluate_abstention(in), "frontier_deny");          // 最高优先

    // 无候选 + frontier 没遮蔽过任何东西 → low_score(max_score=0)。
    AbstentionInput empty;
    EXPECT_EQ(evaluate_abstention(empty), "low_score");
}

// 形态 S §4.4：原文证据有自己的门槛（tau_source，由 planner 判定后传入），
// 不得再被语句侧的 tau_recall 否决。两路分数不可比，混在一个 max_score 里
// 会让任何低于 tau_recall 的原文证据都被拒答。
TEST(AbstentionGate, SourceEvidenceIsNotVetoedByStatementThreshold) {
    AbstentionInput in;
    in.source_evidence = true;      // planner 已按 tau_source 判过
    in.any_candidates = true;
    in.max_score = 0.0;             // 没有语句候选
    EXPECT_EQ(evaluate_abstention(in), "");

    // Renderable original evidence keeps conflicts as labels, not a whole-query veto.
    in.unresolved_conflict = true;
    EXPECT_EQ(evaluate_abstention(in), "");
    in.source_evidence = false;
    EXPECT_EQ(evaluate_abstention(in), "conflict_unresolved");

    // Frontier and recantation guards retain their priority even with sources.
    in.source_evidence = true;
    in.only_recanted_evidence = true;
    EXPECT_EQ(evaluate_abstention(in), "only_recanted");
    in.frontier_denied = true;
    EXPECT_EQ(evaluate_abstention(in), "frontier_deny");
}

TEST(AbstentionGate, NoSourceEvidenceKeepsLegacyBehaviour) {
    AbstentionInput in;             // source_evidence 默认 false
    in.any_candidates = true;
    in.max_score = 0.1;
    EXPECT_EQ(evaluate_abstention(in), "low_score");
}

}  // namespace starling::retrieval
