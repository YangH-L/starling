// test_reconsolidation_engine.cpp -- Tests for ReconsolidationEngine.
//
// TC-A5-001  tick_one_batch: belief.conflict event → window opened, checkpoint advances.
// TC-A5-002  close_due_windows: arbitrates expired window → window status=closed.
// TC-A5-003  commitment.fulfilled event → NOT opened (P2.c stub).
// TC-A5-004  TC-A5-002 fallback: deleted stmt → no throw, window still closed.

#include "starling/reconsolidation/reconsolidation_engine.hpp"
#include "starling/reconsolidation/plastic_window.hpp"
#include "starling/reconsolidation/arbitration.hpp"

#include "starling/bus/bus_event.hpp"
#include "starling/bus/subscriber_pump.hpp"
#include "starling/bus/statement_writer.hpp"
#include "starling/store/sqlite_statement_store.hpp"
#include "starling/bus/outbox_writer.hpp"
#include "starling/persistence/connection.hpp"
#include "starling/persistence/sqlite_adapter.hpp"
#include "starling/persistence/sqlite_handles.hpp"

#include <gtest/gtest.h>
#include <sqlite3.h>

#include <memory>
#include <string>

using namespace starling::reconsolidation;
using starling::bus::BusEvent;
using starling::bus::OutboxWriter;
using starling::persistence::Connection;
using starling::persistence::SqliteAdapter;
using starling::persistence::StmtHandle;
using starling::persistence::TransactionGuard;

namespace {

// ── Helpers ──────────────────────────────────────────────────────────────────

// Open a fresh in-memory DB with all migrations applied.
std::unique_ptr<SqliteAdapter> open_fresh() {
    return SqliteAdapter::open(":memory:");
}

// Seed a consolidated statement row.
void seed_stmt(sqlite3* db, const std::string& id,
               const std::string& tenant = "default",
               const std::string& modality = "believes") {
    std::string s =
        "INSERT INTO statements(id,tenant_id,holder_id,holder_perspective,"
        "subject_kind,subject_id,predicate,object_kind,object_value,canonical_object_hash,"
        "canonical_object_hash_version,modality,polarity,confidence,observed_at,salience,"
        "affect_json,activation,last_accessed,provenance,consolidation_state,review_status,"
        "confidence_history_json,created_at,updated_at) VALUES('" + id + "','" + tenant + "','alice',"
        "'first_person','cognizer','bob','knows','str','x','" + std::string(64,'a') + "','v1','"
        + modality + "','pos',0.7,'2026-05-27T09:00:00Z',0.5,'{}',0.0,"
        "'2026-05-27T09:00:00Z','user_input','consolidated','approved','[]',"
        "'2026-05-27T09:00:00Z','2026-05-27T09:00:00Z')";
    sqlite3_exec(db, s.c_str(), nullptr, nullptr, nullptr);
}

// Append a bus event via OutboxWriter.
void append_bus_event(Connection& conn,
                      const std::string& event_type,
                      const std::string& primary_id,
                      const std::string& tenant_id,
                      const std::string& payload_json,
                      const std::string& ikey) {
    TransactionGuard tx(conn);
    OutboxWriter ow(conn);
    BusEvent ev;
    ev.tenant_id       = tenant_id;
    ev.event_type      = event_type;
    ev.primary_id      = primary_id;
    ev.aggregate_id    = primary_id;
    ev.idempotency_key = ikey;
    ev.payload_json    = payload_json;
    ow.append(ev);
    tx.commit();
}

// Read single text column.
std::string scol(sqlite3* db, const std::string& q) {
    sqlite3_stmt* s = nullptr;
    sqlite3_prepare_v2(db, q.c_str(), -1, &s, nullptr);
    sqlite3_step(s);
    const auto* txt = sqlite3_column_text(s, 0);
    std::string v = txt ? reinterpret_cast<const char*>(txt) : "";
    sqlite3_finalize(s);
    return v;
}

// Read single int column.
int icol(sqlite3* db, const std::string& q) {
    sqlite3_stmt* s = nullptr;
    sqlite3_prepare_v2(db, q.c_str(), -1, &s, nullptr);
    sqlite3_step(s);
    int v = sqlite3_column_int(s, 0);
    sqlite3_finalize(s);
    return v;
}

// Read reconsolidation_checkpoint value.
int get_recon_checkpoint(Connection& conn) {
    return icol(conn.raw(),
        "SELECT last_processed_outbox_sequence "
        "FROM reconsolidation_checkpoint WHERE id=1");
}

}  // namespace

// ── TC-A5-001: belief.conflict event → window opened, checkpoint advances ──

TEST(ReconsolidationEngine, TickBeliefConflictOpensWindow) {
    auto adapter = open_fresh();
    auto& conn   = adapter->connection();
    ReconsolidationEngine engine(*adapter);

    // Seed a consolidated statement.
    seed_stmt(conn.raw(), "stmt-1");

    // Emit a belief.conflict bus event for that statement.
    append_bus_event(conn, "belief.conflict", "stmt-1", "default",
                     "{}", "ikey-bc-1");

    // Run a tick.
    auto stats = engine.tick_one_batch(conn, "2026-05-27T10:00:00Z");
    EXPECT_EQ(stats.events_processed, 1);
    EXPECT_EQ(stats.windows_opened, 1);

    // Window row should exist and be open.
    EXPECT_EQ(scol(conn.raw(),
        "SELECT status FROM reconsolidation_windows WHERE stmt_id='stmt-1'"),
        "open");

    // Checkpoint should have advanced beyond 0.
    int chk = get_recon_checkpoint(conn);
    EXPECT_GT(chk, 0);
}

// ── TC-A5-001b: statement.recalled also opens a window ──────────────────────

TEST(ReconsolidationEngine, TickRecalledOpensWindow) {
    auto adapter = open_fresh();
    auto& conn   = adapter->connection();
    ReconsolidationEngine engine(*adapter);

    seed_stmt(conn.raw(), "stmt-2");
    append_bus_event(conn, "statement.recalled", "stmt-2", "default",
                     "{}", "ikey-rec-1");

    auto stats = engine.tick_one_batch(conn, "2026-05-27T10:00:00Z");
    EXPECT_EQ(stats.events_processed, 1);
    EXPECT_EQ(stats.windows_opened, 1);
    EXPECT_EQ(scol(conn.raw(),
        "SELECT status FROM reconsolidation_windows WHERE stmt_id='stmt-2'"),
        "open");
}

TEST(ReconsolidationEngine, TickUsesEventTenantForSharedStmtId) {
    auto adapter = open_fresh();
    auto& conn   = adapter->connection();
    ReconsolidationEngine engine(*adapter);

    seed_stmt(conn.raw(), "stmt-shared", "tenant-a");
    seed_stmt(conn.raw(), "stmt-shared", "tenant-b", "COMMITS");
    append_bus_event(conn, "statement.recalled", "stmt-shared", "tenant-b",
                     "{}", "ikey-rec-shared-b");

    auto stats = engine.tick_one_batch(conn, "2026-05-27T10:00:00Z");
    EXPECT_EQ(stats.events_processed, 1);
    EXPECT_EQ(stats.windows_opened, 1);

    EXPECT_EQ(icol(conn.raw(),
        "SELECT COUNT(*) FROM reconsolidation_windows WHERE stmt_id='stmt-shared'"), 1);
    EXPECT_EQ(scol(conn.raw(),
        "SELECT tenant_id FROM reconsolidation_windows WHERE stmt_id='stmt-shared'"), "tenant-b");
}

// ── TC-A5-001c: checkpoint idempotency — second tick sees 0 events ──────────

TEST(ReconsolidationEngine, CheckpointAdvancesAndIdempotent) {
    auto adapter = open_fresh();
    auto& conn   = adapter->connection();
    ReconsolidationEngine engine(*adapter);

    seed_stmt(conn.raw(), "stmt-3");
    append_bus_event(conn, "belief.conflict", "stmt-3", "default",
                     "{}", "ikey-bc-idem");

    auto s1 = engine.tick_one_batch(conn, "2026-05-27T10:00:00Z");
    EXPECT_EQ(s1.events_processed, 1);

    // Second tick: nothing new.
    auto s2 = engine.tick_one_batch(conn, "2026-05-27T10:01:00Z");
    EXPECT_EQ(s2.events_processed, 0);
    EXPECT_EQ(s2.windows_opened, 0);
}

TEST(ReconsolidationEngine, HandlerFailureDoesNotAdvanceCheckpoint) {
    auto adapter = open_fresh();
    auto& conn = adapter->connection();
    ReconsolidationEngine engine(*adapter);
    seed_stmt(conn.raw(), "stmt-fail");
    append_bus_event(conn, "belief.conflict", "stmt-fail", "default",
                     "{}", "ikey-recon-handler-fail");
    ASSERT_EQ(sqlite3_exec(conn.raw(), "DROP TABLE reconsolidation_windows",
                           nullptr, nullptr, nullptr), SQLITE_OK);

    EXPECT_THROW(engine.tick_one_batch(conn, "2026-05-27T10:00:00Z"), std::exception);
    EXPECT_EQ(get_recon_checkpoint(conn), 0);
}

// ── TC-A5-002: close_due_windows arbitrates expired window ──────────────────

TEST(ReconsolidationEngine, CloseDueWindowsArbitrates) {
    auto adapter = open_fresh();
    auto& conn   = adapter->connection();
    ReconsolidationEngine engine(*adapter);

    // Open a window via the explicit API.
    seed_stmt(conn.raw(), "stmt-arb");
    engine.reconsolidate(conn, "stmt-arb", "belief.conflict", "hash-1", 1.0,
                         "2026-05-27T10:00:00Z");

    // Window is open; deadline is ~30 min out.
    EXPECT_EQ(scol(conn.raw(),
        "SELECT status FROM reconsolidation_windows WHERE stmt_id='stmt-arb'"),
        "open");

    // Should not be due yet (10 minutes later).
    int closed_early = engine.close_due_windows(conn, "2026-05-27T10:10:00Z");
    EXPECT_EQ(closed_early, 0);

    // Past deadline (2 hours later) → should arbitrate and close.
    int closed = engine.close_due_windows(conn, "2026-05-27T12:00:00Z");
    EXPECT_EQ(closed, 1);

    EXPECT_EQ(scol(conn.raw(),
        "SELECT status FROM reconsolidation_windows WHERE stmt_id='stmt-arb'"),
        "closed");
}

// ── TC-A5-003: commitment.fulfilled → NOT opened ────────────────────────────

TEST(ReconsolidationEngine, CommitmentFulfilledIsSkipped) {
    auto adapter = open_fresh();
    auto& conn   = adapter->connection();
    ReconsolidationEngine engine(*adapter);

    seed_stmt(conn.raw(), "stmt-c");
    append_bus_event(conn, "commitment.fulfilled", "stmt-c", "default",
                     "{}", "ikey-cf-1");
    append_bus_event(conn, "commitment.broken", "stmt-c", "default",
                     "{}", "ikey-cb-1");

    auto stats = engine.tick_one_batch(conn, "2026-05-27T10:00:00Z");
    EXPECT_EQ(stats.events_processed, 2);
    EXPECT_EQ(stats.windows_opened, 0);

    // No window should have been opened.
    EXPECT_EQ(icol(conn.raw(),
        "SELECT COUNT(*) FROM reconsolidation_windows WHERE stmt_id='stmt-c'"), 0);
}

// ── TC-A5-004: deleted stmt → fallback, no throw, window still closed ───────

TEST(ReconsolidationEngine, FallbackDeletedStmtDoesNotThrow) {
    auto adapter = open_fresh();
    auto& conn   = adapter->connection();
    ReconsolidationEngine engine(*adapter);

    // Open a window for a statement that will be deleted.
    seed_stmt(conn.raw(), "stmt-del");
    engine.reconsolidate(conn, "stmt-del", "belief.conflict", "hash-del", 1.0,
                         "2026-05-27T10:00:00Z");

    EXPECT_EQ(scol(conn.raw(),
        "SELECT status FROM reconsolidation_windows WHERE stmt_id='stmt-del'"),
        "open");

    // Delete the statement (simulates a race condition).
    sqlite3_exec(conn.raw(),
        "DELETE FROM statements WHERE id='stmt-del'",
        nullptr, nullptr, nullptr);

    // close_due_windows should NOT throw, and the window should end up closed.
    int closed = 0;
    EXPECT_NO_THROW(
        closed = engine.close_due_windows(conn, "2026-05-27T12:00:00Z")
    );
    EXPECT_EQ(closed, 1);

    EXPECT_EQ(scol(conn.raw(),
        "SELECT status FROM reconsolidation_windows WHERE stmt_id='stmt-del'"),
        "closed");
}

// ── reconsolidate explicit API: opens then appends on repeat call ─────────

TEST(ReconsolidationEngine, ReconsolidateOpensAndAppends) {
    auto adapter = open_fresh();
    auto& conn   = adapter->connection();
    ReconsolidationEngine engine(*adapter);

    seed_stmt(conn.raw(), "stmt-r");

    // First call: opens window.
    engine.reconsolidate(conn, "stmt-r", "belief.conflict", "hash-r1", 1.0,
                         "2026-05-27T10:00:00Z");
    EXPECT_EQ(scol(conn.raw(),
        "SELECT status FROM reconsolidation_windows WHERE stmt_id='stmt-r'"),
        "open");

    // Second call with a different hash (different event_id): appends.
    engine.reconsolidate(conn, "stmt-r", "belief.conflict", "hash-r2", 0.5,
                         "2026-05-27T10:01:00Z");

    // Window is still open (not yet closed) after two triggers.
    EXPECT_EQ(scol(conn.raw(),
        "SELECT status FROM reconsolidation_windows WHERE stmt_id='stmt-r'"),
        "open");

    // There should be 1 pending evidence entry (from the second call).
    EXPECT_GE(icol(conn.raw(),
        "SELECT COUNT(*) FROM reconsolidation_pending_evidence "
        "WHERE window_stmt_id='stmt-r'"), 1);
}

TEST(ReconsolidationEngine, ReconsolidateMissingStatementIsNoOp) {
    auto adapter = open_fresh();
    auto& conn   = adapter->connection();
    ReconsolidationEngine engine(*adapter);

    EXPECT_NO_THROW(engine.reconsolidate(conn, "missing", "belief.conflict",
                                         "hash-missing", 1.0,
                                         "2026-05-27T10:00:00Z"));
    EXPECT_EQ(icol(conn.raw(),
        "SELECT COUNT(*) FROM reconsolidation_windows WHERE stmt_id='missing'"),
        0);
}

TEST(ReconsolidationEngine, ReconsolidateAmbiguousSharedStmtIdFailsClosed) {
    auto adapter = open_fresh();
    auto& conn   = adapter->connection();
    ReconsolidationEngine engine(*adapter);

    seed_stmt(conn.raw(), "stmt-shared", "tenant-a");
    seed_stmt(conn.raw(), "stmt-shared", "tenant-b");

    EXPECT_THROW(engine.reconsolidate(conn, "stmt-shared", "belief.conflict",
                                      "hash-shared", 1.0,
                                      "2026-05-27T10:00:00Z"),
                 std::runtime_error);
    EXPECT_EQ(icol(conn.raw(),
        "SELECT COUNT(*) FROM reconsolidation_windows WHERE stmt_id='stmt-shared'"),
        0);
}

// ── Regression: severe arbitration inside an outer SAVEPOINT (pump context) ──
//
// SubscriberPump::run_isolated wraps each subscriber in `SAVEPOINT sub_*`, so in
// production close_due_windows runs inside an active savepoint-transaction.
// apply_severe_contradict previously opened a persistence::TransactionGuard
// (BEGIN IMMEDIATE), which throws "cannot start a transaction within a
// transaction" inside that savepoint; close_due_windows' fallback then silently
// swallowed it and the severe fork was lost. With the SAVEPOINT-based guard the
// 4-item severe commit lands correctly in BOTH contexts. This test reproduces
// the pump context by wrapping close_due_windows in an explicit outer SAVEPOINT.
TEST(ReconsolidationEngine, SevereArbitrationWorksInsideOuterSavepoint) {
    auto adapter = open_fresh();
    auto& conn   = adapter->connection();
    ReconsolidationEngine engine(*adapter);

    seed_stmt(conn.raw(), "stmt-sev");
    // Accumulate high-weight contradicting evidence (first call opens the window
    // with no pending row; calls 2-5 append pending evidence) → strength 1.0
    // > 0.7 → severe. Mirrors the Python TC-A8-001 setup.
    for (int i = 0; i < 5; ++i) {
        engine.reconsolidate(conn, "stmt-sev", "belief.conflict",
                             "hash-sev-" + std::to_string(i), 1.0,
                             "2026-05-27T10:00:00Z");
    }

    // Simulate the pump: wrap close_due_windows in an outer SAVEPOINT.
    ASSERT_EQ(sqlite3_exec(conn.raw(), "SAVEPOINT sub_reconsolidation",
                           nullptr, nullptr, nullptr), SQLITE_OK);
    int closed = 0;
    EXPECT_NO_THROW(
        closed = engine.close_due_windows(conn, "2026-05-27T12:00:00Z"));
    ASSERT_EQ(sqlite3_exec(conn.raw(), "RELEASE sub_reconsolidation",
                           nullptr, nullptr, nullptr), SQLITE_OK);

    EXPECT_EQ(closed, 1);

    // The severe fork must have committed inside the outer savepoint:
    //  (1) a new reconsolidation_derived CONSOLIDATED statement exists,
    //  (2) the old statement is archived,
    //  (3) a SUPERSEDES edge new→old was created.
    EXPECT_EQ(icol(conn.raw(),
        "SELECT COUNT(*) FROM statements WHERE provenance='reconsolidation_derived' "
        "AND consolidation_state='consolidated'"), 1);
    EXPECT_EQ(scol(conn.raw(),
        "SELECT consolidation_state FROM statements WHERE id='stmt-sev'"), "archived");
    EXPECT_EQ(icol(conn.raw(),
        "SELECT COUNT(*) FROM statement_edges WHERE dst_id='stmt-sev' "
        "AND edge_kind='supersedes'"), 1);
}


// PR-1a: recall is an observation of access, not contradictory evidence.
TEST(ReconsolidationEngine, Pr1aThreeRecallsThenWriteDoNotFork) {
    auto adapter = open_fresh(); auto& conn = adapter->connection();
    ReconsolidationEngine engine(*adapter);
    seed_stmt(conn.raw(), "recall-only");
    for (int i = 0; i < 3; ++i) {
        append_bus_event(conn, "statement.recalled", "recall-only", "default", "{}",
                         "recall-" + std::to_string(i));
        engine.tick_one_batch(conn, "2026-05-27T10:00:00Z");
    }
    EXPECT_EQ(icol(conn.raw(), "SELECT COUNT(*) FROM reconsolidation_pending_evidence WHERE weight<>0"), 0);
    // Commit a real StatementWriter write, then run the production post-write
    // pump with a controlled clock just after the 30-minute deadline.
    starling::extractor::ExtractedStatement written;
    written.holder_id = "alice"; written.holder_tenant_id = "default";
    written.holder_perspective = starling::schema::Perspective::FIRST_PERSON;
    written.subject_kind = "entity"; written.subject_id = "later-subject";
    written.predicate = "knows"; written.object_kind = "str"; written.object_value = "y";
    written.canonical_object_hash = std::string(64, 'b'); written.confidence = 0.9;
    written.observed_at = "2026-05-27T10:31:00Z"; written.source_hash = "later-hash";
    written.perceived_by = {"alice"};
    conn.exec("INSERT INTO engrams(id,tenant_id,content_hash,source_kind,ingest_policy,ingest_mode,"
              "privacy_class,retention_mode,refcount,payload_inline,created_at,adapter_name,source_item_id) VALUES("
              "'later-source','default','later-hash','user_input','store','whole_record','internal','audit_retain',"
              "0,X'','2026-05-27T10:31:00Z','test','later-source')");
    {
        TransactionGuard tx(conn);
        const auto outcome = starling::bus::StatementWriter(conn).write(
            written, "later-source", "later-span", std::nullopt);
        ASSERT_TRUE(std::holds_alternative<starling::bus::StatementWriteAccepted>(outcome));
        tx.commit();
    }
    starling::bus::SubscriberPump::run_post_write(*adapter, conn, "2026-05-27T10:31:00Z");
    EXPECT_EQ(scol(conn.raw(), "SELECT consolidation_state FROM statements WHERE id='recall-only'"), "consolidated");
    EXPECT_EQ(icol(conn.raw(), "SELECT COUNT(*) FROM statements"), 2);
    EXPECT_EQ(icol(conn.raw(), "SELECT COUNT(*) FROM statement_edges WHERE edge_kind='supersedes'"), 0);
    EXPECT_EQ(scol(conn.raw(), "SELECT status FROM reconsolidation_windows WHERE stmt_id='recall-only'"), "closed");
}

TEST(ReconsolidationEngine, Pr1aExpiredRecallCannotResurrectArchived) {
    auto adapter = open_fresh(); auto& conn = adapter->connection();
    ReconsolidationEngine engine(*adapter);
    seed_stmt(conn.raw(), "archived-recall");
    append_bus_event(conn, "statement.recalled", "archived-recall", "default", "{}", "one-recall");
    engine.tick_one_batch(conn, "2026-05-27T10:00:00Z");
    conn.exec("UPDATE statements SET consolidation_state='archived' WHERE id='archived-recall'");
    ASSERT_EQ(engine.close_due_windows(conn, "2026-05-27T10:31:00Z"), 1);
    EXPECT_EQ(scol(conn.raw(), "SELECT consolidation_state FROM statements WHERE id='archived-recall'"), "archived");
}

TEST(ReconsolidationEngine, Pr1aCloseDeletesOnlyItsPendingEvidence) {
    auto adapter = open_fresh(); auto& conn = adapter->connection();
    ReconsolidationEngine engine(*adapter);
    seed_stmt(conn.raw(), "due"); seed_stmt(conn.raw(), "later", "other");
    for (int i = 0; i < 2; ++i) {
        engine.reconsolidate(conn, "due", "belief.conflict", "due-" + std::to_string(i), 1.0, "2026-05-27T10:00:00Z");
        engine.reconsolidate(conn, "later", "belief.conflict", "later-" + std::to_string(i), 1.0, "2026-05-27T10:20:00Z");
    }
    ASSERT_EQ(icol(conn.raw(), "SELECT COUNT(*) FROM reconsolidation_pending_evidence"), 2);
    ASSERT_EQ(engine.close_due_windows(conn, "2026-05-27T10:31:00Z"), 1);
    EXPECT_EQ(icol(conn.raw(), "SELECT COUNT(*) FROM reconsolidation_pending_evidence WHERE window_stmt_id='due'"), 0);
    EXPECT_EQ(icol(conn.raw(), "SELECT COUNT(*) FROM reconsolidation_pending_evidence WHERE window_tenant_id='other'"), 1);
}

TEST(ReconsolidationEngine, Pr1aForkCopiesEveryNonGeneratedColumn) {
    auto adapter = open_fresh(); auto& conn = adapter->connection();
    ReconsolidationEngine engine(*adapter);
    seed_stmt(conn.raw(), "template");
    // Seed immutable perception/scope fields by INSERT, as production does.
    std::string names, values;
    {
        sqlite3_stmt* info = nullptr;
        ASSERT_EQ(sqlite3_prepare_v2(conn.raw(), "PRAGMA table_xinfo(statements)", -1, &info, nullptr), SQLITE_OK);
        StmtHandle columns(info);
        while (sqlite3_step(columns.get()) == SQLITE_ROW) {
            if (sqlite3_column_int(columns.get(), 6) != 0) continue;
            const std::string name = reinterpret_cast<const char*>(sqlite3_column_text(columns.get(), 1));
            if (!names.empty()) { names += ","; values += ","; }
            names += "\"" + name + "\"";
            if (name == "id") values += "'full-evidence'";
            else if (name == "perceived_by_json") values += "'[\"alice\"]'";
            else if (name == "scope_parties_json") values += "'[\"alice\",\"bob\"]'";
            else values += "\"" + name + "\"";
        }
    }
    conn.exec("INSERT INTO statements(" + names + ") SELECT " + values + " FROM statements WHERE id='template'");
    conn.exec("DELETE FROM statements WHERE id='template'");
    conn.exec(R"(UPDATE statements SET evidence_json='[{"engram_ref":"e1"},{"engram_ref":"e2"}]',
        source_spans_json='[{"start":1,"end":8}]', semantic_claim_json='{"actor":"alice"}',
        inferred_at='2026-05-01',valid_from='2026-05-01',valid_to='2026-06-01',
        event_time_start='2026-05-02',event_time_end='2026-05-03',
        confidence_history_json='[0.5]',temporal_anchor_json='{"kind":"date"}',
        derived_from_json='["parent"]',derived_depth=2,
        access_count=3,last_replayed='2026-05-04',replay_count=2,nesting_depth=1,
        visibility_json='["alice"]',retention_policy='retain',last_replay_batch_id='batch',
        consolidation_summary='summary'
        WHERE id='full-evidence')");
    for (int i = 0; i < 2; ++i)
        engine.reconsolidate(conn, "full-evidence", "belief.conflict", "fork-" + std::to_string(i), 1.0, "2026-05-27T10:00:00Z");
    ASSERT_EQ(engine.close_due_windows(conn, "2026-05-27T10:31:00Z"), 1);
    ASSERT_EQ(icol(conn.raw(), "SELECT COUNT(*) FROM statements WHERE supersedes_id='full-evidence'"), 1);
    EXPECT_EQ(scol(conn.raw(), "SELECT provenance FROM statements WHERE supersedes_id='full-evidence'"), "reconsolidation_derived");
    EXPECT_EQ(scol(conn.raw(), "SELECT consolidation_state FROM statements WHERE supersedes_id='full-evidence'"), "consolidated");
    EXPECT_EQ(scol(conn.raw(), "SELECT created_at FROM statements WHERE supersedes_id='full-evidence'"), "2026-05-27T10:31:00Z");
    EXPECT_EQ(scol(conn.raw(), "SELECT updated_at FROM statements WHERE supersedes_id='full-evidence'"), "2026-05-27T10:31:00Z");
    sqlite3_stmt* raw = nullptr;
    ASSERT_EQ(sqlite3_prepare_v2(conn.raw(), "PRAGMA table_xinfo(statements)", -1, &raw, nullptr), SQLITE_OK);
    StmtHandle columns(raw);
    while (sqlite3_step(columns.get()) == SQLITE_ROW) {
        if (sqlite3_column_int(columns.get(), 6) != 0) continue;
        const std::string name = reinterpret_cast<const char*>(sqlite3_column_text(columns.get(), 1));
        if (name == "id" || name == "provenance" || name == "consolidation_state" ||
            name == "supersedes_id" || name == "created_at" || name == "updated_at") continue;
        EXPECT_EQ(icol(conn.raw(), "SELECT o.\"" + name + "\" IS f.\"" + name +
            "\" FROM statements o JOIN statements f ON f.supersedes_id=o.id AND f.tenant_id=o.tenant_id WHERE o.id='full-evidence'"), 1) << name;
    }
}

TEST(ReconsolidationEngine, Pr1aSupportsAllowsOnlyConsolidatedAndReconsolidating) {
    auto adapter = open_fresh(); auto& conn = adapter->connection();
    const char* states[] = {"volatile", "replaying_consolidating", "consolidated", "replaying_reconsolidating", "archived", "forgotten"};
    for (const auto* state : states) {
        seed_stmt(conn.raw(), state);
        conn.exec("UPDATE statements SET consolidation_state='" + std::string(state) + "' WHERE id='" + state + "'");
        starling::store::SqliteStatementStore(conn).set_confidence_consolidated(state, "default", 0.9);
        const bool allowed = std::string(state) == "consolidated" || std::string(state) == "replaying_reconsolidating";
        EXPECT_EQ(scol(conn.raw(), "SELECT consolidation_state FROM statements WHERE id='" + std::string(state) + "'"), allowed ? "consolidated" : state);
        EXPECT_EQ(scol(conn.raw(), "SELECT confidence FROM statements WHERE id='" + std::string(state) + "'"), allowed ? "0.9" : "0.7");
    }
}

TEST(ReconsolidationEngine, Pr1aRecallDoesNotDiluteConflictEvidence) {
    auto adapter = open_fresh(); auto& conn = adapter->connection();
    ReconsolidationEngine engine(*adapter); seed_stmt(conn.raw(), "mixed");
    engine.reconsolidate(conn, "mixed", "belief.conflict", "open", 1.0, "2026-05-27T10:00:00Z");
    engine.reconsolidate(conn, "mixed", "belief.conflict", "conflict", 0.8, "2026-05-27T10:01:00Z");
    for (int i = 0; i < 3; ++i)
        engine.reconsolidate(conn, "mixed", "statement.recalled", "access-" + std::to_string(i), 1.0, "2026-05-27T10:02:00Z");
    const auto agg = aggregate_evidence(conn, "mixed", "default");
    EXPECT_DOUBLE_EQ(agg.strength, 0.8);
    EXPECT_EQ(agg.path, ArbitrationPath::SevereContradict);
}

TEST(ReconsolidationEngine, Pr1aForceCloseAlsoClearsPending) {
    auto adapter = open_fresh(); auto& conn = adapter->connection();
    ReconsolidationEngine engine(*adapter); seed_stmt(conn.raw(), "force");
    for (int i = 0; i < 11; ++i)
        engine.reconsolidate(conn, "force", "belief.conflict", "force-" + std::to_string(i), 1.0, "2026-05-27T10:00:00Z");
    ASSERT_EQ(scol(conn.raw(), "SELECT status FROM reconsolidation_windows WHERE stmt_id='force'"), "closed");
    EXPECT_EQ(icol(conn.raw(), "SELECT COUNT(*) FROM reconsolidation_pending_evidence WHERE window_stmt_id='force'"), 0);
}
