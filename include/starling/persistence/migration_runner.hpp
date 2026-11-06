#pragma once
#include <sqlite3.h>
#include <stdexcept>
#include <string>
#include <vector>

namespace starling::persistence {

struct AppliedMigration {
    int version;
    std::string name;
    std::string applied_at;
    std::string checksum;
};

class MigrationDriftError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

class MigrationRunner {
public:
    explicit MigrationRunner(sqlite3* db);

    // Idempotent. Runs every embedded migration whose version is not yet in
    // schema_migrations. For already-applied versions, verifies that the
    // recorded checksum matches the embedded SQL — if not, throws
    // MigrationDriftError without applying anything.
    void migrate_to_latest();

    // Returns rows from schema_migrations ordered by version.
    std::vector<AppliedMigration> applied() const;

    // 本构建内嵌的最高迁移版本(migrate_to_latest 之后 schema_migrations 的 MAX(version))。
    static int latest_embedded_version();

    // sha256 hex of the SQL string. Exposed for tests.
    static std::string checksum_of(std::string_view sql);

private:
    sqlite3* db_;  // not owned
};

}  // namespace starling::persistence
