#pragma once
#include "starling/adapter.hpp"
#include "starling/persistence/connection.hpp"
#include "starling/profile_capability.hpp"
#include <filesystem>
#include <functional>
#include <memory>
#include <string>

namespace starling::persistence {

// SqliteAdapter is the local-store-sqlite Adapter implementation. open()
// creates the connection (WAL + foreign_keys=ON) and runs migrations to the
// latest version before returning, so the schema invariants (idx_statement_id_tenant,
// outbox_sequence_counter row, etc.) are guaranteed before Runtime preflight.
//
// The Adapter base deletes its move-ctor (and copy ops). open() therefore
// returns a std::unique_ptr<SqliteAdapter> so callers can hold the adapter
// without slicing or copying.
class SqliteAdapter : public starling::Adapter {
public:
    static std::unique_ptr<SqliteAdapter> open(const std::filesystem::path& db_path);

    // 只读打开(spec §6.3):Connection::open_read_only,不跑迁移。write_admitted() 恒为
    // false,经 governance::require_write_admission 把守的写函数因此抛 WriteGateRejected;
    // 绕过写门的 SQL 写由 mode=ro 连接拒绝(SqliteError)。close() 不做 checkpoint。
    static std::unique_ptr<SqliteAdapter> open_read_only(const std::filesystem::path& db_path);
    [[nodiscard]] bool read_only() const noexcept { return read_only_; }

    // schema_migrations 的 MAX(version);表不存在时为 0。只读打开 schema 落后的库时
    // 小于 MigrationRunner::latest_embedded_version()。Throws std::logic_error after close().
    [[nodiscard]] int schema_version() const;

    starling::ProfileCapability declare_capability() const override;
    bool check_final_query(const std::string& sql) const override;
    [[nodiscard]] bool has_index(std::string_view name);

    // 写门钩子(P3.c write-gate):未设 → 放行(behavior-neutral by construction)。
    // 返回 bool 避免 persistence 依赖 governance。production Runtime 经
    // governance::install_write_gate 设一次:钩子内读 supervisor 健康态。
    // 只读适配器不看钩子,恒拒。
    void set_write_admit(std::function<bool()> hook) { write_admit_ = std::move(hook); }
    [[nodiscard]] bool write_admitted() const {
        return !read_only_ && (!write_admit_ || write_admit_());
    }

    // Throws std::logic_error("adapter closed") after close().
    Connection& connection();

    // Merges the WAL into the main file (PRAGMA wal_checkpoint(TRUNCATE)) and
    // releases the sqlite handle. Idempotent. If the checkpoint throws, the
    // handle is still released and the error propagates (the WAL stays on disk
    // and is recovered by the next open). Not a write, and not thread-safe
    // against concurrent users of connection(): the host must have no call in
    // flight and no further call to make (starling.Memory.close does this).
    // The checkpoint cannot truncate the WAL while another connection to the
    // same file holds a read transaction; SQLite then leaves the WAL for the
    // last connection to close (or the next open) to merge.
    void close();
    const std::filesystem::path& db_path() const noexcept { return db_path_; }

private:
    SqliteAdapter(Connection conn, std::filesystem::path path, bool read_only)
        : conn_(std::move(conn)), db_path_(std::move(path)), read_only_(read_only) {}
    Connection conn_;
    std::filesystem::path db_path_;
    std::function<bool()> write_admit_;
    bool read_only_ = false;
};

}  // namespace starling::persistence
