#include "starling/reconsolidation/plastic_window.hpp"

#include "starling/persistence/sqlite_helpers.hpp"
#include "starling/persistence/sqlite_handles.hpp"

#include <algorithm>
#include <cstdio>
#include <ctime>
#include <format>
#include <random>
#include <string>

namespace starling::reconsolidation {

namespace {

using starling::persistence::detail::bind_sv;
using starling::persistence::detail::make_sqlite_error;
using starling::persistence::StmtHandle;

std::string random_hex_32() {
    static thread_local std::mt19937_64 rng{std::random_device{}()};
    const std::uint64_t a = rng();
    const std::uint64_t b = rng();
    char buf[33];
    std::snprintf(buf, sizeof(buf), "%016llx%016llx",
                  static_cast<unsigned long long>(a),
                  static_cast<unsigned long long>(b));
    return std::string(buf, 32);
}

// Parse ISO-8601 UTC string to epoch seconds.
std::time_t parse_iso_epoch(std::string_view iso) {
    std::tm tm{};
    int y, mo, d, h, mi, s;
    if (std::sscanf(std::string(iso).c_str(), "%d-%d-%dT%d:%d:%dZ",
                    &y, &mo, &d, &h, &mi, &s) != 6) return 0;
    tm.tm_year = y - 1900; tm.tm_mon = mo - 1; tm.tm_mday = d;
    tm.tm_hour = h; tm.tm_min = mi; tm.tm_sec = s;
    return timegm(&tm);
}

// Add minutes to an ISO-8601 UTC timestamp.
std::string add_minutes_to_iso(std::string_view iso, int minutes) {
    std::time_t epoch = parse_iso_epoch(iso);
    epoch += static_cast<std::time_t>(minutes) * 60;
    std::tm tm{};
    gmtime_r(&epoch, &tm);
    return std::format("{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z",
                       tm.tm_year + 1900, tm.tm_mon + 1, tm.tm_mday,
                       tm.tm_hour, tm.tm_min, tm.tm_sec);
}

}  // namespace

int adaptive_timeout_minutes(std::string_view modality, int trigger_freq_per_hour) {
    // Base timeout by modality
    int base = 30;
    if (modality == "COMMITS" || modality == "commits")         base = 360;
    else if (modality == "NORM_OUGHT" || modality == "norm_ought") base = 180;
    else if (modality == "KNOWS" || modality == "knows")        base = 60;
    else if (modality == "BELIEVES" || modality == "believes")  base = 30;
    else if (modality == "ASSUMES" || modality == "assumes")    base = 5;

    // High-frequency override (applied after modality)
    if (trigger_freq_per_hour >= 3) return 5;

    // Clamp to [5, 360]
    return std::clamp(base, 5, 360);
}

OpenResult open_or_append(persistence::Connection& conn,
                          std::string_view stmt_id, std::string_view tenant_id,
                          std::string_view event_id, std::string_view event_type,
                          std::string_view payload_hash, double weight,
                          std::string_view modality, std::string_view now_iso) {
    sqlite3* db = conn.raw();
    OpenResult result;

    // Check for existing window
    std::string current_status;
    {
        sqlite3_stmt* raw = nullptr;
        const char* sql =
            "SELECT status FROM reconsolidation_windows "
            "WHERE stmt_id = ?1 AND tenant_id = ?2";
        if (sqlite3_prepare_v2(db, sql, -1, &raw, nullptr) != SQLITE_OK) {
            throw make_sqlite_error(db, "open_or_append: prepare SELECT");
        }
        StmtHandle h(raw);
        bind_sv(h.get(), 1, stmt_id);
        bind_sv(h.get(), 2, tenant_id);
        int rc = sqlite3_step(h.get());
        if (rc == SQLITE_ROW) {
            current_status = reinterpret_cast<const char*>(sqlite3_column_text(h.get(), 0));
        }
        // SQLITE_DONE means no row
    }

    if (current_status == "open") {
        // Append pending evidence
        const std::string evidence_id = random_hex_32();
        {
            sqlite3_stmt* raw = nullptr;
            const char* sql =
                "INSERT INTO reconsolidation_pending_evidence "
                "(id, window_stmt_id, window_tenant_id, event_id, event_type, source_stmt_id, "
                " payload_hash, weight, arrived_at) "
                "VALUES (?1, ?2, ?3, ?4, ?5, NULL, ?6, ?7, ?8)";
            if (sqlite3_prepare_v2(db, sql, -1, &raw, nullptr) != SQLITE_OK) {
                throw make_sqlite_error(db, "open_or_append: prepare INSERT evidence");
            }
            StmtHandle h(raw);
            bind_sv(h.get(), 1, evidence_id);
            bind_sv(h.get(), 2, stmt_id);
            bind_sv(h.get(), 3, tenant_id);
            bind_sv(h.get(), 4, event_id);
            bind_sv(h.get(), 5, event_type);
            bind_sv(h.get(), 6, payload_hash);
            sqlite3_bind_double(h.get(), 7, event_type == "statement.recalled" ? 0.0 : weight);
            bind_sv(h.get(), 8, now_iso);
            if (sqlite3_step(h.get()) != SQLITE_DONE) {
                throw make_sqlite_error(db, "open_or_append: INSERT evidence step");
            }
        }

        // Increment force_close_trigger_count
        {
            sqlite3_stmt* raw = nullptr;
            const char* sql =
                "UPDATE reconsolidation_windows "
                "   SET force_close_trigger_count = force_close_trigger_count + 1 "
                " WHERE stmt_id = ?1 AND tenant_id = ?2";
            if (sqlite3_prepare_v2(db, sql, -1, &raw, nullptr) != SQLITE_OK) {
                throw make_sqlite_error(db, "open_or_append: prepare UPDATE trigger_count");
            }
            StmtHandle h(raw);
            bind_sv(h.get(), 1, stmt_id);
            bind_sv(h.get(), 2, tenant_id);
            if (sqlite3_step(h.get()) != SQLITE_DONE) {
                throw make_sqlite_error(db, "open_or_append: UPDATE trigger_count step");
            }
        }

        // Check pending count; if > 100, evict oldest
        {
            sqlite3_stmt* raw = nullptr;
            const char* sql =
                "SELECT COUNT(*) FROM reconsolidation_pending_evidence "
                " WHERE window_stmt_id = ?1 AND window_tenant_id = ?2";
            if (sqlite3_prepare_v2(db, sql, -1, &raw, nullptr) != SQLITE_OK) {
                throw make_sqlite_error(db, "open_or_append: prepare COUNT evidence");
            }
            StmtHandle h(raw);
            bind_sv(h.get(), 1, stmt_id);
            bind_sv(h.get(), 2, tenant_id);
            if (sqlite3_step(h.get()) != SQLITE_ROW) {
                throw make_sqlite_error(db, "open_or_append: COUNT evidence step");
            }
            int count = sqlite3_column_int(h.get(), 0);

            if (count > kPendingEvidenceMax) {
                // Delete oldest by arrived_at
                sqlite3_stmt* raw2 = nullptr;
                const char* del_sql =
                    "DELETE FROM reconsolidation_pending_evidence "
                    " WHERE id = ("
                    "   SELECT id FROM reconsolidation_pending_evidence "
                    "    WHERE window_stmt_id = ?1 AND window_tenant_id = ?2 "
                    "    ORDER BY arrived_at ASC LIMIT 1"
                    ")";
                if (sqlite3_prepare_v2(db, del_sql, -1, &raw2, nullptr) != SQLITE_OK) {
                    throw make_sqlite_error(db, "open_or_append: prepare DELETE oldest");
                }
                StmtHandle h2(raw2);
                bind_sv(h2.get(), 1, stmt_id);
                bind_sv(h2.get(), 2, tenant_id);
                if (sqlite3_step(h2.get()) != SQLITE_DONE) {
                    throw make_sqlite_error(db, "open_or_append: DELETE oldest step");
                }

                // Increment evicted_count
                sqlite3_stmt* raw3 = nullptr;
                const char* upd_sql =
                    "UPDATE reconsolidation_windows "
                    "   SET evicted_count = evicted_count + 1 "
                    " WHERE stmt_id = ?1 AND tenant_id = ?2";
                if (sqlite3_prepare_v2(db, upd_sql, -1, &raw3, nullptr) != SQLITE_OK) {
                    throw make_sqlite_error(db, "open_or_append: prepare UPDATE evicted_count");
                }
                StmtHandle h3(raw3);
                bind_sv(h3.get(), 1, stmt_id);
                bind_sv(h3.get(), 2, tenant_id);
                if (sqlite3_step(h3.get()) != SQLITE_DONE) {
                    throw make_sqlite_error(db, "open_or_append: UPDATE evicted_count step");
                }
            }
        }

        // Check force_close_trigger_count >= 10 → close
        {
            sqlite3_stmt* raw = nullptr;
            const char* sql =
                "SELECT force_close_trigger_count FROM reconsolidation_windows "
                " WHERE stmt_id = ?1 AND tenant_id = ?2";
            if (sqlite3_prepare_v2(db, sql, -1, &raw, nullptr) != SQLITE_OK) {
                throw make_sqlite_error(db, "open_or_append: prepare SELECT trigger_count");
            }
            StmtHandle h(raw);
            bind_sv(h.get(), 1, stmt_id);
            bind_sv(h.get(), 2, tenant_id);
            if (sqlite3_step(h.get()) == SQLITE_ROW) {
                int count = sqlite3_column_int(h.get(), 0);
                if (count >= kForceCloseTriggerCount) {
                    sqlite3_stmt* raw2 = nullptr;
                    const char* upd_sql =
                        "UPDATE reconsolidation_windows "
                        "   SET status = 'closed' "
                        " WHERE stmt_id = ?1 AND tenant_id = ?2";
                    if (sqlite3_prepare_v2(db, upd_sql, -1, &raw2, nullptr) != SQLITE_OK) {
                        throw make_sqlite_error(db, "open_or_append: prepare force close");
                    }
                    StmtHandle h2(raw2);
                    bind_sv(h2.get(), 1, stmt_id);
                    bind_sv(h2.get(), 2, tenant_id);
                    if (sqlite3_step(h2.get()) != SQLITE_DONE) {
                        throw make_sqlite_error(db, "open_or_append: force close step");
                    }
                    clear_pending_evidence(conn, stmt_id, tenant_id);
                }
            }
        }

        result.appended = true;
        return result;
    } else {
        // No open window: insert new (or reopen closed via INSERT OR REPLACE)
        const int timeout_min = adaptive_timeout_minutes(modality, 0);
        const std::string deadline = add_minutes_to_iso(now_iso, timeout_min);
        const std::string trigger_ids_json =
            std::string("[\"") + std::string(event_id) + "\"]";

        sqlite3_stmt* raw = nullptr;
        const char* sql =
            "INSERT OR REPLACE INTO reconsolidation_windows "
            "(stmt_id, tenant_id, opened_at, close_deadline, "
            " trigger_event_ids_json, force_close_trigger_count, evicted_count, "
            " evicted_summary_hashes_json, status) "
            "VALUES (?1, ?2, ?3, ?4, ?5, 0, 0, '[]', 'open')";
        if (sqlite3_prepare_v2(db, sql, -1, &raw, nullptr) != SQLITE_OK) {
            throw make_sqlite_error(db, "open_or_append: prepare INSERT window");
        }
        StmtHandle h(raw);
        bind_sv(h.get(), 1, stmt_id);
        bind_sv(h.get(), 2, tenant_id);
        bind_sv(h.get(), 3, now_iso);
        bind_sv(h.get(), 4, deadline);
        bind_sv(h.get(), 5, trigger_ids_json);
        if (sqlite3_step(h.get()) != SQLITE_DONE) {
            throw make_sqlite_error(db, "open_or_append: INSERT window step");
        }

        result.opened = true;
        result.close_deadline = deadline;
        return result;
    }
}

std::vector<DueWindow> due_windows(persistence::Connection& conn, std::string_view now_iso) {
    sqlite3* db = conn.raw();
    sqlite3_stmt* raw = nullptr;
    const char* sql =
        "SELECT stmt_id, tenant_id FROM reconsolidation_windows "
        " WHERE status = 'open' AND close_deadline <= ?1";
    if (sqlite3_prepare_v2(db, sql, -1, &raw, nullptr) != SQLITE_OK) {
        throw make_sqlite_error(db, "due_windows: prepare");
    }
    StmtHandle h(raw);
    bind_sv(h.get(), 1, now_iso);

    std::vector<DueWindow> result;
    while (sqlite3_step(h.get()) == SQLITE_ROW) {
        const auto* stmt = sqlite3_column_text(h.get(), 0);
        const auto* tenant = sqlite3_column_text(h.get(), 1);
        result.push_back({
            stmt ? reinterpret_cast<const char*>(stmt) : "",
            tenant ? reinterpret_cast<const char*>(tenant) : "",
        });
    }
    return result;
}

void clear_pending_evidence(persistence::Connection& conn,
                            std::string_view stmt_id, std::string_view tenant_id) {
    sqlite3_stmt* raw = nullptr;
    if (sqlite3_prepare_v2(conn.raw(),
        "DELETE FROM reconsolidation_pending_evidence "
        "WHERE window_stmt_id=?1 AND window_tenant_id=?2", -1, &raw, nullptr) != SQLITE_OK) {
        throw make_sqlite_error(conn.raw(), "clear_pending_evidence: prepare");
    }
    StmtHandle handle(raw);
    bind_sv(handle.get(), 1, stmt_id);
    bind_sv(handle.get(), 2, tenant_id);
    if (sqlite3_step(handle.get()) != SQLITE_DONE) {
        throw make_sqlite_error(conn.raw(), "clear_pending_evidence: step");
    }
}

}  // namespace starling::reconsolidation
