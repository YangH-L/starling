#include "starling/retrieval/context_pack.hpp"
#include "starling/retrieval/claim_evidence.hpp"

#include <sstream>

namespace starling::retrieval {

namespace {
// EvidenceRef array: accept both the production key and the legacy alias.
// Count each reference once; key names in text/metadata are not evidence.
int evidence_count(std::string_view evidence_json) {
    const auto refs = nlohmann::json::parse(evidence_json, nullptr, false);
    if (!refs.is_array()) { return 0; }
    int n = 0;
    for (const auto& ref : refs) {
        if (!ref.is_object()) { continue; }
        const auto has_reference = [&](const char* key) {
            const auto field = ref.find(key);
            return field != ref.end() && field->is_string() &&
                   !field->get_ref<const std::string&>().empty();
        };
        if (has_reference("engram_ref") || has_reference("engram_id")) { ++n; }
    }
    return n;
}
}  // namespace

ContextPackLabel classify_with_provenance(const StatementRow& row,
                                          const PackContext& ctx,
                                          std::string_view provenance) {
    if (static_cast<unsigned int>(ctx.todo_ids.contains(row.id)) != 0U) {     return ContextPackLabel::TODO;
}
    if (static_cast<unsigned int>(ctx.conflict_ids.contains(row.id)) != 0U) { return ContextPackLabel::CONFLICT;
}
    if (static_cast<unsigned int>(ctx.common_ids.contains(row.id)) != 0U) {   return ContextPackLabel::COMMON;
}
    if (!provenance.empty() && provenance != "user_input") {
        return ContextPackLabel::INFERRED;
}
    const bool other_holder = !ctx.querier.empty() && row.holder_id != ctx.querier;
    if (other_holder && evidence_count(row.evidence_json) <= 1) {
        return ContextPackLabel::HEARSAY;
}
    if (other_holder) { return ContextPackLabel::BELIEF;
}
    if ((row.modality == "BELIEVES" || row.modality == "ASSUMES" ||
         row.modality == "DOUBTS") && row.confidence < 0.8) {
        return ContextPackLabel::BELIEF;
}
    return ContextPackLabel::FACT;
}

ContextPackLabel classify(const StatementRow& row, const PackContext& ctx) {
    return classify_with_provenance(row, ctx, "user_input");
}

std::string render_line(const StatementRow& row, ContextPackLabel label) {
    std::ostringstream os;
    os << "[" << to_string(label) << "] ";
    const bool scoped_polarity = row.polarity == "neg" || row.polarity == "unknown";
    if (row.polarity == "neg") { os << "NOT (";
    } else if (row.polarity == "unknown") { os << "UNKNOWN (";
}
    os << row.subject_id << " " << row.predicate << " " << row.object_value;
    if (scoped_polarity) { os << ")";
}
    os.setf(std::ios::fixed); os.precision(2);
    os << " (conf " << row.confidence;
    if (!row.holder_id.empty()) { os << ", holder " << row.holder_id;
}
    os << ")";
    if (!row.semantic_claim_json.empty()) {
        const auto claim = parse_claim_evidence(row);
        if (claim.is_object()) {
            // JSON serialization escapes control characters, keeping each claim
            // in one context line. No raw source text is fetched or rendered.
            os << " {scope " << claim.value("assertion_scope", "UNKNOWN");
            os << ", scope_markers " << claim.value("scope_markers", nlohmann::json::array()).dump();
            os << ", actor " << claim.value("actor", nlohmann::json()).dump();
            if (claim.contains("topic") && claim["topic"].is_string()) {
                os << ", topic " << claim["topic"].dump();
}
            if (claim.contains("source_turn") && claim["source_turn"].is_object()) {
                os << ", source_turn " << claim["source_turn"].dump();
}
            if (claim.contains("attributed_to") && !claim["attributed_to"].is_null()) {
                os << ", attributed_to " << claim["attributed_to"].dump();
}
            const bool fallback = !claim.contains("event_time") || claim["event_time"].is_null();
            os << ", event_time " << (fallback ? "UNKNOWN" : claim["event_time"].dump());
            os << ", source_time " << claim.value("source_time", nlohmann::json()).dump();
            os << ", time_basis " << (fallback ? "source_time_fallback" : "event_time");
            if (claim.contains("predicate_catalog_version") &&
                claim["predicate_catalog_version"].is_string()) {
                os << ", predicate_catalog_version " << claim["predicate_catalog_version"].dump();
}
            if (claim.contains("semantic_family") && claim["semantic_family"].is_string()) {
                os << ", semantic_family " << claim["semantic_family"].dump();
}
            if (claim.contains("time_text") && claim["time_text"].is_string() && !claim["time_text"].get_ref<const std::string&>().empty()) {
                os << ", time_text " << claim["time_text"].dump();
}
            os << ", clause " << claim.value("clause_id", nlohmann::json()).dump();
            os << ", evidence " << claim.value("source_span", nlohmann::json()).dump() << "}";
        }
    }
    return os.str();
}

std::string render_pack(const std::vector<PackEntry>& entries,
                        std::string_view abstention_reason) {
    if (!abstention_reason.empty()) {
        std::string s = "[ABSTAIN] 无可靠记忆,主动拒答(";
        s += abstention_reason; s += ")";
        return s;
    }
    std::ostringstream os;
    for (std::size_t i = 0; i < entries.size(); ++i) {
        if (i != 0U) { os << "\n";
}
        os << entries[i].line;
    }
    return os.str();
}

std::string render_temporal_evidence(const TemporalEvidenceView& view) {
    return "[EVIDENCE_ORDER] " + temporal_evidence_json(view) +
        "\n仅表示可见候选内的来源顺序，不等于事件时间；变化判断须引用父来源并标注推断。";
}

}  // namespace starling::retrieval
