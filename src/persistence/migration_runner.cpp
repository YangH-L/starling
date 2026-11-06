#include "starling/persistence/migration_runner.hpp"
#include "starling/crypto/sha256.hpp"
#include "starling/migrations.inc"

#include <algorithm>
#include <chrono>
#include <ctime>
#include <iomanip>
#include <sstream>

namespace starling::persistence {

namespace {

std::string now_iso8601_utc() {
    using namespace std::chrono;
    const auto t = system_clock::to_time_t(system_clock::now());
    std::tm tm{};
    gmtime_r(&t, &tm);
    std::ostringstream oss;
    oss << std::put_time(&tm, "%Y-%m-%dT%H:%M:%SZ");
    return oss.str();
}

void exec_or_throw(sqlite3* db, const char* sql) {
    char* err = nullptr;
    if (sqlite3_exec(db, sql, nullptr, nullptr, &err) != SQLITE_OK) {
        std::string msg = err ? err : "unknown sqlite_exec error";
        sqlite3_free(err);
        throw std::runtime_error("migration_exec failed: " + msg);
    }
}

}  // namespace

std::string MigrationRunner::checksum_of(std::string_view sql) {
    return starling::crypto::sha256_hex(sql);
}

MigrationRunner::MigrationRunner(sqlite3* db) : db_(db) {}

int MigrationRunner::latest_embedded_version() {
    int latest = 0;
    for (const auto& mig : detail::kEmbeddedMigrations) {
        latest = std::max(latest, mig.version);
    }
    return latest;
}

void MigrationRunner::migrate_to_latest() {
    exec_or_throw(db_,
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
        "applied_at TEXT NOT NULL, checksum TEXT NOT NULL)");

    for (const auto& mig : detail::kEmbeddedMigrations) {
        const std::string expected = checksum_of(mig.sql);

        sqlite3_stmt* stmt = nullptr;
        sqlite3_prepare_v2(db_,
            "SELECT checksum FROM schema_migrations WHERE version = ?",
            -1, &stmt, nullptr);
        sqlite3_bind_int(stmt, 1, mig.version);
        const int rc = sqlite3_step(stmt);
        if (rc == SQLITE_ROW) {
            const std::string recorded = reinterpret_cast<const char*>(
                sqlite3_column_text(stmt, 0));
            sqlite3_finalize(stmt);
            if (recorded != expected) {
                throw MigrationDriftError(
                    "migration drift: version " + std::to_string(mig.version) +
                    " recorded=" + recorded + " expected=" + expected);
            }
            continue;
        }
        sqlite3_finalize(stmt);

        exec_or_throw(db_, "BEGIN IMMEDIATE");
        try {
            exec_or_throw(db_, mig.sql);
            sqlite3_stmt* ins = nullptr;
            sqlite3_prepare_v2(db_,
                "INSERT INTO schema_migrations(version,name,applied_at,checksum) "
                "VALUES(?,?,?,?)", -1, &ins, nullptr);
            const std::string ts = now_iso8601_utc();
            sqlite3_bind_int(ins, 1, mig.version);
            sqlite3_bind_text(ins, 2, mig.name, -1, SQLITE_TRANSIENT);
            sqlite3_bind_text(ins, 3, ts.c_str(), -1, SQLITE_TRANSIENT);
            sqlite3_bind_text(ins, 4, expected.c_str(), -1, SQLITE_TRANSIENT);
            if (sqlite3_step(ins) != SQLITE_DONE) {
                sqlite3_finalize(ins);
                throw std::runtime_error("schema_migrations insert failed");
            }
            sqlite3_finalize(ins);
            exec_or_throw(db_, "COMMIT");
        } catch (...) {
            sqlite3_exec(db_, "ROLLBACK", nullptr, nullptr, nullptr);
            throw;
        }
    }
}

std::vector<AppliedMigration> MigrationRunner::applied() const {
    std::vector<AppliedMigration> out;
    sqlite3_stmt* stmt = nullptr;
    sqlite3_prepare_v2(db_,
        "SELECT version,name,applied_at,checksum FROM schema_migrations "
        "ORDER BY version", -1, &stmt, nullptr);
    while (sqlite3_step(stmt) == SQLITE_ROW) {
        out.push_back({
            sqlite3_column_int(stmt, 0),
            reinterpret_cast<const char*>(sqlite3_column_text(stmt, 1)),
            reinterpret_cast<const char*>(sqlite3_column_text(stmt, 2)),
            reinterpret_cast<const char*>(sqlite3_column_text(stmt, 3)),
        });
    }
    sqlite3_finalize(stmt);
    return out;
}

}  // namespace starling::persistence
