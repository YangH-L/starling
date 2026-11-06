#pragma once
#include "starling/persistence/sqlite_handles.hpp"
#include <filesystem>
#include <stdexcept>
#include <string>
#include <string_view>

namespace starling::persistence {

class SqliteError : public std::runtime_error {
public:
    SqliteError(std::string msg, int code) : runtime_error(std::move(msg)), code_(code) {}
    int code() const noexcept { return code_; }
private:
    int code_;
};

// 只读连接上执行写语句（SQLITE_READONLY 及其扩展码）时抛（裁定 R33）。继承 SqliteError：已有的
// catch (const SqliteError&) 照常接得住；Python 侧是 _core.ReadOnlyWriteRejected（RuntimeError 的子类），看板的
// 只读引擎按类型把它报成 world_read_only。由 make_sqlite_error（Stmt、各处 prepare/step）与 Connection::exec 产生。
class ReadOnlyWriteRejected : public SqliteError {
public:
    using SqliteError::SqliteError;
};

// code 是 SQLITE_READONLY 一族（主码 8，含 SQLITE_READONLY_* 扩展码）时抛 ReadOnlyWriteRejected，否则抛 SqliteError。
[[noreturn]] void throw_sqlite_error(std::string msg, int code);

class Connection {
public:
    // Opens db_path with WAL + foreign_keys=ON + busy_timeout=5000 + synchronous=NORMAL.
    // Use ":memory:" for tests. Creates parent directories as needed when the
    // path is a real file.
    static Connection open(const std::filesystem::path& db_path);

    // 只读打开一个已有的库(spec §6.3):SQLITE_OPEN_READONLY + file:<path>?mode=ro,
    // 不设 journal_mode,不建目录,也不建库文件(不存在时抛 SqliteError)。只设
    // busy_timeout=5000。
    //
    // 库头是 WAL 模式、旁边却没有 -wal 和 -shm 时(写连接已正常关闭),普通 mode=ro
    // 连接会新建这两个文件,所以这种库改用 immutable=1 打开:WAL 里没有内容,主文件
    // 就是最近一次提交。immutable 连接不加锁,也看不到之后的写入,因此这条连接开着
    // 期间不能有写连接打开这个库。其余情况(rollback 日志模式的库,或 -wal / -shm
    // 已经在,通常是写连接开着)走普通 mode=ro,读到写连接最近一次提交。走了哪条
    // 由 immutable() 报告。
    //
    // SQLite 把 -wal / -shm 建在解析后的真实文件旁边,所以库头与旁车都按
    // std::filesystem::canonical(db_path) 查,URI 也用解析后的路径;解析失败时走普通
    // mode=ro(宁可建旁车,也不读漏 WAL 里的提交)。
    static Connection open_read_only(const std::filesystem::path& db_path);

    // open_read_only 交给 sqlite3_open_v2 的 URI（裁定 R28：只读打开世界库的唯一规则）。immutable 判定只在
    // 这里：库头是 WAL、旁边没有 -wal / -shm 时 "?mode=ro&immutable=1"，否则 "?mode=ro"；路径按上面的
    // 规则解析并做百分号编码。world_store 的只读打开与 Python 看板的检视连接（绑定 _core.read_only_uri）
    // 都从这里取 URI，自己不拼。调用方用 SQLITE_OPEN_READONLY | SQLITE_OPEN_URI 打开。
    struct ReadOnlyUri {
        std::string uri;
        bool immutable = false;
    };
    [[nodiscard]] static ReadOnlyUri read_only_uri(const std::filesystem::path& db_path);

    // true = 这条连接是 open_read_only 以 immutable=1 打开的(见上)。close() 之后仍报告
    // 当初走的路径。open() 打开的连接恒为 false。
    [[nodiscard]] bool immutable() const noexcept { return immutable_; }

    // nullptr once close() has run.
    [[nodiscard]] sqlite3* raw() const noexcept { return handle_.get(); }

    // Releases the sqlite handle. Idempotent. After close(), raw() is nullptr,
    // exec() throws SqliteError, and the transaction helpers are no-ops. The
    // caller must have finalized (or be done with) every statement first: a
    // statement still alive keeps the database file open (sqlite3_close_v2).
    void close() noexcept { handle_.reset(); }
    [[nodiscard]] bool is_open() const noexcept { return handle_ != nullptr; }

    // Executes SQL with no parameters. Throws SqliteError on failure.
    void exec(std::string_view sql);

    // BEGIN IMMEDIATE / COMMIT / ROLLBACK helpers.
    void begin_immediate();
    void commit();
    void rollback() noexcept;

    int64_t last_insert_rowid() const noexcept;

private:
    explicit Connection(SqliteHandle handle, bool immutable = false)
        : handle_(std::move(handle)), immutable_(immutable) {}
    SqliteHandle handle_;
    bool immutable_ = false;
};

class TransactionGuard {
public:
    // The outermost guard owns BEGIN IMMEDIATE; nested guards use SAVEPOINTs.
    explicit TransactionGuard(Connection& c);
    ~TransactionGuard();
    void commit();

    TransactionGuard(const TransactionGuard&) = delete;
    TransactionGuard& operator=(const TransactionGuard&) = delete;
    TransactionGuard(TransactionGuard&&) = delete;
    TransactionGuard& operator=(TransactionGuard&&) = delete;

private:
    Connection& conn_;
    std::string savepoint_;
    bool outer_ = false;
    bool active_ = true;
};

}  // namespace starling::persistence
