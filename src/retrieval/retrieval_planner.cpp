#include "starling/retrieval/retrieval_planner.hpp"
#include "starling/retrieval/claim_evidence.hpp"

#include <algorithm>
#include <ranges>
#include <chrono>
#include <cstdio>
#include <limits>
#include <set>
#include <stdexcept>
#include <unordered_map>
#include <unordered_set>

#include <sqlite3.h>

#include "starling/bus/bus_event.hpp"
#include "starling/bus/outbox_writer.hpp"
#include "starling/cognizer/knowledge_frontier.hpp"
#include "starling/persistence/connection.hpp"
#include "starling/persistence/sqlite_handles.hpp"
#include "starling/persistence/sqlite_helpers.hpp"
#include "starling/tom/common_ground.hpp"
#include "starling/vector/source_vector_index.hpp"
#include "starling/extractor/claim_contract.hpp"

#include <sstream>

namespace starling::retrieval {

using persistence::StmtHandle;
using persistence::detail::bind_sv;
using persistence::detail::make_sqlite_error;

namespace {

// 候选行 + planner 需要、StatementRow 不含的三列。
struct FetchedRow {
    StatementRow row;
    double salience = 0.0;
    double activation = 0.0;
    std::string provenance;
    double base = 1.0;   // 结构化路径=1.0;语义路径=cosine
};

constexpr const char* kSelectCols =
    "SELECT id, tenant_id, holder_id, holder_perspective, subject_kind, "
    "subject_id, predicate, object_kind, object_value, canonical_object_hash, "
    "modality, polarity, confidence, observed_at, valid_from, valid_to, "
    "consolidation_state, review_status, evidence_json, affect_json, "
    "salience, activation, provenance, semantic_claim_json, source_spans_json, nesting_depth FROM statements ";

FetchedRow read_row(sqlite3_stmt* st) {
    auto txt = [&](int i) {
        const auto* p = sqlite3_column_text(st, i);
        // NOLINTNEXTLINE(cppcoreguidelines-pro-type-reinterpret-cast)
        return p != nullptr ? std::string(reinterpret_cast<const char*>(p)) : std::string();
    };
    FetchedRow f;
    auto& r = f.row;
    r.id = txt(0); r.tenant_id = txt(1); r.holder_id = txt(2);
    r.holder_perspective = txt(3); r.subject_kind = txt(4); r.subject_id = txt(5);
    r.predicate = txt(6); r.object_kind = txt(7); r.object_value = txt(8);
    r.canonical_object_hash = txt(9); r.modality = txt(10); r.polarity = txt(11);
    r.confidence = sqlite3_column_double(st, 12);
    r.observed_at = txt(13); r.valid_from = txt(14); r.valid_to = txt(15);
    r.consolidation_state = txt(16); r.review_status = txt(17);
    r.evidence_json = txt(18); r.affect_json = txt(19);
    f.salience = sqlite3_column_double(st, 20);
    f.activation = sqlite3_column_double(st, 21);
    f.provenance = txt(22);
    r.provenance = f.provenance;
    r.semantic_claim_json = txt(23); r.source_spans_json = txt(24);
    r.nesting_depth = sqlite3_column_int(st, 25);
    return f;
}

// Stable visibility and [valid_from, valid_to) bounds are shared by every path.
// SQL NULL and empty strings both represent an unbounded endpoint.
constexpr const char* kStableTail =
    " AND consolidation_state IN ('consolidated','archived')"
    " AND review_status NOT IN ('rejected','pending_review')"
    " AND (valid_from IS NULL OR valid_from = '' OR valid_from <= ?9)"
    " AND (valid_to   IS NULL OR valid_to   = '' OR valid_to   >  ?9) ";

void push_filter(RetrievalScopeStep& step, std::string name, std::string value) {
    step.filters.emplace_back(std::move(name), std::move(value));
}



// ── 左脑来源 lane（形态 S §4.2-§4.5）────────────────────────────────────────
//
// 与语句腿完全独立的取回-融合-渲染路径：原文没有 holder/predicate 语义，
// 也不经过 claim evidence 复核或 affect rerank。两路各自排名后用 RRF 融合。

constexpr int kRrfK = 60;   // RRF 常数；用名次，不要求两路分数同分布

// 从 payload 里取出该 clause 的原文；取不到返回 false（索引与 payload 错配）。
bool unit_text_from_payload(std::string_view payload, std::string_view clause,
                            std::string* text_out) {
    try {
        const auto units = nlohmann::json::parse(
            extractor::claim_source_units(payload));
        for (const auto& unit : units) {
            if (unit.value("clause_id", std::string()) != clause) {
                continue;
            }
            // v3 把说话内容放在 utterance；v1/v2 的 text 是整行。
            std::string text = unit.value("utterance", std::string());
            if (text.empty()) {
                text = unit.value("text", std::string());
            }
            *text_out = std::move(text);
            return true;
        }
    } catch (const std::exception&) {
        return false;
    }
    return false;
}

// 原文渲染：单行、JSON 转义，避免多行原文伪造出新的标签行。
std::string render_source_line(const SourceEntryOut& entry) {
    std::ostringstream out_stream;
    out_stream << "[SOURCE] ";
    if (entry.provenance != "ingest_clock" && !entry.observed_at.empty()) {
        out_stream << "[" << entry.observed_at << "] ";
    }
    if (!entry.speaker.empty()) {
        out_stream << entry.speaker << ": ";
    }
    // JSON 转义把换行/引号等序列化为单行，续行不会产生标签前缀。
    out_stream << nlohmann::json(entry.text).dump();
    return out_stream.str();
}



}  // namespace

void validate_source_lane(const SourceLaneDeps& deps) {
    if (deps.index == nullptr) {
        throw std::invalid_argument("source lane: index is required");
    }
    if (deps.embedder == nullptr) {
        throw std::invalid_argument("source lane: embedder is required");
    }
    if (deps.dim <= 0 || deps.model.empty()) {
        throw std::invalid_argument("source lane: dim/model must be set");
    }
    // §4.4 硬约束：tau_source 未经标定不得开启 lane。
    if (!(deps.tau_source > 0.0)) {
        throw std::invalid_argument(
            "source lane: tau_source must be calibrated (> 0) before enabling");
    }
    if (deps.k_max <= 0 || deps.chunk_byte_budget <= 0) {
        throw std::invalid_argument("source lane: invalid budget");
    }
}

PlannerResult RetrievalPlanner::run(const PlannerQuery& q) {
    if (q.tenant_id.empty() || q.querier.empty() || q.as_of_iso8601.empty()
        || q.query_id.empty()) {
        throw std::invalid_argument(
            "RetrievalPlanner: tenant_id/querier/as_of/query_id are required");
    }
    if(q.temporal_evidence && q.temporal_evidence->tenant_id!=q.tenant_id) {
        throw std::invalid_argument("RetrievalPlanner: temporal evidence tenant must match query tenant");
}
    if(q.temporal_evidence && (q.k<=0 || q.k>std::numeric_limits<int>::max()/10)) {
        throw std::invalid_argument("RetrievalPlanner: invalid temporal candidate budget");
}
    auto& conn = adapter_.connection();
    sqlite3* db = conn.raw();

    PlannerResult out;
    std::optional<TemporalEvidenceView> temporal_view;
    auto& rc = out.receipt;
    rc.trace_id = q.trace_id; rc.query_id = q.query_id;
    rc.querier = q.querier;
    rc.perspective = q.perspective.empty() ? q.querier : q.perspective;
    rc.intent_name = to_string(q.intent);
    rc.runtime_health = q.runtime_health;
    rc.scope_plan.plan_id = q.query_id;
    rc.scope_plan.mode =
        (q.intent == QueryIntent::ABSTAIN_CHECK) ? "exhaustive" : "progressive";
    const std::string perspective = rc.perspective;

    // ── 1. parse:意图显式传入;归一结构化提示。──────────────────────────
    rc.plan_steps.push_back({"parse",
        std::string("intent=") + rc.intent_name +
        (q.subject_id.empty() ? "" : " subject=" + q.subject_id) +
        (q.predicate.empty() ? "" : " predicate=" + q.predicate)});

    // ── 2. mask:perspective ≠ querier 时取 KnowledgeFrontier 可见集。────
    const bool need_mask = perspective != q.querier;
    std::unordered_set<std::string> visible;
    if (need_mask) {
        cognizer::KnowledgeFrontier frontier(adapter_);
        visible = frontier.visible_engrams_at(q.tenant_id, perspective,
                                              q.as_of_iso8601);
    }
    // 左脑 lane 的感知过滤**无条件**生效（§3.2）：原话不按 holder 归属，
    // 自查时若不过滤，agent 就能召回它从未听到的话。这是与右脑 planner 遮罩
    // （只在 perspective≠querier 时套）的关键差异。
    std::unordered_set<std::string> source_visible;
    if (q.source_lane.has_value()) {
        validate_source_lane(*q.source_lane);
        cognizer::KnowledgeFrontier frontier(adapter_);
        source_visible = frontier.visible_engrams_at(q.tenant_id, perspective,
                                                     q.as_of_iso8601);
        rc.frontier_visible_engrams = static_cast<std::int64_t>(source_visible.size());
    }
    rc.plan_steps.push_back({"mask",
        need_mask ? "frontier visible_engrams=" + std::to_string(visible.size())
                  : "perspective==querier, no mask"});

    // ── ground 集预查(COMMON_GROUND intent 的 fetch 也要用,提前到这里;
    //    plan_steps 的 "ground" 行仍按管线位序在 fuse 之后写入)。──────────
    PackContext pctx;
    pctx.querier = q.querier;
    const std::string cg_other = q.target.empty() ? perspective : q.target;
    for (const auto& e : tom::common_ground::query(adapter_, q.querier, cg_other,
                                                   q.tenant_id, q.as_of_iso8601)) {
        if (e.status == "grounded") pctx.common_ids.insert(e.statement_id);
        if (e.status == "recanted") pctx.recanted_ids.insert(e.statement_id);
    }

    // ── 3. plan:Intent→Path。────────────────────────────────────────────
    auto add_step = [&](std::string scope, std::string holder,
                        int max_candidates) {
        RetrievalScopeStep step;
        step.scope = std::move(scope);
        step.holder_scope = std::move(holder);
        step.max_candidates = max_candidates;
        push_filter(step, "tenant_id", q.tenant_id);
        push_filter(step, "holder_scope", step.holder_scope);
        push_filter(step, "perspective", perspective);
        rc.scope_plan.steps.push_back(std::move(step));
    };
    // 每个 case 决定 statement_main 的附加 WHERE 与 holder。
    std::string extra_where;
    const bool has_hints = !q.subject_id.empty() || !q.predicate.empty();
    switch (q.intent) {
        case QueryIntent::FACT_LOOKUP:
            // 无结构化提示时 statement_main 没有选择性(等于按 holder 全量),
            // 主路改语义;有提示才结构化先行(progressive 低成本优先)。
            if (has_hints) { add_step("statement_main", q.querier, q.k * 5);
}
            add_step("semantic_index", q.querier, q.k * 5);
            break;
        case QueryIntent::BELIEF_OF_OTHER:
            add_step("statement_main", q.target, q.k * 5);
            break;
        case QueryIntent::HISTORY:
            add_step("statement_main", q.querier, q.k * 10);
            add_step("graph_index", q.querier, q.k * 5);   // supersedes 链
            break;
        case QueryIntent::META_BELIEF:
            extra_where = " AND nesting_depth >= 1 ";
            add_step("statement_main", q.querier, q.k * 5);
            break;
        case QueryIntent::COMMITMENT_DUE:
            extra_where =
                " AND id IN (SELECT stmt_id FROM commitments WHERE tenant_id = ?1"
                "            AND state IN ('ACTIVE','created')) ";
            add_step("statement_main", q.querier, q.k * 5);
            break;
        case QueryIntent::PREFERENCE:
            extra_where = " AND predicate = 'prefers' ";
            add_step("statement_main",
                     q.target.empty() ? q.querier : q.target, q.k * 5);
            break;
        case QueryIntent::NORM_LOOKUP:
            extra_where = " AND predicate IN ('forbids','requires') ";
            add_step("statement_main", q.querier, q.k * 5);
            break;
        case QueryIntent::COMMON_GROUND:
            add_step("container_view", q.querier,
                     static_cast<int>(pctx.common_ids.size()));
            break;
        case QueryIntent::ABSTAIN_CHECK:
            // exhaustive:有提示带结构化路,语义必跑(不早停)。
            if (has_hints) { add_step("statement_main", q.querier, q.k * 5);
}
            add_step("semantic_index", q.querier, q.k * 5);
            break;
    }
    // 混合过滤拒绝(spec §filter 混合形式拒绝规则)。
    if (!q.global_holder_filter.empty()) {
        for (const auto& step : rc.scope_plan.steps) {
            if (step.holder_scope != q.global_holder_filter) {
                rc.abstention_reason = "invalid_scope_filter_mix";
                rc.sufficiency_status = Sufficiency::ABSTAINED;
                rc.plan_steps.push_back({"plan", "rejected: scope filter mix"});
                out.abstained = true;
                out.context_pack = render_pack({}, rc.abstention_reason);
                return out;
            }
        }
    }
    rc.plan_steps.push_back({"plan",
        "steps=" + std::to_string(rc.scope_plan.steps.size()) +
        " mode=" + rc.scope_plan.mode});

    // ── 4. fetch:按 plan 逐 scope;progressive 早停。─────────────────────
    std::vector<FetchedRow> fetched;
    auto fetch_statement_main = [&](const RetrievalScopeStep& step) {
        std::string sql = std::string(kSelectCols) +
            "WHERE tenant_id = ?1 AND holder_id = ?2 ";
        if (!q.subject_id.empty()) { sql += " AND subject_id = ?3 ";
}
        if (!q.predicate.empty()) {  sql += " AND predicate  = ?4 ";
}
        sql += extra_where;
        sql += kStableTail;
        sql += (q.intent == QueryIntent::HISTORY)
                   ? " ORDER BY observed_at ASC LIMIT ?5"
                   : " ORDER BY observed_at DESC LIMIT ?5";
        sqlite3_stmt* raw = nullptr;
        if (sqlite3_prepare_v2(db, sql.c_str(), -1, &raw, nullptr) != SQLITE_OK) {
            throw make_sqlite_error(db, "planner: statement_main prepare");
}
        StmtHandle h(raw);
        bind_sv(h.get(), 1, q.tenant_id);
        bind_sv(h.get(), 2, step.holder_scope);
        if (!q.subject_id.empty()) { bind_sv(h.get(), 3, q.subject_id);
}
        if (!q.predicate.empty()) {  bind_sv(h.get(), 4, q.predicate);
}
        sqlite3_bind_int(h.get(), 5, step.max_candidates);
        bind_sv(h.get(), 9, q.as_of_iso8601);
        while (sqlite3_step(h.get()) == SQLITE_ROW) { fetched.push_back(read_row(h.get()));
}
    };
    auto fetch_by_id = [&](const std::string& id, const SemanticScored* semantic_row=nullptr) {
        sqlite3_stmt* raw = nullptr;
        const std::string sql = std::string(kSelectCols) +
            "WHERE id = ?1 AND tenant_id = ?2" +
            (q.intent == QueryIntent::HISTORY
                ? " AND consolidation_state IN ('consolidated','archived')"
                  " AND review_status NOT IN ('rejected','pending_review')"
                  " AND (valid_from IS NULL OR valid_from = '' OR valid_from <= ?9) "
                : kStableTail);
        if (sqlite3_prepare_v2(db, sql.c_str(), -1, &raw, nullptr) != SQLITE_OK) {
            return;
}
        StmtHandle h(raw);
        bind_sv(h.get(), 1, id);
        bind_sv(h.get(), 2, q.tenant_id);
        bind_sv(h.get(), 9, q.as_of_iso8601);
        if (sqlite3_step(h.get()) == SQLITE_ROW) {
            auto f=read_row(h.get());
            if (semantic_row != nullptr) {
                // Keep the semantic DTO and cosine score, but materialize only
                // after the same visibility/time query used by structured paths.
                f.row=semantic_row->row;
                f.base=semantic_row->score;
            }
            fetched.push_back(std::move(f));
        }
    };
    auto fetch_semantic = [&](const RetrievalScopeStep& step) {
        if (q.text.empty()) {
            rc.skipped_scopes.push_back({"semantic_index", "empty_query_text",
                                         rc.scope_plan.stop_policy});
            return;
        }
        SemanticRetrieverParams sp;
        sp.tenant_id = q.tenant_id; sp.holder_id = step.holder_scope;
        sp.query_text = q.text; sp.k = step.max_candidates;
        sp.trace_id = q.trace_id; sp.query_id = q.query_id;
        const auto sr = semantic_.vector_recall(conn, sp);
        for (const auto& [identity, reason] : sr.receipt.claim_exclusions)
            record_claim_exclusion(rc, identity.first, identity.second, reason);
        if (sr.degraded) {
            rc.degraded_paths.push_back({"semantic_index", "embedder_unavailable",
                                         "statement_main_only"});
}
        for (const auto& s : sr.rows) fetch_by_id(s.row.id,&s);
    };
    auto fetch_graph_supersedes = [&]() {
        // HISTORY 辅路:从已取行沿 supersedes 边补链上行。
        std::unordered_set<std::string> have;
        for (const auto& f : fetched) have.insert(f.row.id);
        std::vector<std::string> seeds(have.begin(), have.end());
        // Worklist traversal: newly materialized rows may have older ancestors.
        // have also prevents cycles and duplicate fetches.
        for (std::size_t next = 0; next < seeds.size(); ++next) {
            const std::string id = seeds[next];
            sqlite3_stmt* raw = nullptr;
            if (sqlite3_prepare_v2(db,
                "SELECT dst_id FROM statement_edges WHERE src_id=?1 AND "
                "tenant_id=?2 AND edge_kind='supersedes' "
                "UNION SELECT src_id FROM statement_edges WHERE dst_id=?1 AND "
                "tenant_id=?2 AND edge_kind='supersedes'",
                -1, &raw, nullptr) != SQLITE_OK) continue;
            StmtHandle h(raw);
            bind_sv(h.get(), 1, id); bind_sv(h.get(), 2, q.tenant_id);
            std::vector<std::string> others;
            while (sqlite3_step(h.get()) == SQLITE_ROW) {
                const auto* p = sqlite3_column_text(h.get(), 0);
                // NOLINTNEXTLINE(cppcoreguidelines-pro-type-reinterpret-cast)
                if (p != nullptr) { others.emplace_back(reinterpret_cast<const char*>(p)); }
            }
            for (const auto& other : others) {
                if (have.contains(other)) { continue; }
                have.insert(other);
                const auto before = fetched.size();
                fetch_by_id(other);
                if (fetched.size() > before) { seeds.push_back(other); }
            }
        }
    };
    auto fetch_container_view = [&]() {
        for (const auto& id : pctx.common_ids) fetch_by_id(id);
    };

    for (const auto& step : rc.scope_plan.steps) {
        const bool progressive = rc.scope_plan.mode == "progressive";
        if (progressive && static_cast<int>(fetched.size()) >= q.k
            && step.scope == "semantic_index") {
            rc.skipped_scopes.push_back({step.scope, "sufficient_from_prior_scope",
                                         rc.scope_plan.stop_policy});
            rc.stop_reason = "after_first_sufficient";
            continue;
        }
        if (step.scope == "statement_main")       fetch_statement_main(step);
        else if (step.scope == "semantic_index")  fetch_semantic(step);
        else if (step.scope == "graph_index")     fetch_graph_supersedes();
        else if (step.scope == "container_view")  fetch_container_view();
        rc.scopes_searched.push_back(step.scope);
    }
    rc.candidate_counts.fetched = static_cast<std::int64_t>(fetched.size()) + rc.candidate_counts.dropped_by_claim_evidence;
    rc.plan_steps.push_back({"fetch",
        "fetched=" + std::to_string(fetched.size())});

    // ── 4b. 左脑取回：查询嵌入 + 感知过滤 + 迭代回填 ──────────────────────
    // 全部过滤在 SQL 内下推、排序之前（§4.2）；这里只做 payload 交叉校验
    // 与数量控制。
    std::vector<SourceEntryOut> source_hits;
    if (q.source_lane.has_value()) {
        const auto& deps = *q.source_lane;
        vector::SourceSearchScope sscope;
        sscope.tenant_id = q.tenant_id;
        sscope.visible_engrams.insert(source_visible.begin(), source_visible.end());
        sscope.as_of = q.as_of_iso8601;
        sscope.include_unknown_time = false;   // §4.2：无时间的 unit 不进召回

        if (!q.text.empty() && !sscope.visible_engrams.empty()) {
            try {
                const embedding::EmbeddingResult query_vec =
                    deps.embedder->embed(std::string_view(q.text));
                if (query_vec.dim != deps.dim || query_vec.model != deps.model) {
                    // 查询向量与索引不是同一模型：静默比较会得到无意义的分数。
                    rc.source_degraded_reason = "embedder_mismatch";
                } else {
                    // 迭代回填：批量倍增，直到凑够 k 或遍历完符合条件集合。
                    // 左脑用自己的 k_source（0 表示沿用 q.k，保持旧行为）。
                    const int target = deps.k_source > 0 ? deps.k_source : q.k;
                    int want = target;
                    int fetched_count = 0;
                    while (want <= deps.k_max) {
                        const auto batch = deps.index->search(
                            conn, query_vec.vector, want, sscope);
                        fetched_count = static_cast<int>(batch.size());
                        source_hits.clear();
                        for (const auto& hit : batch) {
                            SourceEntryOut entry;
                            entry.engram_ref = hit.engram_ref;
                            entry.clause_id = hit.clause_id;
                            entry.speaker = hit.speaker;
                            entry.session_id = hit.session_id;
                            entry.score = hit.score;
                            source_hits.push_back(std::move(entry));
                        }
                        if (fetched_count >= target) { break; }
                        if (fetched_count < want) { break; }  // 已遍历完
                        want *= 2;
                    }
                    if (fetched_count >= deps.k_max && fetched_count >= target) {
                        rc.source_truncated = 1;
                    }
                }
            } catch (const std::exception& e) {
                rc.source_degraded_reason = std::string("source_lane_failed: ") + e.what();
                source_hits.clear();
            }
        } else if (!q.text.empty() && sscope.visible_engrams.empty()) {
            rc.source_degraded_reason = "no_visible_engrams";
        }

        rc.source_candidates = static_cast<std::int64_t>(source_hits.size());

        // payload 交叉校验（数据完整性，不是授权）：clause 必须仍在该 engram
        // 的 payload 里、正文非空、且时间与 provenance 取权威元数据。
        std::vector<SourceEntryOut> verified;
        verified.reserve(source_hits.size());
        std::string loaded_engram;
        std::string loaded_payload;
        for (auto& entry : source_hits) {
            if (entry.engram_ref != loaded_engram) {
                loaded_engram = entry.engram_ref;
                loaded_payload.clear();
                sqlite3_stmt* raw = nullptr;
                if (sqlite3_prepare_v2(db,
                        "SELECT payload_inline, created_at FROM engrams"
                        " WHERE id = ?1 AND tenant_id = ?2 AND erased_at IS NULL",
                        -1, &raw, nullptr) == SQLITE_OK) {
                    StmtHandle handle(raw);
                    bind_sv(handle.get(), 1, entry.engram_ref);
                    bind_sv(handle.get(), 2, q.tenant_id);
                    if (sqlite3_step(handle.get()) == SQLITE_ROW) {
                        const void* bytes = sqlite3_column_blob(handle.get(), 0);
                        const int length = sqlite3_column_bytes(handle.get(), 0);
                        if (bytes != nullptr && length > 0) {
                            loaded_payload.assign(static_cast<const char*>(bytes),
                                                  static_cast<std::size_t>(length));
                        }
                    }
                }
            }
            if (loaded_payload.empty()) {
                ++rc.source_rejected;
                continue;
            }
            std::string text;
            if (!unit_text_from_payload(loaded_payload, entry.clause_id, &text) || text.empty()) {
                ++rc.source_rejected;
                continue;
            }
            entry.text = std::move(text);

            // 时间与 provenance 以权威元数据为准（索引里没有这两列）。
            sqlite3_stmt* raw = nullptr;
            if (sqlite3_prepare_v2(db,
                    "SELECT observed_at, provenance, session_id, speaker"
                    " FROM source_unit_meta WHERE tenant_id = ?1 AND engram_ref = ?2"
                    "   AND clause_id = ?3", -1, &raw, nullptr) != SQLITE_OK) {
                ++rc.source_rejected;
                continue;
            }
            StmtHandle handle(raw);
            bind_sv(handle.get(), 1, q.tenant_id);
            bind_sv(handle.get(), 2, entry.engram_ref);
            bind_sv(handle.get(), 3, entry.clause_id);
            if (sqlite3_step(handle.get()) != SQLITE_ROW) {
                ++rc.source_rejected;
                continue;
            }
            const auto text_col = [&](int column) {
                const auto* ptr = sqlite3_column_text(handle.get(), column);
                // NOLINTNEXTLINE(cppcoreguidelines-pro-type-reinterpret-cast)
                return ptr != nullptr ? std::string(reinterpret_cast<const char*>(ptr))
                                      : std::string();
            };
            entry.observed_at = text_col(0);
            entry.provenance = text_col(1);
            if (entry.session_id.empty()) { entry.session_id = text_col(2); }
            if (entry.speaker.empty())    { entry.speaker = text_col(3); }
            if (entry.observed_at.empty()) {
                ++rc.source_no_time;   // 无时间不参与召回，也不渲染日期
            }
            verified.push_back(std::move(entry));
        }
        source_hits = std::move(verified);
    }

    // mask 应用:rerank 之前(spec :107 位序硬约束);evidence 任一可见即可见。
    std::int64_t masked = 0;
    const std::size_t before_mask = fetched.size();
    if (need_mask) {
        std::vector<FetchedRow> kept;
        kept.reserve(fetched.size());
        for (auto& f : fetched) {
            bool any_visible = false;
            for (const auto& eng : visible) {
                if (f.row.evidence_json.find(eng) != std::string::npos) {
                    any_visible = true; break;
                }
            }
            if (any_visible) kept.push_back(std::move(f));
            else ++masked;
        }
        fetched = std::move(kept);
    }
    rc.frontier_masked_count = masked;

    // Direct evidence eligibility is checked after tenant/perspective masking,
    // before ranking; malformed certificates cannot become answer context.
    fetched.erase(std::remove_if(fetched.begin(), fetched.end(), [&](const auto& f) {
        const auto reason = claim_evidence_error(conn, f.row);
        if (reason.empty()) { return false;
}
        record_claim_exclusion(rc, f.row.tenant_id, f.row.id, reason);
        return true;
    }), fetched.end());

    // ── 5. fuse:按 id 去重(保最高 base)→ Affect-aware rerank → 截断 k。──
    std::unordered_map<std::string, FetchedRow> dedup;
    for (auto& f : fetched) {
        auto it = dedup.find(f.row.id);
        if (it == dedup.end() || f.base > it->second.base)
            dedup[f.row.id] = std::move(f);
    }
    std::vector<RerankCandidate> cands;
    std::unordered_map<std::string, std::string> provenance_by_id;
    cands.reserve(dedup.size());
    for (auto& [id, f] : dedup) {
        provenance_by_id[id] = f.provenance;
        cands.push_back({std::move(f.row), f.base, f.salience, f.activation});
    }
    QuerierAffectState qa{};
    auto breakdown = rerank(cands, qa, q.as_of_iso8601);
    if(q.temporal_evidence) {
        auto request=*q.temporal_evidence;
        request.limit=std::min(request.limit,q.k);
        std::vector<TemporalEvidenceCandidate> visible_candidates;
        visible_candidates.reserve(cands.size());
        for(std::size_t i=0;i<cands.size();++i) { visible_candidates.push_back({cands[i].row,breakdown[i].final_score});
}
        temporal_view=select_temporal_evidence(visible_candidates,request);
        rc.temporal_evidence_json=temporal_evidence_json(*temporal_view);
        std::set<std::pair<std::string,std::string>> selected;
        for(const auto& ref:temporal_view->selected) selected.emplace(ref.tenant_id,ref.statement_id);
        std::vector<RerankCandidate> kept;
        std::vector<ScoreRow> kept_scores;
        for(std::size_t i=0;i<cands.size();++i) {
            if(selected.contains({cands[i].row.tenant_id,cands[i].row.id})) {
                kept.push_back(std::move(cands[i]));kept_scores.push_back(breakdown[i]);
            }
        }
        cands=std::move(kept);breakdown=std::move(kept_scores);
    }
    if (static_cast<int>(cands.size()) > q.k) {
        cands.resize(static_cast<std::size_t>(q.k));
        breakdown.resize(static_cast<std::size_t>(q.k));
    }
    // ── 5b. 两路 RRF 融合（§4.3）──────────────────────────────────────
    // 语句名次与原文名次各自 1..n；score = Σ 1/(60+rank)。用名次而不是分数，
    // 因为余弦与 final_score 不可直接比较。
    if (!source_hits.empty()) {
        std::ranges::sort(source_hits, [](const SourceEntryOut& lhs, const SourceEntryOut& rhs) {
            if (lhs.score != rhs.score) { return lhs.score > rhs.score; }
            // 同分按 identity 稳定排序，保证同名次结果可复现。
            if (lhs.engram_ref != rhs.engram_ref) { return lhs.engram_ref < rhs.engram_ref; }
            return lhs.clause_id < rhs.clause_id;
        });
        std::int64_t rank = 1;
        for (auto& hit : source_hits) {
            hit.rrf_rank = rank;
            hit.rrf_score = 1.0 / (static_cast<double>(kRrfK) + static_cast<double>(rank));
            ++rank;
        }
        std::int64_t stmt_rank = 1;
        for (auto& scored : breakdown) {
            scored.final_score +=
                1.0 / (static_cast<double>(kRrfK) + static_cast<double>(stmt_rank));
            ++stmt_rank;
        }
    }

    rc.score_breakdown = breakdown;
    rc.plan_steps.push_back({"fuse",
        "deduped=" + std::to_string(dedup.size()) +
        " returned<=" + std::to_string(q.k)});

    // ── 6. ground:承诺/冲突两集补查 + 共识集(已预查)记录。──────────────
    {
        sqlite3_stmt* raw = nullptr;
        if (sqlite3_prepare_v2(db,
            "SELECT stmt_id FROM commitments WHERE tenant_id=?1 AND "
            "state IN ('ACTIVE','created')", -1, &raw, nullptr) == SQLITE_OK) {
            StmtHandle h(raw);
            bind_sv(h.get(), 1, q.tenant_id);
            while (sqlite3_step(h.get()) == SQLITE_ROW) {
                const auto* p = sqlite3_column_text(h.get(), 0);
                // NOLINTNEXTLINE(cppcoreguidelines-pro-type-reinterpret-cast)
                if (p != nullptr) { pctx.todo_ids.insert(reinterpret_cast<const char*>(p));
}
            }
        }
    }
    {
        sqlite3_stmt* raw = nullptr;
        if (sqlite3_prepare_v2(db,
            "SELECT src_id, dst_id FROM statement_edges WHERE tenant_id=?1 AND "
            "edge_kind='conflicts_with'", -1, &raw, nullptr) == SQLITE_OK) {
            StmtHandle h(raw);
            bind_sv(h.get(), 1, q.tenant_id);
            while (sqlite3_step(h.get()) == SQLITE_ROW) {
                const auto* a = sqlite3_column_text(h.get(), 0);
                const auto* dst_text = sqlite3_column_text(h.get(), 1);
                // NOLINTNEXTLINE(cppcoreguidelines-pro-type-reinterpret-cast)
                if (a != nullptr) { pctx.conflict_ids.insert(reinterpret_cast<const char*>(a));
}
                // NOLINTNEXTLINE(cppcoreguidelines-pro-type-reinterpret-cast)
                if (dst_text != nullptr) { pctx.conflict_ids.insert(reinterpret_cast<const char*>(dst_text));
}
            }
        }
    }
    rc.plan_steps.push_back({"ground",
        "common=" + std::to_string(pctx.common_ids.size()) +
        " todo=" + std::to_string(pctx.todo_ids.size()) +
        " conflict=" + std::to_string(pctx.conflict_ids.size()),});

    // ── 7. abstain:四条件。──────────────────────────────────────────────
    // 原文侧的"有证据"判据是分数（§4.4）：取消低分保护会把 rank 1 当成
    // 必然相关。且必须**预算之后**判定——被字节预算截掉的不算证据。
    const double best_source_score = source_hits.empty()
        ? std::numeric_limits<double>::lowest()
        : source_hits.front().score;
    bool renderable_source_evidence = false;
    if (q.source_lane.has_value()) {
        std::size_t used_bytes = 0;
        for (const auto& cand : cands) {
            const auto label = classify_with_provenance(
                cand.row, pctx, provenance_by_id.at(cand.row.id));
            used_bytes += render_line(cand.row, label).size() + 1;
        }
        for (const auto& source : source_hits) {
            const std::size_t cost = render_source_line(source).size() + 1;
            if (source.score >= q.source_lane->tau_source
                && used_bytes + cost <= static_cast<std::size_t>(
                    q.source_lane->chunk_byte_budget)) {
                renderable_source_evidence = true;
                break;
            }
        }
    }
    const bool source_is_evidence = q.source_lane.has_value()
        && !source_hits.empty()
        && best_source_score >= q.source_lane->tau_source
        && renderable_source_evidence;

    AbstentionInput abstain_input;
    abstain_input.any_candidates = !cands.empty() || source_is_evidence;
    // 两路门槛分开：语句分数照旧交给 tau_recall；原文证据单独以 source_evidence
    // 传入。旧实现把两者合成一个 max_score，tau_source < tau_recall 时原文证据
    // 会被语句门槛误杀（阶段 A 冒烟发现）。
    abstain_input.max_score = breakdown.empty() ? 0.0 : breakdown.front().final_score;
    abstain_input.source_evidence = source_is_evidence;
    abstain_input.frontier_denied = need_mask && before_mask > 0 && cands.empty() && !source_is_evidence;
    if (!cands.empty()) {
        bool all_recanted = true;
        for (const auto& c : cands)
            if (!pctx.recanted_ids.contains(c.row.id)) { all_recanted = false; break; }
        abstain_input.only_recanted_evidence = all_recanted;
        abstain_input.unresolved_conflict = pctx.conflict_ids.contains(cands.front().row.id);
    }
    rc.abstention_reason = evaluate_abstention(abstain_input, q.abstention);
    rc.plan_steps.push_back({"abstain",
        rc.abstention_reason.empty() ? "pass" : rc.abstention_reason});

    // ── 输出组装 + sufficiency + projection lag + 中心化 emit。────────────
    if (!rc.abstention_reason.empty()) {
        out.abstained = true;
        out.context_pack = render_pack({}, rc.abstention_reason);
        if(temporal_view) { out.context_pack+="\n"+render_temporal_evidence(*temporal_view);
}
        rc.sufficiency_status = Sufficiency::ABSTAINED;
        rc.candidate_counts.returned = 0;
        return out;
    }
    std::vector<PackEntry> entries;
    for (std::size_t i = 0; i < cands.size(); ++i) {
        auto& cand = cands.at(i);
        const auto label = classify_with_provenance(
            cand.row, pctx, provenance_by_id.at(cand.row.id));
        entries.push_back(PackEntry{.label = label,
                                    .statement_id = cand.row.id,
                                    .line = render_line(cand.row, label),});
        record_claim_link(conn, rc, cand.row);
        out.entries.push_back(PlannerEntryOut{.row = std::move(cand.row),
                                              .score = breakdown.at(i).final_score,
                                              .label = label,});
    }

    // 原文行：与语句行按融合分交错，共用同一个字节预算（§4.3）。预算按整行
    // 截断——半行原文比没有原文更糟。
    if (!source_hits.empty()) {
        struct RenderedSource { SourceEntryOut entry; std::string line; };
        std::vector<RenderedSource> rendered;
        rendered.reserve(source_hits.size());
        for (auto& hit : source_hits) {
            // 先渲染再移动：聚合初始化的求值顺序未定义，写成
            // {std::move(hit), render_source_line(hit)} 会在移动之后才读 hit，
            // 渲染出空行（并让预算判断把整行误当成 0 字节）。
            std::string line = render_source_line(hit);
            rendered.push_back(RenderedSource{.entry = std::move(hit), .line = std::move(line)});
        }
        std::ranges::sort(rendered, [](const RenderedSource& lhs, const RenderedSource& rhs) {
            if (lhs.entry.rrf_score != rhs.entry.rrf_score) {
                return lhs.entry.rrf_score > rhs.entry.rrf_score;
            }
            if (lhs.entry.engram_ref != rhs.entry.engram_ref) {
                return lhs.entry.engram_ref < rhs.entry.engram_ref;
            }
            return lhs.entry.clause_id < rhs.entry.clause_id;
        });
        std::size_t used_bytes = 0;
        for (const auto& entry : entries) { used_bytes += entry.line.size() + 1; }
        for (auto& item : rendered) {
            const std::size_t cost = item.line.size() + 1;
            if (q.source_lane.has_value()
                && used_bytes + cost > static_cast<std::size_t>(q.source_lane->chunk_byte_budget)) {
                ++rc.source_truncated;
                continue;
            }
            used_bytes += cost;
            entries.push_back(PackEntry{.label = ContextPackLabel::SOURCE,
                                        .statement_id = item.entry.engram_ref,
                                        .line = item.line,});
            out.source_entries.push_back(std::move(item.entry));
        }
        rc.source_selected = static_cast<std::int64_t>(out.source_entries.size());
    }
    out.context_pack = render_pack(entries, "");
    if(temporal_view) { out.context_pack+="\n"+render_temporal_evidence(*temporal_view);
}
    rc.candidate_counts.returned = static_cast<std::int64_t>(out.entries.size());
    rc.sufficiency_status = out.entries.empty() ? Sufficiency::MISSING_INFO
                                                : Sufficiency::SUFFICIENT;
    if(temporal_view && !temporal_view->sufficient) { rc.sufficiency_status=Sufficiency::MISSING_INFO;
}
    {   // projection lag:outbox 头 − 最慢 checkpoint(无消费者按 0)。
        sqlite3_stmt* raw = nullptr;
        if (sqlite3_prepare_v2(db,
            "SELECT COALESCE((SELECT MAX(outbox_sequence) FROM bus_events),0) - "
            "COALESCE((SELECT MIN(last_dispatched_sequence) FROM consumer_checkpoints),"
            "(SELECT COALESCE(MAX(outbox_sequence),0) FROM bus_events))",
            -1, &raw, nullptr) == SQLITE_OK) {
            StmtHandle stmt(raw);
            if (sqlite3_step(stmt.get()) == SQLITE_ROW) {
                rc.projection_lag_events = sqlite3_column_int64(stmt.get(), 0);
}
        }
    }
    // fire-and-forget recalled emit(键公式与 basic_retriever 一致;失败仅
    // stderr,不上抛——读副作用契约)。
    try {
        persistence::TransactionGuard tx(conn);
        bus::OutboxWriter w(conn);
        const auto now = std::chrono::system_clock::now();
        const std::string bucket =
            bus::compute_window_bucket("statement.recalled", now);
        for (const auto& e : out.entries) {
            bus::BusEvent ev;
            ev.tenant_id    = q.tenant_id;
            ev.event_type   = "statement.recalled";
            ev.primary_id   = e.row.id;
            ev.aggregate_id = e.row.id;
            ev.payload_json = std::string("{\"statement_id\":\"") + e.row.id +
                "\",\"querier\":\"" + q.querier + "\",\"perspective\":\"" +
                perspective + "\",\"intent\":\"" + rc.intent_name +
                "\",\"query_id\":\"" + q.query_id + "\"}";
            ev.version = "v1";
            ev.idempotency_key = bus::compute_idempotency_key(
                "statement.recalled", e.row.id, e.row.id, q.query_id, bucket);
            try {
                w.append(ev);
                rc.emitted_events.push_back(ev.event_id);
            } catch (const persistence::SqliteError& err) {
                if (err.code() != SQLITE_CONSTRAINT_UNIQUE) throw;  // 2s 窗口去重
            }
        }
        tx.commit();
    } catch (const std::exception& e) {
        std::fprintf(stderr,
            "retrieval_planner: recalled emit failed for query_id=%s: %s\n",
            q.query_id.c_str(), e.what());
    }
    return out;
}

}  // namespace starling::retrieval
