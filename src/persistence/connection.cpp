#include "starling/persistence/connection.hpp"

#include <array>
#include <atomic>
#include <fstream>
#include <stdexcept>
#include <system_error>

namespace starling::persistence {

namespace {

// URI 里 %、?、# 有特殊含义,路径里出现时必须百分号编码。
std::string uri_escape_path(const std::string& path) {
    std::string out;
    out.reserve(path.size());
    for (const char chr : path) {
        switch (chr) {
            case '%': out += "%25"; break;
            case '?': out += "%3f"; break;
            case '#': out += "%23"; break;
            default: out += chr; break;
        }
    }
    return out;
}

// 库头第 19 字节是读版本:2 = WAL 模式(SQLite 据此决定是否走 WAL 读)。
// 读不到完整的头(文件不存在、空文件)按非 WAL 处理,交给 sqlite3_open_v2 报错。
bool header_is_wal(const std::filesystem::path& db_path) {
    constexpr std::size_t kReadVersionOffset = 19;
    std::array<char, kReadVersionOffset + 1> header{};
    std::ifstream input(db_path, std::ios::binary);
    if (!input.read(header.data(), static_cast<std::streamsize>(header.size()))) {
        return false;
    }
    return header.back() == 2;   // 数组只读到读版本那一字节为止
}

// 查不清(权限等错误)时按存在处理:走普通 mode=ro,宁可建旁车也不读漏 WAL 里的提交。
bool sidecar_present(const std::filesystem::path& db_path, const char* suffix) {
    std::error_code err;
    const bool found = std::filesystem::exists(db_path.string() + suffix, err);
    return found || static_cast<bool>(err);
}

}  // namespace

Connection Connection::open_read_only(const std::filesystem::path& db_path) {
    // 旁车跟着真实文件走:符号链接旁边没有 -wal 不等于真实文件旁边没有。
    std::error_code resolve_err;
    const std::filesystem::path resolved = std::filesystem::canonical(db_path, resolve_err);
    const bool frozen_wal = !resolve_err && header_is_wal(resolved) &&
                            !sidecar_present(resolved, "-wal") &&
                            !sidecar_present(resolved, "-shm");
    const std::filesystem::path& target = resolve_err ? db_path : resolved;
    const std::string uri = "file:" + uri_escape_path(target.string()) +
                            (frozen_wal ? "?mode=ro&immutable=1" : "?mode=ro");
    sqlite3* raw = nullptr;
    const int code = sqlite3_open_v2(
        uri.c_str(), &raw, SQLITE_OPEN_READONLY | SQLITE_OPEN_URI | SQLITE_OPEN_FULLMUTEX,
        nullptr);
    SqliteHandle handle(raw);
    if (code != SQLITE_OK) {
        throw SqliteError(std::string("sqlite3_open_v2 (read-only) failed: ") +
            (raw != nullptr ? sqlite3_errmsg(raw) : "alloc failure"), code);
    }
    Connection conn(std::move(handle), frozen_wal);
    conn.exec("PRAGMA busy_timeout = 5000");
    return conn;
}

Connection Connection::open(const std::filesystem::path& db_path) {
    if (db_path != ":memory:" && db_path.has_parent_path()) {
        std::filesystem::create_directories(db_path.parent_path());
    }
    sqlite3* raw = nullptr;
    const int rc = sqlite3_open_v2(
        db_path.string().c_str(), &raw,
        SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX,
        nullptr);
    SqliteHandle h(raw);
    if (rc != SQLITE_OK) {
        throw SqliteError(std::string("sqlite3_open_v2 failed: ") +
            (raw ? sqlite3_errmsg(raw) : "alloc failure"), rc);
    }
    Connection c(std::move(h));
    c.exec("PRAGMA foreign_keys = ON");
    c.exec("PRAGMA journal_mode = WAL");
    c.exec("PRAGMA synchronous = NORMAL");
    c.exec("PRAGMA busy_timeout = 5000");
    return c;
}

void Connection::exec(std::string_view sql) {
    if (!handle_) {
        throw SqliteError("exec failed: connection closed", SQLITE_MISUSE);
    }
    char* err = nullptr;
    if (sqlite3_exec(handle_.get(), std::string(sql).c_str(),
                     nullptr, nullptr, &err) != SQLITE_OK) {
        std::string msg = err ? err : "unknown sqlite_exec error";
        sqlite3_free(err);
        throw SqliteError("exec failed: " + msg, sqlite3_errcode(handle_.get()));
    }
}

void Connection::begin_immediate() { exec("BEGIN IMMEDIATE"); }
void Connection::commit()          { exec("COMMIT"); }
void Connection::rollback() noexcept {
    if (!handle_) {
        return;
    }
    sqlite3_exec(handle_.get(), "ROLLBACK", nullptr, nullptr, nullptr);
}

int64_t Connection::last_insert_rowid() const noexcept {
    if (!handle_) {
        return 0;
    }
    return sqlite3_last_insert_rowid(handle_.get());
}

TransactionGuard::TransactionGuard(Connection& c) : conn_(c) {
    // raw() is nullptr after Connection::close(), and sqlite3_get_autocommit
    // dereferences its argument unless SQLITE_ENABLE_API_ARMOR is built in
    // (it is not). Fail with an exception instead of a segfault.
    if (!conn_.is_open()) {
        throw std::logic_error("connection closed");
    }
    if (sqlite3_get_autocommit(conn_.raw()) != 0) {
        outer_ = true;
        conn_.begin_immediate();
        return;
    }
    static std::atomic<unsigned long long> sequence{0};
    savepoint_ = "starling_tx_" + std::to_string(++sequence);
    conn_.exec("SAVEPOINT " + savepoint_);
}

TransactionGuard::~TransactionGuard() {
    if (!active_) return;
    if (outer_) {
        conn_.rollback();
        return;
    }
    sqlite3_exec(conn_.raw(), ("ROLLBACK TO SAVEPOINT " + savepoint_).c_str(),
                 nullptr, nullptr, nullptr);
    sqlite3_exec(conn_.raw(), ("RELEASE SAVEPOINT " + savepoint_).c_str(),
                 nullptr, nullptr, nullptr);
}

void TransactionGuard::commit() {
    if (outer_) conn_.commit();
    else conn_.exec("RELEASE SAVEPOINT " + savepoint_);
    active_ = false;
}

}  // namespace starling::persistence
