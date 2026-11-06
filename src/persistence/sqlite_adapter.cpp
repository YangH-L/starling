#include "starling/persistence/sqlite_adapter.hpp"
#include "starling/final_query_assertion.hpp"
#include "starling/persistence/migration_runner.hpp"
#include "starling/persistence/stmt.hpp"

#include <stdexcept>

namespace starling::persistence {

std::unique_ptr<SqliteAdapter> SqliteAdapter::open(
        const std::filesystem::path& db_path) {
    auto conn = Connection::open(db_path);
    MigrationRunner(conn.raw()).migrate_to_latest();
    // unique_ptr<...>(new SqliteAdapter(...)) because the ctor is private —
    // std::make_unique can't reach a private ctor without a friend declaration.
    return std::unique_ptr<SqliteAdapter>(
        new SqliteAdapter(std::move(conn), db_path, /*read_only=*/false));
}

std::unique_ptr<SqliteAdapter> SqliteAdapter::open_read_only(
        const std::filesystem::path& db_path) {
    auto conn = Connection::open_read_only(db_path);
    return std::unique_ptr<SqliteAdapter>(
        new SqliteAdapter(std::move(conn), db_path, /*read_only=*/true));
}

int SqliteAdapter::schema_version() const {
    if (!conn_.is_open()) {
        throw std::logic_error("adapter closed");
    }
    Stmt table(conn_.raw(),
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'");
    if (!table.step_row()) {
        return 0;
    }
    Stmt query(conn_.raw(), "SELECT COALESCE(MAX(version), 0) FROM schema_migrations");
    if (!query.step_row()) {
        return 0;
    }
    return sqlite3_column_int(query.raw(), 0);
}

bool SqliteAdapter::schema_behind() const {
    return schema_version() < MigrationRunner::latest_embedded_version();
}

Connection& SqliteAdapter::connection() {
    if (!conn_.is_open()) {
        throw std::logic_error("adapter closed");
    }
    return conn_;
}

void SqliteAdapter::close() {
    if (!conn_.is_open()) {
        return;
    }
    if (read_only_) {   // 只读连接不能 checkpoint,WAL 留给写连接合并
        conn_.close();
        return;
    }
    try {
        conn_.exec("PRAGMA wal_checkpoint(TRUNCATE)");
    } catch (...) {
        conn_.close();
        throw;
    }
    conn_.close();
}

starling::ProfileCapability SqliteAdapter::declare_capability() const {
    starling::ProfileCapability cap;
    cap.profile_name                  = "local-store-sqlite";
    cap.relational_backend            = "sqlite";
    cap.vector_backend                = "none";          // P1: no vectors
    cap.graph_backend                 = "none";          // P1: edges via statement_edges
    cap.c_plus_plus_core              = true;
    cap.cross_partition_transaction   = true;            // single SQLite db; trivially yes
    cap.transactional_outbox          = true;
    cap.consumer_checkpoint           = true;
    cap.tenant_isolation              = "storage_enforced";  // every P1 query joins tenant_id
    cap.engram_per_record_key         = false;           // KMS lands in M0.3
    cap.engram_refcount               = true;
    cap.projection_index_supported    = false;
    cap.dimension_versions_supported  = false;
    cap.testing_helper_marker         = false;
    return cap;
}

bool SqliteAdapter::check_final_query(const std::string& sql) const {
    return starling::is_final_query_safe(sql);
}

// Legacy compatibility probe: returns whether a named SQLite index EXISTS.
// It proves existence only — NOT tenant-isolation semantics (the (id, tenant_id)
// composite-key invariant). Kept because TC-NEW-PREFLIGHT pins idx_statement_id_tenant.
bool SqliteAdapter::has_index(std::string_view name) {
    // Non-const to match the persistence/bus store convention (e.g. ConsumerCheckpoint
    // read methods).
    Stmt stmt(connection().raw(), "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?1 LIMIT 1");
    stmt.bind(name);
    return stmt.step_row();
}

}  // namespace starling::persistence
