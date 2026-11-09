#include "starling/store/sqlite_statement_store.hpp"

#include <sqlite3.h>

#include "starling/persistence/sqlite_handles.hpp"
#include "starling/persistence/sqlite_helpers.hpp"

namespace starling::store {

using persistence::StmtHandle;
using persistence::detail::bind_sv;
using persistence::detail::make_sqlite_error;

namespace {
// 逐 id 跑一条「UPDATE … WHERE id=? AND tenant_id=? [守卫]」,累加受影响行。
// 三个 batch-记账转换法共用形:bind1=replay_batch_id, bind2=id, bind3=tenant。
int run_per_id(sqlite3* db, const char* sql, const char* op,
               const std::vector<std::string>& ids, std::string_view tenant,
               std::string_view batch) {
    int affected = 0;
    for (const auto& id : ids) {
        sqlite3_stmt* raw = nullptr;
        if (sqlite3_prepare_v2(db, sql, -1, &raw, nullptr) != SQLITE_OK)
            throw make_sqlite_error(db, op);
        StmtHandle h(raw);
        bind_sv(h.get(), 1, batch);
        bind_sv(h.get(), 2, id);
        bind_sv(h.get(), 3, tenant);
        if (sqlite3_step(h.get()) != SQLITE_DONE)
            throw make_sqlite_error(db, op);
        affected += sqlite3_changes(db);
    }
    return affected;
}
}  // namespace

int SqliteStatementStore::mark_consolidated(
    const std::vector<std::string>& ids, std::string_view tenant,
    std::string_view replay_batch_id) {
    // exact 自 src/replay/consolidation_ops.cpp:31(op_compress)。
    return run_per_id(conn_.raw(),
        "UPDATE statements SET consolidation_state='consolidated', "
        "  last_replay_batch_id=?, replay_count=replay_count+1 "
        " WHERE id=? AND tenant_id=? AND consolidation_state='volatile'",
        "StatementStore::mark_consolidated", ids, tenant, replay_batch_id);
}

int SqliteStatementStore::reinforce(
    const std::vector<std::string>& ids, std::string_view tenant,
    std::string_view replay_batch_id) {
    // exact 自 src/replay/consolidation_ops.cpp:57(op_reinforce)。
    return run_per_id(conn_.raw(),
        "UPDATE statements SET access_count=access_count+1, "
        "  consolidation_state='consolidated', "
        "  last_replay_batch_id=?, replay_count=replay_count+1 "
        " WHERE id=? AND tenant_id=?",
        "StatementStore::reinforce", ids, tenant, replay_batch_id);
}

int SqliteStatementStore::bump_replay_count(
    const std::vector<std::string>& ids, std::string_view tenant,
    std::string_view replay_batch_id) {
    // exact 自 src/replay/consolidation_ops.cpp:80(op_abstract)。
    return run_per_id(conn_.raw(),
        "UPDATE statements SET last_replay_batch_id=?, replay_count=replay_count+1 "
        " WHERE id=? AND tenant_id=?",
        "StatementStore::bump_replay_count", ids, tenant, replay_batch_id);
}

int SqliteStatementStore::enter_reconsolidating(std::string_view id,
                                                std::string_view tenant) {
    // exact 自 src/replay/consolidation_ops.cpp:154(op_reconcile)。
    sqlite3* db = conn_.raw();
    const char* sql =
        "UPDATE statements SET consolidation_state='replaying_reconsolidating' "
        "WHERE id=? AND tenant_id=? AND consolidation_state='consolidated'";
    sqlite3_stmt* raw = nullptr;
    if (sqlite3_prepare_v2(db, sql, -1, &raw, nullptr) != SQLITE_OK)
        throw make_sqlite_error(db, "StatementStore::enter_reconsolidating prepare");
    StmtHandle h(raw);
    bind_sv(h.get(), 1, id);
    bind_sv(h.get(), 2, tenant);
    if (sqlite3_step(h.get()) != SQLITE_DONE)
        throw make_sqlite_error(db, "StatementStore::enter_reconsolidating step");
    return sqlite3_changes(db);
}

int SqliteStatementStore::restore_consolidated(std::string_view id,
                                               std::string_view tenant) {
    // exact 自 src/reconsolidation/reconsolidation_engine.cpp:265(兜底)。
    sqlite3* db = conn_.raw();
    const char* sql =
        "UPDATE statements SET consolidation_state='consolidated' "
        "WHERE id=? AND tenant_id=? "
        "AND consolidation_state='replaying_reconsolidating'";
    sqlite3_stmt* raw = nullptr;
    if (sqlite3_prepare_v2(db, sql, -1, &raw, nullptr) != SQLITE_OK)
        throw make_sqlite_error(db, "StatementStore::restore_consolidated prepare");
    StmtHandle h(raw);
    bind_sv(h.get(), 1, id);
    bind_sv(h.get(), 2, tenant);
    if (sqlite3_step(h.get()) != SQLITE_DONE)
        throw make_sqlite_error(db, "StatementStore::restore_consolidated step");
    return sqlite3_changes(db);
}

void SqliteStatementStore::set_consolidation_summary(std::string_view stmt_id,
                                                     std::string_view tenant,
                                                     std::string_view summary) {
    // BEST-EFFORT (see header): never throw — the gist is already committed, so a
    // summary-UPDATE failure must not flip it to "failed" (Phase-1 idempotency
    // would then suppress a re-write, losing the summary forever). On failure the
    // column simply stays NULL (observable). consolidation_summary is not an
    // immutable-field-trigger column.
    sqlite3* db_handle = conn_.raw();
    const char* sql =
        "UPDATE statements SET consolidation_summary=? WHERE id=? AND tenant_id=?";
    sqlite3_stmt* raw = nullptr;
    if (sqlite3_prepare_v2(db_handle, sql, -1, &raw, nullptr) != SQLITE_OK) {
        return;
    }
    StmtHandle handle(raw);
    bind_sv(handle.get(), 1, summary);
    bind_sv(handle.get(), 2, stmt_id);
    bind_sv(handle.get(), 3, tenant);
    sqlite3_step(handle.get());  // result intentionally ignored — best-effort
}

int SqliteStatementStore::promote_gist_to_consolidated(std::string_view stmt_id,
                                                       std::string_view tenant,
                                                       std::string_view now_iso) {
    // STATE-GUARDED flip (see header): only a still-volatile consolidation_abstract
    // gist is promoted, so a gist that pipeline arbitration already archived on a
    // conflict is left untouched (conflict ⇒ no auto-consolidate). Load-bearing
    // (unlike set_consolidation_summary) — throws on SQL error.
    sqlite3* db_handle = conn_.raw();
    const char* sql =
        "UPDATE statements SET consolidation_state='consolidated', "
        "review_status='approved', updated_at=? "
        "WHERE id=? AND tenant_id=? AND provenance='consolidation_abstract' "
        "  AND consolidation_state='volatile'";
    sqlite3_stmt* raw = nullptr;
    if (sqlite3_prepare_v2(db_handle, sql, -1, &raw, nullptr) != SQLITE_OK) {
        throw make_sqlite_error(db_handle, "promote_gist_to_consolidated: prepare");
    }
    StmtHandle handle(raw);
    bind_sv(handle.get(), 1, now_iso);
    bind_sv(handle.get(), 2, stmt_id);
    bind_sv(handle.get(), 3, tenant);
    if (sqlite3_step(handle.get()) != SQLITE_DONE) {
        throw make_sqlite_error(db_handle, "promote_gist_to_consolidated: step");
    }
    return sqlite3_changes(db_handle);
}

int SqliteStatementStore::force_consolidate_pending_review() {
    // exact 自 src/replay/replay_scheduler.cpp:289(enforce_oscillation_guard)。
    // 跨租户 bulk(无 tenant 过滤)——保真现行为。
    sqlite3* db = conn_.raw();
    // #38-C: must stay in lockstep with enforce_oscillation_guard's SELECT —
    // exclude consolidation_abstract gists so an ungated gist is never
    // force-consolidated outside Phase-4 gating.
    const char* sql =
        "UPDATE statements "
        "SET consolidation_state='consolidated', review_status='pending_review' "
        "WHERE replay_count >= 5 "
        "  AND consolidation_state IN ('volatile','replaying_consolidating') "
        "  AND provenance != 'consolidation_abstract'";
    sqlite3_stmt* raw = nullptr;
    if (sqlite3_prepare_v2(db, sql, -1, &raw, nullptr) != SQLITE_OK)
        throw make_sqlite_error(db, "StatementStore::force_consolidate prepare");
    StmtHandle h(raw);
    if (sqlite3_step(h.get()) != SQLITE_DONE)
        throw make_sqlite_error(db, "StatementStore::force_consolidate step");
    return sqlite3_changes(db);
}

int SqliteStatementStore::archive(const std::vector<std::string>& ids,
                                  std::string_view tenant,
                                  std::string_view from_state,
                                  std::optional<std::string> updated_at) {
    // 参数化归档:守卫 from_state;updated_at 有值时刷新(supersede bus.cpp:308 /
    // arbitration:452),nullopt 不动 updated_at(decay consolidation_ops.cpp:135 /
    // TTL replay_scheduler.cpp:379)——保真各源行为差异。
    sqlite3* db = conn_.raw();
    const std::string sql = updated_at
        ? "UPDATE statements SET consolidation_state='archived', updated_at=?4 "
          "WHERE id=?1 AND tenant_id=?2 AND consolidation_state=?3"
        : "UPDATE statements SET consolidation_state='archived' "
          "WHERE id=?1 AND tenant_id=?2 AND consolidation_state=?3";
    int affected = 0;
    for (const auto& id : ids) {
        sqlite3_stmt* raw = nullptr;
        if (sqlite3_prepare_v2(db, sql.c_str(), -1, &raw, nullptr) != SQLITE_OK)
            throw make_sqlite_error(db, "StatementStore::archive prepare");
        StmtHandle h(raw);
        bind_sv(h.get(), 1, id);
        bind_sv(h.get(), 2, tenant);
        bind_sv(h.get(), 3, from_state);
        if (updated_at) bind_sv(h.get(), 4, *updated_at);
        if (sqlite3_step(h.get()) != SQLITE_DONE)
            throw make_sqlite_error(db, "StatementStore::archive step");
        affected += sqlite3_changes(db);
    }
    return affected;
}

void SqliteStatementStore::apply_mild_correction(
    std::string_view id, std::string_view tenant, double confidence,
    std::string_view history_json, std::string_view updated_at) {
    // exact 自 src/bus/bus.cpp:264(mild-correction)。provenance 不动。
    sqlite3* db = conn_.raw();
    const char* sql =
        "UPDATE statements "
        "SET confidence = ?, confidence_history_json = ?, updated_at = ? "
        "WHERE id = ? AND tenant_id = ?";
    sqlite3_stmt* raw = nullptr;
    if (sqlite3_prepare_v2(db, sql, -1, &raw, nullptr) != SQLITE_OK)
        throw make_sqlite_error(db, "StatementStore::apply_mild_correction prepare");
    StmtHandle h(raw);
    sqlite3_bind_double(h.get(), 1, confidence);
    bind_sv(h.get(), 2, history_json);
    bind_sv(h.get(), 3, updated_at);
    bind_sv(h.get(), 4, id);
    bind_sv(h.get(), 5, tenant);
    if (sqlite3_step(h.get()) != SQLITE_DONE)
        throw make_sqlite_error(db, "StatementStore::apply_mild_correction step");
}

void SqliteStatementStore::apply_mild_contradict(
    std::string_view id, std::string_view tenant, double confidence,
    std::string_view history_json) {
    // exact 自 src/reconsolidation/arbitration.cpp:284(apply_mild_contradict)。
    // 改 confidence+history+state='consolidated';不动 updated_at/provenance。
    sqlite3* db = conn_.raw();
    const char* sql =
        "UPDATE statements SET confidence=?, confidence_history_json=?, "
        "consolidation_state='consolidated' "
        "WHERE id=? AND tenant_id=?";
    sqlite3_stmt* raw = nullptr;
    if (sqlite3_prepare_v2(db, sql, -1, &raw, nullptr) != SQLITE_OK)
        throw make_sqlite_error(db, "StatementStore::apply_mild_contradict prepare");
    StmtHandle h(raw);
    sqlite3_bind_double(h.get(), 1, confidence);
    bind_sv(h.get(), 2, history_json);
    bind_sv(h.get(), 3, id);
    bind_sv(h.get(), 4, tenant);
    if (sqlite3_step(h.get()) != SQLITE_DONE)
        throw make_sqlite_error(db, "StatementStore::apply_mild_contradict step");
}

int SqliteStatementStore::archive_nonterminal(
    std::string_view id, std::string_view tenant, std::string_view updated_at) {
    // exact 自 src/reconsolidation/arbitration.cpp:452(severe-archive)。
    sqlite3* db = conn_.raw();
    const char* sql =
        "UPDATE statements SET consolidation_state='archived', updated_at=? "
        "WHERE id=? AND tenant_id=? "
        "  AND consolidation_state NOT IN ('archived','forgotten')";
    sqlite3_stmt* raw = nullptr;
    if (sqlite3_prepare_v2(db, sql, -1, &raw, nullptr) != SQLITE_OK)
        throw make_sqlite_error(db, "StatementStore::archive_nonterminal prepare");
    StmtHandle h(raw);
    bind_sv(h.get(), 1, updated_at);
    bind_sv(h.get(), 2, id);
    bind_sv(h.get(), 3, tenant);
    if (sqlite3_step(h.get()) != SQLITE_DONE)
        throw make_sqlite_error(db, "StatementStore::archive_nonterminal step");
    return sqlite3_changes(db);
}

int SqliteStatementStore::forget(std::string_view id, std::string_view tenant,
                                 std::string_view updated_at) {
    // P3.b2:→ forgotten(逻辑删除终态),幂等守卫已 forgotten 不动。
    sqlite3* db = conn_.raw();
    const char* sql =
        "UPDATE statements SET consolidation_state='forgotten', updated_at=? "
        "WHERE id=? AND tenant_id=? "
        "  AND consolidation_state != 'forgotten'";
    sqlite3_stmt* raw = nullptr;
    if (sqlite3_prepare_v2(db, sql, -1, &raw, nullptr) != SQLITE_OK)
        throw make_sqlite_error(db, "StatementStore::forget prepare");
    StmtHandle h(raw);
    bind_sv(h.get(), 1, updated_at);
    bind_sv(h.get(), 2, id);
    bind_sv(h.get(), 3, tenant);
    if (sqlite3_step(h.get()) != SQLITE_DONE)
        throw make_sqlite_error(db, "StatementStore::forget step");
    return sqlite3_changes(db);
}

int SqliteStatementStore::approve_review(std::string_view id, std::string_view tenant,
                                         std::string_view updated_at) {
    // 片 6 干预集:人工审批 review_requested → approved。守卫:仅 review_requested 转,
    // 故非 review_requested 行(已 approved / pending_review / rejected 等)0-change no-op,
    // 重复 approve 幂等。tenant-scoped。reject 不在此(=forget,→forgotten 终态)。
    sqlite3* db = conn_.raw();
    const char* sql =
        "UPDATE statements SET review_status='approved', updated_at=? "
        "WHERE id=? AND tenant_id=? "
        "  AND review_status='review_requested'";
    sqlite3_stmt* raw = nullptr;
    if (sqlite3_prepare_v2(db, sql, -1, &raw, nullptr) != SQLITE_OK)
        throw make_sqlite_error(db, "StatementStore::approve_review prepare");
    StmtHandle h(raw);
    bind_sv(h.get(), 1, updated_at);
    bind_sv(h.get(), 2, id);
    bind_sv(h.get(), 3, tenant);
    if (sqlite3_step(h.get()) != SQLITE_DONE)
        throw make_sqlite_error(db, "StatementStore::approve_review step");
    return sqlite3_changes(db);
}

int SqliteStatementStore::set_confidence_consolidated(
    std::string_view id, std::string_view tenant, double confidence) {
    // Supports cannot revive terminal or unrelated consolidation states.
    sqlite3* db = conn_.raw();
    const char* sql =
        "UPDATE statements SET confidence=?, consolidation_state='consolidated' "
        "WHERE id=? AND tenant_id=? "
        "AND consolidation_state IN ('consolidated','replaying_reconsolidating')";
    sqlite3_stmt* raw = nullptr;
    if (sqlite3_prepare_v2(db, sql, -1, &raw, nullptr) != SQLITE_OK)
        throw make_sqlite_error(db, "StatementStore::set_confidence_consolidated prepare");
    StmtHandle h(raw);
    sqlite3_bind_double(h.get(), 1, confidence);
    bind_sv(h.get(), 2, id);
    bind_sv(h.get(), 3, tenant);
    if (sqlite3_step(h.get()) != SQLITE_DONE)
        throw make_sqlite_error(db, "StatementStore::set_confidence_consolidated step");
    return sqlite3_changes(db);
}

void SqliteStatementStore::insert_arbitrated_fork(const ArbitratedFork& f) {
    // Fork identity/state/timestamps come from arbitration; all remaining
    // non-generated columns are inherited from the tenant-scoped original.
    sqlite3* db = conn_.raw();
    const char* sql =
        "INSERT INTO statements("
        "id, tenant_id, holder_id, holder_perspective, subject_kind, subject_id, "
        "predicate, object_kind, object_value, canonical_object_hash, "
        "canonical_object_hash_version, modality, polarity, confidence, observed_at, "
        "salience, affect_json, activation, last_accessed, provenance, "
        "consolidation_state, review_status, supersedes_id, created_at, updated_at, "
        "inferred_at, valid_from, valid_to, event_time_start, "
        "event_time_end, confidence_history_json, evidence_json, source_spans_json, "
        "temporal_anchor_json, derived_from_json, derived_depth, perceived_by_json, "
        "access_count, last_replayed, replay_count, nesting_depth, "
        "visibility_json, retention_policy, last_replay_batch_id, scope_parties_json, "
        "consolidation_summary, semantic_claim_json) "
        "SELECT ?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16,?17,?18,?19,"
        "'reconsolidation_derived','consolidated',?20,?21,?22,?23, "
        "inferred_at, valid_from, valid_to, event_time_start, "
        "event_time_end, confidence_history_json, evidence_json, source_spans_json, "
        "temporal_anchor_json, derived_from_json, derived_depth, perceived_by_json, "
        "access_count, last_replayed, replay_count, nesting_depth, "
        "visibility_json, retention_policy, last_replay_batch_id, scope_parties_json, "
        "consolidation_summary, semantic_claim_json "
        "FROM statements WHERE id=?21 AND tenant_id=?2";
    sqlite3_stmt* raw = nullptr;
    if (sqlite3_prepare_v2(db, sql, -1, &raw, nullptr) != SQLITE_OK)
        throw make_sqlite_error(db, "StatementStore::insert_arbitrated_fork prepare");
    StmtHandle h(raw);
    bind_sv(h.get(),  1, f.new_id);
    bind_sv(h.get(),  2, f.tenant_id);
    bind_sv(h.get(),  3, f.holder_id);
    bind_sv(h.get(),  4, f.holder_perspective);
    bind_sv(h.get(),  5, f.subject_kind);
    bind_sv(h.get(),  6, f.subject_id);
    bind_sv(h.get(),  7, f.predicate);
    bind_sv(h.get(),  8, f.object_kind);
    bind_sv(h.get(),  9, f.object_value);
    bind_sv(h.get(), 10, f.canonical_object_hash);
    bind_sv(h.get(), 11, f.canonical_object_hash_version);
    bind_sv(h.get(), 12, f.modality);
    bind_sv(h.get(), 13, f.polarity);
    sqlite3_bind_double(h.get(), 14, f.confidence);
    bind_sv(h.get(), 15, f.observed_at);
    bind_sv(h.get(), 16, f.salience_str);
    bind_sv(h.get(), 17, f.affect_json);
    bind_sv(h.get(), 18, f.activation_str);
    bind_sv(h.get(), 19, f.last_accessed);
    bind_sv(h.get(), 20, f.review_status);
    bind_sv(h.get(), 21, f.supersedes_id);
    bind_sv(h.get(), 22, f.created_at);
    bind_sv(h.get(), 23, f.updated_at);
    if (sqlite3_step(h.get()) != SQLITE_DONE)
        throw make_sqlite_error(db, "StatementStore::insert_arbitrated_fork step");
}

void SqliteStatementStore::inherit_salience(
    std::string_view id, std::string_view tenant, double min_salience,
    std::string_view affect_json) {
    // exact 自 src/tom/second_order.cpp:170(salience 继承)。原 WHERE 仅 id;
    // 这里加 tenant 守卫(id 全局唯一,行为等价),与本 store 其余法一致。
    sqlite3* db = conn_.raw();
    const char* sql =
        "UPDATE statements SET salience = MAX(salience, ?1), affect_json = ?2 "
        "WHERE id = ?3 AND tenant_id = ?4";
    sqlite3_stmt* raw = nullptr;
    if (sqlite3_prepare_v2(db, sql, -1, &raw, nullptr) != SQLITE_OK)
        throw make_sqlite_error(db, "StatementStore::inherit_salience prepare");
    StmtHandle h(raw);
    sqlite3_bind_double(h.get(), 1, min_salience);
    bind_sv(h.get(), 2, affect_json);
    bind_sv(h.get(), 3, id);
    bind_sv(h.get(), 4, tenant);
    if (sqlite3_step(h.get()) != SQLITE_DONE)
        throw make_sqlite_error(db, "StatementStore::inherit_salience step");
}

}  // namespace starling::store
