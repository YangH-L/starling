#pragma once
#include "starling/persistence/sqlite_handles.hpp"
#include <cstdint>
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

// 只读连接要不要冻结（裁定 R36）。immutable 连接不加锁、看不到之后的写入，所以只能用在这条连接活着期间一定没有
// 写连接的文件上，由调用方显式选：
//   Shared（默认）：永远普通 mode=ro。别的连接可以照常写，读事务看到的是一致的快照，代价是 WAL 头的库旁边会
//     新建 -wal / -shm。
//   Frozen：库头是 WAL、旁边没有 -wal / -shm 时加 immutable=1（不建旁车），其余情况仍是普通 mode=ro。只用于
//     (1) 这次操作自己拥有、还没登记的文件（partial、复制品、登记前的新世界目录、刚写完的快照文件）；
//     (2) C++ 判为只读打开的已登记世界（governance::read_only_freeze_for，即 decide_open_mode 为 ReadOnly）。
// 默认是 Shared：漏改的调用点最坏只是种子旁边多出旁车，不会读到撕裂的数据。
enum class ReadOnlyFreeze : std::uint8_t { Shared, Frozen };

class Connection {
public:
    // Opens db_path with WAL + foreign_keys=ON + busy_timeout=5000 + synchronous=NORMAL.
    // Use ":memory:" for tests. Creates parent directories as needed when the
    // path is a real file.
    static Connection open(const std::filesystem::path& db_path);

    // 只读打开一个已有的库(spec §6.3):SQLITE_OPEN_READONLY + file:<path>?mode=ro,
    // 不设 journal_mode,不建目录,也不建库文件(不存在时抛 SqliteError)。只设
    // busy_timeout=5000。URI 取自 read_only_uri(db_path, freeze);走了哪条由 immutable() 报告。
    static Connection open_read_only(const std::filesystem::path& db_path,
                                     ReadOnlyFreeze freeze = ReadOnlyFreeze::Shared);

    // open_read_only 交给 sqlite3_open_v2 的 URI（裁定 R36，取代 R28 的「唯一规则」）。world_store 的只读打开与
    // Python 看板的检视连接（绑定 _core.read_only_uri）都从这里取 URI，自己不拼；调用方用
    // SQLITE_OPEN_READONLY | SQLITE_OPEN_URI 打开。
    //   Shared（默认）："file:<path>?mode=ro"。
    //   Frozen：库头是 WAL、旁边没有 -wal / -shm（写连接已正常关闭）时 "?mode=ro&immutable=1"：WAL 里没有内容，
    //     主文件就是最近一次提交，普通 mode=ro 会在旁边新建这两个文件。其余情况（rollback 日志模式的库，或旁车
    //     已经在）仍是普通 mode=ro。何时可以选 Frozen 见 ReadOnlyFreeze。
    // SQLite 把 -wal / -shm 建在解析后的真实文件旁边,所以库头与旁车都按 std::filesystem::canonical(db_path) 查,
    // URI 也用解析后的路径;解析失败时走普通 mode=ro(宁可建旁车,也不读漏 WAL 里的提交)。
    struct ReadOnlyUri {
        std::string uri;
        bool immutable = false;
    };
    [[nodiscard]] static ReadOnlyUri read_only_uri(const std::filesystem::path& db_path,
                                                   ReadOnlyFreeze freeze = ReadOnlyFreeze::Shared);

    // true = 这条连接是 open_read_only 以 immutable=1 打开的(只有 Frozen 才会)。close() 之后仍报告
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
