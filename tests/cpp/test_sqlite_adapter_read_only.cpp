// test_sqlite_adapter_read_only.cpp — 只读打开路径(spec §6.3)。钉住:不跑迁移、不改
// 主文件字节、不新建 -wal/-shm、不建目录、写入口一律 WriteGateRejected;写连接开着时
// 读到它最近一次提交。库都经正常写路径(SqliteAdapter::open)建出,零 LLM、零网络。

#include "starling/persistence/sqlite_adapter.hpp"

#include "starling/crypto/sha256.hpp"
#include "starling/extractor/fake_llm_adapter.hpp"
#include "starling/governance/write_gate.hpp"
#include "starling/memory/memory_ops.hpp"
#include "starling/persistence/migration_runner.hpp"
#include "starling/persistence/stmt.hpp"

#include <gtest/gtest.h>
#include <sqlite3.h>
#include <unistd.h>

#include <atomic>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <memory>
#include <set>
#include <string>
#include <string_view>
#include <system_error>

namespace starling::persistence {

namespace {

namespace fs = std::filesystem;

constexpr const char* kCannedJson =
    R"JSON([{"holder":"self","holder_perspective":"FIRST_PERSON","subject":"Bob","predicate":"responsible_for","object":"auth","modality":"BELIEVES","polarity":"POS","nesting_depth":0}])JSON";

// 每个用例一个独立的临时目录,析构时删除。
class TempDir {
public:
    TempDir() {
        static std::atomic<int> seq{0};
        path_ = fs::temp_directory_path() /
                ("starling-ro-" + std::to_string(::getpid()) + "-" + std::to_string(++seq));
        fs::remove_all(path_);
        fs::create_directories(path_);
    }
    ~TempDir() { std::error_code err; fs::remove_all(path_, err); }
    TempDir(const TempDir&) = delete;
    TempDir& operator=(const TempDir&) = delete;
    TempDir(TempDir&&) = delete;
    TempDir& operator=(TempDir&&) = delete;
    [[nodiscard]] const fs::path& path() const { return path_; }
private:
    fs::path path_;
};

std::string file_sha256(const fs::path& file) {
    std::ifstream input(file, std::ios::binary);
    const std::string bytes{std::istreambuf_iterator<char>(input),
                            std::istreambuf_iterator<char>()};
    return crypto::sha256_hex(bytes);
}

// 库旁边现有的 SQLite 旁车文件名。
std::set<std::string> sidecars(const fs::path& db) {
    std::set<std::string> found;
    for (const char* suffix : {"-wal", "-shm"}) {
        const fs::path file = db.string() + suffix;
        if (fs::exists(file)) { found.insert(file.filename().string()); }
    }
    return found;
}

// after 里每一个旁车都已在 before 里(集合没有变大)。
void expect_no_new_sidecars(const std::set<std::string>& before,
                            const std::set<std::string>& after) {
    for (const auto& name : after) {
        EXPECT_EQ(before.count(name), 1U) << name << " appeared after the read-only open";
    }
}

int max_applied_version(Connection& conn) {
    Stmt query(conn.raw(), "SELECT MAX(version) FROM schema_migrations");
    EXPECT_TRUE(query.step_row());
    return sqlite3_column_int(query.raw(), 0);
}

void drop_newest_migration_row(Connection& conn) {
    conn.exec("DELETE FROM schema_migrations "
              "WHERE version = (SELECT MAX(version) FROM schema_migrations)");
}

// 经正常写路径建一个迁移到最新的库;schema_behind 时删掉 schema_migrations 的最后一行,
// 模拟旧版 Starling 写出的库。关闭写连接后返回库里记的最高版本。
int make_library(const fs::path& db, bool schema_behind) {
    auto writer = SqliteAdapter::open(db);
    if (schema_behind) { drop_newest_migration_row(writer->connection()); }
    const int version = max_applied_version(writer->connection());
    writer->close();
    return version;
}

memoryops::RememberParams params(std::string_view text) {
    memoryops::RememberParams out;
    out.tenant_id          = "default";
    out.holder_id          = "cog-self";
    out.adapter_name       = "facade";
    out.source_prefix      = "mem-";
    out.created_at_iso8601 = "2026-11-06T10:00:00Z";
    out.payload.assign(text.begin(), text.end());
    return out;
}

}  // namespace

TEST(SqliteAdapterReadOnly, OpenDoesNotMigrateOrChangeBytes) {
    const TempDir dir;
    const fs::path db = dir.path() / "world.db";
    const int behind = make_library(db, /*schema_behind=*/true);
    ASSERT_LT(behind, MigrationRunner::latest_embedded_version());
    const auto sidecars_before = sidecars(db);
    const std::string sha_before = file_sha256(db);

    auto reader = SqliteAdapter::open_read_only(db);
    EXPECT_TRUE(reader->read_only());
    EXPECT_EQ(reader->schema_version(), behind);   // 打开没有补跑迁移
    EXPECT_LT(reader->schema_version(), MigrationRunner::latest_embedded_version());
    reader->close();
    reader.reset();

    EXPECT_EQ(file_sha256(db), sha_before);
    expect_no_new_sidecars(sidecars_before, sidecars(db));
}

TEST(SqliteAdapterReadOnly, WritesAreRejected) {
    const TempDir dir;
    const fs::path db = dir.path() / "world.db";
    (void)make_library(db, /*schema_behind=*/false);
    const auto sidecars_before = sidecars(db);
    const std::string sha_before = file_sha256(db);

    auto reader = SqliteAdapter::open_read_only(db);
    EXPECT_FALSE(reader->write_admitted());
    // 装上一个放行的写门钩子(supervisor READY)也不能打开只读适配器的写入。
    reader->set_write_admit([] { return true; });
    EXPECT_FALSE(reader->write_admitted());

    extractor::FakeLLMAdapter llm;
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});
    EXPECT_THROW((void)memoryops::remember(*reader, llm, "", params("Bob owns auth")),
                 governance::WriteGateRejected);
    // 绕过写门直接发 SQL,连接本身也拒绝(mode=ro)。
    EXPECT_THROW(reader->connection().exec("DELETE FROM schema_migrations"), SqliteError);
    reader->close();
    reader.reset();

    EXPECT_EQ(file_sha256(db), sha_before);
    expect_no_new_sidecars(sidecars_before, sidecars(db));
}

TEST(SqliteAdapterReadOnly, ReadsLatestCommitWhileWriterOpen) {
    const TempDir dir;
    const fs::path db = dir.path() / "world.db";
    auto writer = SqliteAdapter::open(db);
    writer->connection().exec("PRAGMA wal_checkpoint(TRUNCATE)");
    // 这次提交只在 WAL 里,主文件还是最新版本。
    drop_newest_migration_row(writer->connection());
    const int behind = max_applied_version(writer->connection());
    const auto sidecars_before = sidecars(db);
    ASSERT_EQ(sidecars_before.size(), 2U);

    auto reader = SqliteAdapter::open_read_only(db);
    EXPECT_EQ(reader->schema_version(), behind);
    EXPECT_NO_THROW(reader->close());   // 只读连接不做 checkpoint
    reader.reset();
    expect_no_new_sidecars(sidecars_before, sidecars(db));
    writer->close();
}

TEST(SqliteAdapterReadOnly, RollbackJournalLibraryOpensWithoutSidecars) {
    // VACUUM INTO 的产物(分叉、快照)头部是 rollback 日志模式,走普通 mode=ro 连接。
    const TempDir dir;
    const fs::path source = dir.path() / "source.db";
    const fs::path copy = dir.path() / "copy.db";
    auto writer = SqliteAdapter::open(source);
    writer->connection().exec("VACUUM INTO '" + copy.string() + "'");
    writer->close();
    const std::string sha_before = file_sha256(copy);

    auto reader = SqliteAdapter::open_read_only(copy);
    EXPECT_EQ(reader->schema_version(), MigrationRunner::latest_embedded_version());
    reader->close();
    reader.reset();

    EXPECT_EQ(file_sha256(copy), sha_before);
    EXPECT_TRUE(sidecars(copy).empty());
}

TEST(SqliteAdapterReadOnly, MissingPathIsNotCreated) {
    const TempDir dir;
    const fs::path missing_dir = dir.path() / "absent";
    EXPECT_THROW((void)SqliteAdapter::open_read_only(missing_dir / "world.db"), SqliteError);
    EXPECT_FALSE(fs::exists(missing_dir));

    const fs::path missing_file = dir.path() / "world.db";
    EXPECT_THROW((void)SqliteAdapter::open_read_only(missing_file), SqliteError);
    EXPECT_FALSE(fs::exists(missing_file));
}

TEST(SqliteAdapterReadOnly, SchemaVersionIsZeroWithoutMigrationsTable) {
    const TempDir dir;
    const fs::path db = dir.path() / "plain.db";
    {
        sqlite3* raw = nullptr;
        ASSERT_EQ(sqlite3_open(db.string().c_str(), &raw), SQLITE_OK);
        const SqliteHandle handle(raw);
        ASSERT_EQ(sqlite3_exec(raw, "CREATE TABLE notes(body TEXT)", nullptr, nullptr, nullptr),
                  SQLITE_OK);
    }
    auto reader = SqliteAdapter::open_read_only(db);
    EXPECT_EQ(reader->schema_version(), 0);
}

}  // namespace starling::persistence
