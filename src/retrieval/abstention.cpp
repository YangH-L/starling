#include "starling/retrieval/abstention.hpp"

namespace starling::retrieval {

std::string evaluate_abstention(const AbstentionInput& in,
                                const AbstentionConfig& cfg) {
    if (in.frontier_denied)         return "frontier_deny";
    if (in.only_recanted_evidence)  return "only_recanted";
    // 原文证据有自己的门槛，已在 planner 内判过；不再拿语句门槛否决它。
    if (in.source_evidence) { return ""; }
    if (in.unresolved_conflict) {
        return "conflict_unresolved";
    }
    if (!in.any_candidates || in.max_score < cfg.tau_recall) return "low_score";
    return "";
}

}  // namespace starling::retrieval
