// reconsolidation_engine.cpp -- ReconsolidationEngine outbox subscriber.
// Consumes bus_events → opens reconsolidation windows; close_due_windows
// arbitrates expired windows.  TC-A5-002 double-layer fallback ensures no
// hang if arbitration throws.

#include "starling/reconsolidation/reconsolidation_engine.hpp"

#include "starling/reconsolidation/arbitration.hpp"
#include "starling/reconsolidation/plastic_window.hpp"

#include "starling/persistence/sqlite_helpers.hpp"
#include "starling/persistence/connection.hpp"
#include "starling/persistence/sqlite_handles.hpp"
#include "starling/store/sqlite_statement_store.hpp"

#include <sqlite3.h>

#include <stdexcept>
#include <string>
#include <vector>

namespace starling::reconsolidation {

namespace {

using starling::persistence::detail::bind_sv;
using starling::persistence::detail::make_sqlite_error;
using starling::persistence::StmtHandle;

// Read the current reconsolidation checkpoint value.
int read_checkpoint(persistence::Connection& conn) {
    const char* sql =
        "SELECT last_processed_outbox_sequence "
        "FROM reconsolidation_checkpoint WHERE id = 1";
    sqlite3_stmt* raw = nullptr;
    if (sqlite3_prepare_v2(conn.raw(), sql, -1, &raw, nullptr) != SQLITE_OK)
        throw make_sqlite_error(conn.raw(), "reconsolidation_engine: read checkpoint prepare");
    StmtHandle h(raw);
    if (sqlite3_step(h.get()) == SQLITE_ROW)
        return sqlite3_column_int(h.get(), 0);
    return 0;
}

// Advance checkpoint to new_seq.
void write_checkpoint(persistence::Connection& conn, int new_seq,
                      std::string_view now_iso) {
    const char* sql =
        "UPDATE reconsolidation_checkpoint "
        "SET last_processed_outbox_sequence = ?, "
        "    last_updated_at = ? "
        "WHERE id = 1";
    sqlite3_stmt* raw = nullptr;
    if (sqlite3_prepare_v2(conn.raw(), sql, -1, &raw, nullptr) != SQLITE_OK)
        throw make_sqlite_error(conn.raw(), "reconsolidation_engine: update checkpoint prepare");
    StmtHandle h(raw);
    sqlite3_bind_int(h.get(), 1, new_seq);
    bind_sv(h.get(), 2, now_iso);
    if (sqlite3_step(h.get()) != SQLITE_DONE)
        throw make_sqlite_error(conn.raw(), "reconsolidation_engine: update checkpoint step");
}

// Look up modality of a statement (default 'believes' if missing).
std::string lookup_modality(persistence::Connection& conn,
                            std::string_view stmt_id,
                            std::string_view tenant_id) {
    const char* sql =
        "SELECT modality FROM statements WHERE id = ? AND tenant_id = ?";
    sqlite3_stmt* raw = nullptr;
    if (sqlite3_prepare_v2(conn.raw(), sql, -1, &raw, nullptr) != SQLITE_OK)
        throw make_sqlite_error(conn.raw(), "reconsolidation_engine: modality lookup prepare");
    StmtHandle h(raw);
    bind_sv(h.get(), 1, stmt_id);
    bind_sv(h.get(), 2, tenant_id);
    if (sqlite3_step(h.get()) == SQLITE_ROW) {
        const auto* txt = sqlite3_column_text(h.get(), 0);
        if (txt) return reinterpret_cast<const char*>(txt);
    }
    return "believes";
}

struct TenantLookup {
    std::string tenant_id;
    bool found = false;
    bool ambiguous = false;
};

// Look up tenant_id of a statement. Missing statements are no-op for the
// explicit API; ambiguous bare stmt_id must fail closed instead of selecting an
// arbitrary tenant.
TenantLookup lookup_tenant(persistence::Connection& conn,
                           std::string_view stmt_id) {
    const char* sql =
        "SELECT DISTINCT tenant_id FROM statements WHERE id = ? LIMIT 2";
    sqlite3_stmt* raw = nullptr;
    if (sqlite3_prepare_v2(conn.raw(), sql, -1, &raw, nullptr) != SQLITE_OK)
        return {};
    StmtHandle h(raw);
    bind_sv(h.get(), 1, stmt_id);
    if (sqlite3_step(h.get()) == SQLITE_ROW) {
        const auto* txt = sqlite3_column_text(h.get(), 0);
        if (!txt) return {};
        std::string out = reinterpret_cast<const char*>(txt);
        if (sqlite3_step(h.get()) == SQLITE_ROW) {
            return {.tenant_id = out, .found = true, .ambiguous = true};
        }
        return {.tenant_id = out, .found = true, .ambiguous = false};
    }
    return {};
}

struct EventRow {
    std::string event_id;
    std::string event_type;
    std::string primary_id;
    std::string tenant_id;
    int         outbox_sequence = 0;
};

}  // namespace

// ── Constructor ─────────────────────────────────────────────────────────────

ReconsolidationEngine::ReconsolidationEngine(persistence::SqliteAdapter& adapter)
    : adapter_(adapter) {
    // adapter_ is stored for potential future use (e.g. opening new connections).
    (void)adapter_;
}

// ── tick_one_batch ──────────────────────────────────────────────────────────

EngineStats ReconsolidationEngine::tick_one_batch(
    persistence::Connection& conn, std::string_view now_iso) {

    EngineStats stats;

    // 1. Read checkpoint.
    const int last_seq = read_checkpoint(conn);

    // 2. SELECT events after checkpoint.
    std::vector<EventRow> batch;
    {
        const char* sql =
            "SELECT event_id, event_type, primary_id, tenant_id, outbox_sequence "
            "FROM bus_events "
            "WHERE outbox_sequence > ? "
            "ORDER BY outbox_sequence";
        sqlite3_stmt* raw = nullptr;
        if (sqlite3_prepare_v2(conn.raw(), sql, -1, &raw, nullptr) != SQLITE_OK)
            throw make_sqlite_error(conn.raw(), "reconsolidation_engine: select events prepare");
        StmtHandle h(raw);
        sqlite3_bind_int(h.get(), 1, last_seq);
        while (sqlite3_step(h.get()) == SQLITE_ROW) {
            EventRow row;
            auto get_text = [&](int col) -> std::string {
                const char* t = reinterpret_cast<const char*>(
                    sqlite3_column_text(h.get(), col));
                return t ? t : "";
            };
            row.event_id        = get_text(0);
            row.event_type      = get_text(1);
            row.primary_id      = get_text(2);
            row.tenant_id       = get_text(3);
            row.outbox_sequence = sqlite3_column_int(h.get(), 4);
            batch.push_back(std::move(row));
        }
    }

    if (batch.empty())
        return stats;

    // 3. Process each event.
    int max_seq = last_seq;
    for (const auto& ev : batch) {
        stats.events_processed++;
        if (ev.outbox_sequence > max_seq) max_seq = ev.outbox_sequence;

        // 5 trigger types:
        //   statement.recalled / statement.references_existing / belief.conflict
        //   / reconsolidate.requested(P3.a3 显式 API,触发器 #4)→ open window.
        //   commitment.fulfilled / commitment.broken → stub, skip.
        //   others → skip.
        if (ev.event_type == "statement.recalled" ||
            ev.event_type == "statement.references_existing" ||
            ev.event_type == "belief.conflict" ||
            ev.event_type == "reconsolidate.requested") {
            const std::string tenant = ev.tenant_id;
            if (tenant.empty()) continue;
            const std::string modality =
                lookup_modality(conn, ev.primary_id, tenant);
            OpenResult res = open_or_append(
                conn,
                ev.primary_id,    // stmt_id
                tenant,           // tenant_id
                ev.event_id,      // event_id
                ev.event_type,    // event_type
                ev.event_id,      // payload_hash (use event_id as hash)
                ev.event_type == "statement.recalled" ? 0.0 : 1.0, // recall is not evidence
                modality,
                now_iso);
            if (res.opened) stats.windows_opened++;
        }
        // commitment.fulfilled / commitment.broken: P2.c stub — intentionally skip.
        // Unknown types: silently skip.
    }

    // 4. Advance checkpoint.
    write_checkpoint(conn, max_seq, now_iso);

    return stats;
}

// ── close_due_windows ───────────────────────────────────────────────────────

int ReconsolidationEngine::close_due_windows(
    persistence::Connection& conn, std::string_view now_iso) {

    // Get list of stmt_ids with expired windows.
    std::vector<DueWindow> due;
    try {
        due = due_windows(conn, now_iso);
    } catch (...) {
        return 0;
    }

    int closed = 0;
    for (const auto& window : due) {
        const std::string& stmt_id = window.stmt_id;
        const std::string& tenant = window.tenant_id;

        // TC-A5-002 double-layer fallback:
        // Try arbitration; on any failure, reset stmt to consolidated.
        try {
            Aggregated agg = aggregate_evidence(conn, stmt_id, tenant);
            if (agg.path == ArbitrationPath::Supports) {
                apply_supports(conn, stmt_id, tenant, agg, now_iso);
            } else if (agg.path == ArbitrationPath::MildContradict) {
                apply_mild_contradict(conn, stmt_id, tenant, agg, now_iso);
            } else {
                apply_severe_contradict(conn, stmt_id, tenant, agg, now_iso);
            }
        } catch (...) {
            // Fallback: ensure stmt is not stuck in replaying_reconsolidating.
            // Best-effort; ignore if stmt was deleted.
            try {
                // P3.b1 phase 2:写收编进 StatementStore;异常被本 catch 吞(best-effort 保留)。
                store::SqliteStatementStore(conn).restore_consolidated(stmt_id, tenant);
            } catch (...) {}
        }

        // ALWAYS mark the window closed, regardless of arbitration outcome.
        {
            const char* sql =
                "UPDATE reconsolidation_windows SET status='closed' "
                "WHERE stmt_id=? AND tenant_id=?";
            sqlite3_stmt* raw = nullptr;
            if (sqlite3_prepare_v2(conn.raw(), sql, -1, &raw, nullptr) == SQLITE_OK) {
                StmtHandle h(raw);
                bind_sv(h.get(), 1, stmt_id);
                bind_sv(h.get(), 2, tenant);
                sqlite3_step(h.get());
            }
        }
        clear_pending_evidence(conn, stmt_id, tenant);
        closed++;
    }

    return closed;
}

// ── reconsolidate (explicit API) ────────────────────────────────────────────

void ReconsolidationEngine::reconsolidate(
    persistence::Connection& conn,
    std::string_view stmt_id,
    std::string_view event_type,
    std::string_view payload_hash,
    double weight,
    std::string_view now_iso) {

    const TenantLookup tenant = lookup_tenant(conn, stmt_id);
    if (!tenant.found) {
        return;
    }
    if (tenant.ambiguous) {
        throw std::runtime_error(
            "ReconsolidationEngine::reconsolidate: statement tenant-ambiguous");
    }
    const std::string modality = lookup_modality(conn, stmt_id, tenant.tenant_id);

    open_or_append(
        conn,
        stmt_id,
        tenant.tenant_id,
        payload_hash,   // event_id = payload_hash
        event_type,
        payload_hash,
        weight,
        modality,
        now_iso);
}

}  // namespace starling::reconsolidation
