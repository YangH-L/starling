// test_memory_events.cpp — events 表(迁移 0038,0039 起按 holder 区分)。读侧:next_seq 与
// pending_events,直接插行、不经写侧。写侧:remember_event 的四条主路径(新 key、同文本
// 已提交、异文本冲突、补齐不抽取),LLM 用 FakeLLMAdapter,零网络。末尾是 0039 的回填、
// 旧行认领、同 key 两个 holder、放弃时点名主人与空 holder 拒绝的用例。

#include "starling/memory/memory_events.hpp"

#include "starling/crypto/sha256.hpp"
#include "starling/extractor/fake_llm_adapter.hpp"
#include "starling/memory/memory_digest.hpp"
#include "starling/memory/memory_ops.hpp"
#include "starling/persistence/migration_runner.hpp"
#include "starling/persistence/sqlite_adapter.hpp"
#include "starling/persistence/stmt.hpp"

#include <gtest/gtest.h>
#include <nlohmann/json.hpp>

#include <algorithm>
#include <cstdint>
#include <functional>
#include <memory>
#include <set>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace starling::memoryops {

namespace {

std::unique_ptr<persistence::SqliteAdapter> make_adapter() {
    auto a = persistence::SqliteAdapter::open(":memory:");
    persistence::MigrationRunner(a->connection().raw()).migrate_to_latest();
    return a;
}

// event_params() 的 holder。直接插的行也记在它名下,所以 abandon_event 等按 holder 的调用
// 走的是「自己的行」这条路,不是旧行认领。
constexpr const char* kHolder = "cog-self";

void insert_event(persistence::Connection& conn, std::string_view tenant, std::string_view key,
                  std::int64_t seq, std::string_view state, std::string_view interlocutor = "bob",
                  std::string_view failure_category = "", std::string_view holder = kHolder) {
    persistence::Stmt s(conn.raw(),
        "INSERT INTO events(tenant_id,holder_id,key,text,interlocutor,text_sha256,seq,state,"
        "failure_category) VALUES(?1,?2,?3,?4,?5,'h',?6,?7,?8)");
    const std::string text = "text-" + std::string(key);
    s.bind(tenant, holder, key, text, interlocutor, seq, state, failure_category);
    s.step_done();
}

constexpr const char* kCannedJson =
    R"JSON([{"holder":"self","holder_perspective":"FIRST_PERSON","subject":"Bob","predicate":"responsible_for","object":"auth","modality":"BELIEVES","polarity":"POS","nesting_depth":0}])JSON";

constexpr const char* kEpoch = "2026-01-01T00:00:00Z";

// remember_event 的基底参数:key 与 text 另传(key 的前缀由 remember_event 自己加,
// payload 不在这里填),时刻由 seq 从 epoch 推出,created_at_iso8601 也不在这里填。
RememberParams event_params() {
    RememberParams p;
    p.tenant_id     = "default";
    p.holder_id     = kHolder;
    p.interlocutor  = "bob";
    p.adapter_name  = "facade";
    p.source_prefix = "mem-";
    return p;
}

int row_count(persistence::Connection& conn, const char* sql) {
    persistence::Stmt q(conn.raw(), sql);
    EXPECT_TRUE(q.step_row());
    return sqlite3_column_int(q.raw(), 0);
}

std::string state_of(persistence::Connection& conn, std::string_view tenant,
                     std::string_view key) {
    persistence::Stmt q(conn.raw(),
                        "SELECT state FROM events WHERE tenant_id=?1 AND key=?2");
    q.bind(tenant, key);
    if (!q.step_row()) return {};
    // NOLINTNEXTLINE(cppcoreguidelines-pro-type-reinterpret-cast)
    const auto* text = reinterpret_cast<const char*>(sqlite3_column_text(q.raw(), 0));
    return text != nullptr ? std::string(text) : std::string();
}

std::string event_state(persistence::Connection& conn, std::string_view key) {
    return state_of(conn, "default", key);
}

// 抽取总是失败的适配器:用来证明某条路径一次 LLM 都没调(调了就会 extraction_failed)。
class AlwaysFailingLLMAdapter final : public extractor::LLMAdapter {
public:
    extractor::LLMResponse extract(std::string_view, std::string_view) override {
        return {.raw_xml = "", .ok = false, .error = "forced extraction failure"};
    }
};

// 在第 2 段(抽取,事务外)里对同一个 key 动手的适配器:确定性地造出「写入在途中,
// 行被改成终态或被删掉」这个竞态,不用线程、不靠时序。第一次 extract 时回调一次,
// 之后照常返回 response(abandon_event 本身幂等,但只跑一次更贴近真实时序)。
class MutateRowDuringExtractAdapter final : public extractor::LLMAdapter {
public:
    using Hook = std::function<void()>;
    MutateRowDuringExtractAdapter(extractor::LLMResponse response, Hook hook)
        : response_(std::move(response)), hook_(std::move(hook)) {}

    extractor::LLMResponse extract(std::string_view, std::string_view) override {
        if (!fired_) {
            fired_ = true;
            hook_();
        }
        return response_;
    }

private:
    extractor::LLMResponse response_;
    Hook                   hook_;
    bool                   fired_ = false;
};

}  // namespace

TEST(MemoryEvents, NextSeqIsMaxPlusOnePerTenant) {
    auto adapter = make_adapter();
    auto& conn = adapter->connection();
    insert_event(conn, "default", "a", 0, "committed");
    insert_event(conn, "default", "b", 1, "pending");
    insert_event(conn, "other", "c", 0, "committed");

    EXPECT_EQ(next_seq(conn, "default"), 2);
    EXPECT_EQ(next_seq(conn, "other"), 1);
    EXPECT_EQ(next_seq(conn, "none"), 0);
}

TEST(MemoryEvents, NextSeqCountsAbandonedRowsSoNumbersAreNeverReused) {
    auto adapter = make_adapter();
    auto& conn = adapter->connection();
    insert_event(conn, "default", "a", 0, "committed");
    insert_event(conn, "default", "b", 1, "abandoned");

    EXPECT_EQ(next_seq(conn, "default"), 2);
}

TEST(MemoryEvents, NextSeqOnEmptyLibraryIsZero) {
    auto adapter = make_adapter();
    EXPECT_EQ(next_seq(adapter->connection(), "default"), 0);
}

TEST(MemoryEvents, PendingEventsOrderedBySeqOnlyPending) {
    // 全世界的 pending 行(所有 holder),按世界一级的 seq 升序。holder 名的字母序与 seq
    // 相反,所以按主键 (tenant_id, holder_id, key) 扫出来的顺序会让本用例失败。
    auto adapter = make_adapter();
    auto& conn = adapter->connection();
    insert_event(conn, "default", "late", 5, "pending", "carol", "timeout", "aaa");
    insert_event(conn, "default", "done", 2, "committed");
    insert_event(conn, "default", "gone", 3, "abandoned");
    insert_event(conn, "default", "early", 1, "pending", "bob", "", "zzz");
    insert_event(conn, "other", "foreign", 0, "pending");

    const auto rows = pending_events(conn, "default");
    ASSERT_EQ(rows.size(), 2U);
    EXPECT_EQ(rows[0].key, "early");
    EXPECT_EQ(rows[0].text, "text-early");
    EXPECT_EQ(rows[0].interlocutor, "bob");
    EXPECT_EQ(rows[0].seq, 1);
    EXPECT_EQ(rows[0].failure_category, "");
    EXPECT_EQ(rows[0].holder, "zzz");
    EXPECT_EQ(rows[1].key, "late");
    EXPECT_EQ(rows[1].interlocutor, "carol");
    EXPECT_EQ(rows[1].seq, 5);
    EXPECT_EQ(rows[1].failure_category, "timeout");
    EXPECT_EQ(rows[1].holder, "aaa");
}

TEST(MemoryEvents, PendingEventsEmptyForUnknownTenant) {
    auto adapter = make_adapter();
    auto& conn = adapter->connection();
    insert_event(conn, "default", "a", 0, "pending");

    EXPECT_TRUE(pending_events(conn, "none").empty());
}

// ── 写侧 remember_event ──

TEST(MemoryEvents, RememberEventNewKeyCommitsAndAssignsSeq) {
    auto adapter = make_adapter();
    extractor::FakeLLMAdapter llm;
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});

    const auto res = remember_event(*adapter, llm, event_params(), RememberPrompts{},
                                    "r1/turn#3", "bob:hi", kEpoch);
    EXPECT_EQ(res.outcome, "accepted");
    EXPECT_FALSE(res.extraction_failed);
    EXPECT_FALSE(res.statement_ids.empty());

    auto& conn = adapter->connection();
    EXPECT_EQ(event_state(conn, "r1/turn#3"), "committed");
    EXPECT_EQ(next_seq(conn, "default"), 1);        // 第一条事件的 seq 是 0
    EXPECT_TRUE(pending_events(conn, "default").empty());
    // key 作为前缀写进 Starling 的文本(设计源 4.3),前缀在 Starling 侧加。
    persistence::Stmt q(conn.raw(), "SELECT payload_inline, created_at FROM engrams");
    ASSERT_TRUE(q.step_row());
    const auto* blob = static_cast<const char*>(sqlite3_column_blob(q.raw(), 0));
    const std::string payload(blob, static_cast<std::size_t>(sqlite3_column_bytes(q.raw(), 0)));
    EXPECT_EQ(payload, "[r1/turn#3] bob:hi");
    // NOLINTNEXTLINE(cppcoreguidelines-pro-type-reinterpret-cast)
    EXPECT_EQ(std::string(reinterpret_cast<const char*>(sqlite3_column_text(q.raw(), 1))),
              kEpoch);                              // seq=0 → epoch + 0 秒
}

TEST(MemoryEvents, RememberEventSameKeySameTextReturnsStoredIdsWithoutExtraction) {
    auto adapter = make_adapter();
    extractor::FakeLLMAdapter llm;
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});
    const auto first = remember_event(*adapter, llm, event_params(), RememberPrompts{},
                                      "r1/turn#3", "bob:hi", kEpoch);
    ASSERT_FALSE(first.statement_ids.empty());
    const int before = row_count(adapter->connection(), "SELECT COUNT(*) FROM statements");

    // 行已 committed:一次 LLM 都不该调,所以换上永远失败的适配器也必须照常返回。
    AlwaysFailingLLMAdapter failing;
    const auto again = remember_event(*adapter, failing, event_params(), RememberPrompts{},
                                      "r1/turn#3", "bob:hi", kEpoch);
    EXPECT_EQ(again.outcome, "idempotent");
    EXPECT_FALSE(again.extraction_failed);
    EXPECT_EQ(again.statement_ids, first.statement_ids);
    EXPECT_EQ(again.engram_ref, first.engram_ref);
    EXPECT_EQ(row_count(adapter->connection(), "SELECT COUNT(*) FROM statements"), before);
    EXPECT_EQ(next_seq(adapter->connection(), "default"), 1);   // 不另分配 seq
}

TEST(MemoryEvents, RememberEventSameKeyDifferentTextThrowsEventConflict) {
    auto adapter = make_adapter();
    extractor::FakeLLMAdapter llm;
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});
    ASSERT_EQ(remember_event(*adapter, llm, event_params(), RememberPrompts{},
                             "r1/turn#3", "bob:hi", kEpoch).outcome, "accepted");

    EXPECT_THROW(remember_event(*adapter, llm, event_params(), RememberPrompts{},
                                "r1/turn#3", "bob:bye", kEpoch),
                 EventConflict);
    // abandoned 行同样冲突(调用方已放弃这个 key,不能静默接着写)。
    insert_event(adapter->connection(), "default", "r1/turn#4", 1, "abandoned");
    EXPECT_THROW(remember_event(*adapter, llm, event_params(), RememberPrompts{},
                                "r1/turn#4", "text-r1/turn#4", kEpoch),
                 EventConflict);
}

namespace {

// 事件行里存的 interlocutor。
std::string stored_interlocutor(persistence::Connection& conn, std::string_view key) {
    persistence::Stmt q(conn.raw(),
                        "SELECT interlocutor FROM events WHERE tenant_id='default' AND key=?1");
    q.bind(key);
    if (!q.step_row()) return {};
    // NOLINTNEXTLINE(cppcoreguidelines-pro-type-reinterpret-cast)
    const auto* text = reinterpret_cast<const char*>(sqlite3_column_text(q.raw(), 0));
    return text != nullptr ? std::string(text) : std::string();
}

// 抛出的 EventConflict 的消息;没抛则返回空串(调用方随后断言非空)。
template <typename Fn>
std::string conflict_message(Fn&& fn) {
    try {
        fn();
    } catch (const EventConflict& e) {
        return e.what();
    }
    return {};
}

}  // namespace

TEST(MemoryEvents, RememberEventRetryWithDifferentInterlocutorThrowsAndLeavesRowUntouched) {
    auto adapter = make_adapter();
    // 首次抽取失败 → 行以 interlocutor=bob 留 pending。
    AlwaysFailingLLMAdapter failing;
    ASSERT_TRUE(remember_event(*adapter, failing, event_params(), RememberPrompts{},
                               "r1/turn#3", "bob:hi", kEpoch).extraction_failed);

    // 同 key 同 text,换成 carol 重试:拒绝,不抽取、不改行。
    extractor::FakeLLMAdapter llm;
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});
    auto carol = event_params();
    carol.interlocutor = "carol";
    const auto msg = conflict_message([&] {
        remember_event(*adapter, llm, carol, RememberPrompts{}, "r1/turn#3", "bob:hi", kEpoch);
    });
    ASSERT_FALSE(msg.empty()) << "expected EventConflict";
    EXPECT_NE(msg.find("different interlocutor"), std::string::npos) << msg;
    EXPECT_EQ(msg.find("different text"), std::string::npos) << msg;   // 与文本不一致可区分
    EXPECT_EQ(msg.find("abandoned"), std::string::npos) << msg;        // 与已放弃可区分
    EXPECT_EQ(event_state(adapter->connection(), "r1/turn#3"), "pending");
    EXPECT_EQ(stored_interlocutor(adapter->connection(), "r1/turn#3"), "bob");
    EXPECT_EQ(row_count(adapter->connection(), "SELECT COUNT(*) FROM statements"), 0);
    EXPECT_EQ(next_seq(adapter->connection(), "default"), 1);          // 没另分配 seq

    // 说话对象改回 bob 的重试照常补齐成 committed,语句落在 bob 的 scope_parties 下。
    const auto ok = remember_event(*adapter, llm, event_params(), RememberPrompts{},
                                   "r1/turn#3", "bob:hi", kEpoch);
    EXPECT_FALSE(ok.extraction_failed);
    EXPECT_FALSE(ok.statement_ids.empty());
    EXPECT_EQ(event_state(adapter->connection(), "r1/turn#3"), "committed");
    EXPECT_EQ(row_count(adapter->connection(),
                        "SELECT COUNT(*) FROM statements WHERE scope_parties_json LIKE '%bob%'"),
              static_cast<int>(ok.statement_ids.size()));
    EXPECT_EQ(row_count(adapter->connection(),
                        "SELECT COUNT(*) FROM statements WHERE scope_parties_json LIKE '%carol%'"),
              0);
}

TEST(MemoryEvents, RememberEventConflictMessagesTellTheThreeCausesApart) {
    auto adapter = make_adapter();
    AlwaysFailingLLMAdapter failing;
    ASSERT_TRUE(remember_event(*adapter, failing, event_params(), RememberPrompts{},
                               "k-text", "bob:hi", kEpoch).extraction_failed);
    ASSERT_TRUE(remember_event(*adapter, failing, event_params(), RememberPrompts{},
                               "k-abandon", "bob:hi", kEpoch).extraction_failed);
    abandon_event(*adapter, "default", kHolder, "k-abandon");
    auto carol = event_params();
    carol.interlocutor = "carol";

    const auto different_text = conflict_message([&] {
        remember_event(*adapter, failing, event_params(), RememberPrompts{},
                       "k-text", "bob:bye", kEpoch);
    });
    const auto different_interlocutor = conflict_message([&] {
        remember_event(*adapter, failing, carol, RememberPrompts{}, "k-text", "bob:hi", kEpoch);
    });
    const auto abandoned = conflict_message([&] {
        remember_event(*adapter, failing, event_params(), RememberPrompts{},
                       "k-abandon", "bob:hi", kEpoch);
    });
    ASSERT_FALSE(different_text.empty());
    ASSERT_FALSE(different_interlocutor.empty());
    ASSERT_FALSE(abandoned.empty());
    EXPECT_NE(different_text.find("different text"), std::string::npos) << different_text;
    EXPECT_NE(different_interlocutor.find("different interlocutor"), std::string::npos)
        << different_interlocutor;
    EXPECT_NE(abandoned.find("already abandoned"), std::string::npos) << abandoned;
    EXPECT_EQ(different_text.find("interlocutor"), std::string::npos) << different_text;
    EXPECT_EQ(abandoned.find("different"), std::string::npos) << abandoned;
}

TEST(MemoryEvents, RememberEventCompletesFromEngramWithoutReextracting) {
    auto adapter = make_adapter();
    // ① 抽取失败 → 行留 pending、无语句。
    AlwaysFailingLLMAdapter failing;
    const auto failed = remember_event(*adapter, failing, event_params(), RememberPrompts{},
                                       "r1/turn#3", "bob:hi", kEpoch);
    EXPECT_TRUE(failed.extraction_failed);
    EXPECT_FALSE(failed.failure_category.empty());
    ASSERT_EQ(pending_events(adapter->connection(), "default").size(), 1U);
    EXPECT_EQ(pending_events(adapter->connection(), "default")[0].failure_category,
              failed.failure_category);

    // ② 用裸 remember_all 把同一 payload 的语句落库(模拟"②成了、③没写成")。前缀是
    // event_params 的 "mem-",不带 holder:这是 0039 之前的来源身份。① 已经建了带 holder
    // 的 engram(空的),补齐要越过它找到旧身份上的语句。
    extractor::FakeLLMAdapter llm;
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});
    auto landed_params = event_params();
    const std::string payload = "[r1/turn#3] bob:hi";
    landed_params.payload.assign(payload.begin(), payload.end());
    landed_params.created_at_iso8601 = kEpoch;
    const auto landed = remember_all(*adapter, llm, landed_params, RememberPrompts{});
    ASSERT_FALSE(landed.statement_ids.empty());
    const int before = row_count(adapter->connection(), "SELECT COUNT(*) FROM statements");

    // ③ 补齐:按来源身份找到语句,不再抽取一次(故用失败适配器)。
    const auto completed = remember_event(*adapter, failing, event_params(), RememberPrompts{},
                                          "r1/turn#3", "bob:hi", kEpoch);
    EXPECT_FALSE(completed.extraction_failed);
    auto got = completed.statement_ids;
    auto want = landed.statement_ids;
    std::sort(got.begin(), got.end());
    std::sort(want.begin(), want.end());
    EXPECT_EQ(got, want);
    EXPECT_EQ(row_count(adapter->connection(), "SELECT COUNT(*) FROM statements"), before);
    EXPECT_EQ(event_state(adapter->connection(), "r1/turn#3"), "committed");
    EXPECT_TRUE(pending_events(adapter->connection(), "default").empty());
}

// ── abandon_event ──

TEST(MemoryEvents, AbandonEventTurnsPendingIntoAbandonedAndKeepsSeq) {
    auto adapter = make_adapter();
    auto& conn = adapter->connection();
    // pending 行可以带 failure_category(第 3 段未入库或抽取失败时留下的),照样能放弃。
    insert_event(conn, "default", "r1/turn#3", 0, "pending", "bob", "not_stored:rejected");
    insert_event(conn, "default", "r1/turn#4", 1, "pending");

    abandon_event(*adapter, "default", kHolder, "r1/turn#3");

    EXPECT_EQ(event_state(conn, "r1/turn#3"), "abandoned");
    EXPECT_EQ(event_state(conn, "r1/turn#4"), "pending");     // 只动目标 key
    const auto rows = pending_events(conn, "default");
    ASSERT_EQ(rows.size(), 1U);
    EXPECT_EQ(rows[0].key, "r1/turn#4");
    EXPECT_EQ(next_seq(conn, "default"), 2);                  // 放弃不回收 seq
}

TEST(MemoryEvents, AbandonEventTwiceIsNoop) {
    auto adapter = make_adapter();
    auto& conn = adapter->connection();
    insert_event(conn, "default", "r1/turn#3", 0, "pending");

    abandon_event(*adapter, "default", kHolder, "r1/turn#3");
    EXPECT_NO_THROW(abandon_event(*adapter, "default", kHolder, "r1/turn#3"));
    EXPECT_EQ(event_state(conn, "r1/turn#3"), "abandoned");
}

TEST(MemoryEvents, AbandonEventRefusesCommittedWithEventConflict) {
    auto adapter = make_adapter();
    extractor::FakeLLMAdapter llm;
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});
    ASSERT_EQ(remember_event(*adapter, llm, event_params(), RememberPrompts{},
                             "r1/turn#3", "bob:hi", kEpoch).outcome, "accepted");

    EXPECT_THROW(abandon_event(*adapter, "default", kHolder, "r1/turn#3"), EventConflict);
    EXPECT_EQ(event_state(adapter->connection(), "r1/turn#3"), "committed");
}

TEST(MemoryEvents, AbandonEventUnknownKeyThrowsOutOfRange) {
    auto adapter = make_adapter();
    auto& conn = adapter->connection();
    insert_event(conn, "other", "r1/turn#3", 0, "pending");   // 别的租户的同名 key 不算

    EXPECT_THROW(abandon_event(*adapter, "default", kHolder, "r1/turn#3"), std::out_of_range);
    EXPECT_EQ(event_state(conn, "r1/turn#3"), "");            // 没有凭空造行
}

TEST(MemoryEvents, AbandonEventOnlyTouchesItsOwnTenant) {
    // abandon 的 UPDATE 带 tenant_id 围栏:两个租户同名 key,放弃一个不许动另一个。
    // 去掉 WHERE 里的 tenant_id=?1 时本用例必须失败(只剩 key 匹配 → 两行一起改)。
    auto adapter = make_adapter();
    auto& conn = adapter->connection();
    insert_event(conn, "default", "r1/turn#3", 0, "pending");
    insert_event(conn, "other", "r1/turn#3", 0, "pending");

    abandon_event(*adapter, "default", kHolder, "r1/turn#3");

    EXPECT_EQ(state_of(conn, "default", "r1/turn#3"), "abandoned");
    EXPECT_EQ(state_of(conn, "other", "r1/turn#3"), "pending");   // 别的租户没被牵连
    ASSERT_EQ(pending_events(conn, "other").size(), 1U);
    EXPECT_EQ(pending_events(conn, "other")[0].key, "r1/turn#3");
    EXPECT_TRUE(pending_events(conn, "default").empty());
}

TEST(MemoryEvents, AbandonDuringExtractIsNotResurrectedByPhaseThree) {
    // 竞态:第 2 段(抽取,事务外)期间 abandon_event 提交,随后第 3 段的提交支不许把
    // 'abandoned' 覆写回 'committed'(设计源 §8:放弃是终局)。零行 → EventConflict。
    auto adapter = make_adapter();
    MutateRowDuringExtractAdapter llm(
        extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true},
        [&adapter] { abandon_event(*adapter, "default", kHolder, "r1/turn#3"); });

    EXPECT_THROW(remember_event(*adapter, llm, event_params(), RememberPrompts{},
                                "r1/turn#3", "bob:hi", kEpoch),
                 EventConflict);

    auto& conn = adapter->connection();
    EXPECT_EQ(event_state(conn, "r1/turn#3"), "abandoned");   // 没复活
    EXPECT_TRUE(pending_events(conn, "default").empty());
    // 之后这个 key 照样是终局:再写一律冲突。
    extractor::FakeLLMAdapter plain;
    plain.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});
    EXPECT_THROW(remember_event(*adapter, plain, event_params(), RememberPrompts{},
                                "r1/turn#3", "bob:hi", kEpoch),
                 EventConflict);
}

TEST(MemoryEvents, AbandonDuringExtractAlsoGuardsTheFailureBranch) {
    // 同一竞态落在第 3 段的未入库/抽取失败支上:那条 UPDATE 也带 state='pending' 围栏,
    // 零行同样抛。否则 failure_category 会写进一行已经放弃的收据里。
    auto adapter = make_adapter();
    MutateRowDuringExtractAdapter llm(
        extractor::LLMResponse{.raw_xml = "", .ok = false, .error = "forced failure"},
        [&adapter] { abandon_event(*adapter, "default", kHolder, "r1/turn#3"); });

    EXPECT_THROW(remember_event(*adapter, llm, event_params(), RememberPrompts{},
                                "r1/turn#3", "bob:hi", kEpoch),
                 EventConflict);

    auto& conn = adapter->connection();
    EXPECT_EQ(event_state(conn, "r1/turn#3"), "abandoned");
    EXPECT_EQ(row_count(conn, "SELECT COUNT(*) FROM events WHERE failure_category<>''"), 0);
}

TEST(MemoryEvents, RowDeletedDuringExtractThrowsInsteadOfSilentlyDoingNothing) {
    // 任务 6 搁置的那条发现:第 2 段期间 clear_tenant 把行删了,第 3 段的 UPDATE 匹配
    // 零行。原先静默收尾、调用方以为收据写成了;现在抛 EventConflict。
    auto adapter = make_adapter();
    MutateRowDuringExtractAdapter llm(
        extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true},
        [&adapter] {
            persistence::Stmt del(adapter->connection().raw(),
                                  "DELETE FROM events WHERE tenant_id='default'");
            del.step_done();
        });

    EXPECT_THROW(remember_event(*adapter, llm, event_params(), RememberPrompts{},
                                "r1/turn#3", "bob:hi", kEpoch),
                 EventConflict);
    EXPECT_EQ(row_count(adapter->connection(), "SELECT COUNT(*) FROM events"), 0);
}

TEST(MemoryEvents, AbandonedKeyRejectsLaterRememberEvent) {
    auto adapter = make_adapter();
    extractor::FakeLLMAdapter llm;
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});
    AlwaysFailingLLMAdapter failing;
    // 抽取失败 → 行留 pending(带 failure_category),随后放弃。
    ASSERT_TRUE(remember_event(*adapter, failing, event_params(), RememberPrompts{},
                               "r1/turn#3", "bob:hi", kEpoch).extraction_failed);
    abandon_event(*adapter, "default", kHolder, "r1/turn#3");

    EXPECT_THROW(remember_event(*adapter, llm, event_params(), RememberPrompts{},
                                "r1/turn#3", "bob:hi", kEpoch),
                 EventConflict);
    EXPECT_EQ(row_count(adapter->connection(), "SELECT COUNT(*) FROM statements"), 0);
}

// ── 迁移 0039:events 按 holder 区分 ──

namespace {

// 0038 的 events 表,逐字照抄迁移文件(已发布的迁移有 checksum 钉着,不会再变)。
// 用来在最新库上造一份「只跑到 0038」的库:删掉 0039 起的表形与迁移记录。
constexpr const char* kEvents0038Ddl = R"SQL(
CREATE TABLE IF NOT EXISTS events (
    tenant_id          TEXT NOT NULL,
    key                TEXT NOT NULL,
    text               TEXT NOT NULL,
    interlocutor       TEXT NOT NULL,
    text_sha256        TEXT NOT NULL,
    engram_ref         TEXT,
    statement_ids_json TEXT NOT NULL DEFAULT '[]',
    seq                INTEGER NOT NULL,
    state              TEXT NOT NULL CHECK (state IN ('pending','committed','abandoned')),
    failure_category   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (tenant_id, key),
    UNIQUE (tenant_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_events_state ON events(tenant_id, state);
)SQL";

void exec_script(persistence::Connection& conn, const char* sql) {
    char* err = nullptr;
    const int result_code = sqlite3_exec(conn.raw(), sql, nullptr, nullptr, &err);
    const std::string msg = err != nullptr ? err : "";
    sqlite3_free(err);
    ASSERT_EQ(result_code, SQLITE_OK) << msg;
}

std::unique_ptr<persistence::SqliteAdapter> make_adapter_at_0038() {
    auto adapter = make_adapter();
    auto& conn = adapter->connection();
    exec_script(conn, "DROP TABLE events");
    exec_script(conn, kEvents0038Ddl);
    exec_script(conn, "DELETE FROM schema_migrations WHERE version >= 39");
    return adapter;
}

// 0038 形状的 events 行(没有 holder_id 列)。
void insert_0038_event(persistence::Connection& conn, std::string_view key, std::int64_t seq,
                       std::string_view state, std::string_view statement_ids_json) {
    persistence::Stmt ins(conn.raw(),
        "INSERT INTO events(tenant_id,key,text,interlocutor,text_sha256,statement_ids_json,"
        "seq,state) VALUES('default',?1,'bob:hi','bob','h',?2,?3,?4)");
    ins.bind(key, statement_ids_json, seq, state);
    ins.step_done();
}

// 一条最小的语句行,只为让回填能按 id 查到 holder_id。
void insert_statement(persistence::Connection& conn, std::string_view tenant,
                      std::string_view stmt_id, std::string_view holder) {
    persistence::Stmt ins(conn.raw(),
        "INSERT INTO statements(id,tenant_id,holder_id,holder_perspective,subject_kind,"
        "subject_id,predicate,object_kind,object_value,canonical_object_hash,modality,"
        "polarity,confidence,observed_at,salience,affect_json,activation,last_accessed,"
        "provenance,created_at,updated_at) "
        "VALUES(?1,?2,?3,'first_person','entity','cog-bob','responsible_for','str','auth',"
        "'h-obj','believes','pos',0.7,'2026-01-01T00:00:00Z',0.5,'{}',0.0,"
        "'2026-01-01T00:00:00Z','user_input','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')");
    ins.bind(stmt_id, tenant, holder);
    ins.step_done();
}

std::string text_query(persistence::Connection& conn, const std::string& sql) {
    persistence::Stmt q(conn.raw(), sql.c_str());
    if (!q.step_row()) return {};
    // NOLINTNEXTLINE(cppcoreguidelines-pro-type-reinterpret-cast)
    const auto* text = reinterpret_cast<const char*>(sqlite3_column_text(q.raw(), 0));
    return text != nullptr ? std::string(text) : std::string();
}

std::string holder_of(persistence::Connection& conn, std::string_view key) {
    return text_query(conn, "SELECT holder_id FROM events WHERE tenant_id='default' AND key='" +
                                std::string(key) + "'");
}

// holder 名下的事件行,文本哈希是真的,同文本能对上。
void insert_text_event(persistence::Connection& conn, std::string_view holder,
                       std::string_view key, std::string_view text, std::int64_t seq,
                       std::string_view state) {
    persistence::Stmt ins(conn.raw(),
        "INSERT INTO events(tenant_id,holder_id,key,text,interlocutor,text_sha256,seq,state) "
        "VALUES('default',?1,?2,?3,'bob',?4,?5,?6)");
    ins.bind(holder, key, text, crypto::sha256_hex(text), seq, state);
    ins.step_done();
}

// 0039 之后、还没被任何 holder 碰过的旧行(holder_id='')。
void insert_unclaimed_event(persistence::Connection& conn, std::string_view key,
                            std::string_view text, std::int64_t seq, std::string_view state) {
    insert_text_event(conn, /*holder=*/"", key, text, seq, state);
}

RememberParams holder_params(std::string_view holder) {
    auto params = event_params();
    params.holder_id = std::string(holder);
    return params;
}

// 语句表里这批 id 的某一列(或列上的表达式)的值(去重)。
std::set<std::string> values_of_statements(persistence::Connection& conn,
                                           const std::vector<std::string>& ids,
                                           const std::string& column_expr) {
    std::set<std::string> values;
    for (const auto& stmt_id : ids) {
        values.insert(text_query(conn, "SELECT " + column_expr +
                                           " FROM statements WHERE tenant_id='default' AND id='" +
                                           stmt_id + "'"));
    }
    return values;
}

// 语句表里这批 id 的 holder_id(去重)。
std::set<std::string> holders_of_statements(persistence::Connection& conn,
                                            const std::vector<std::string>& ids) {
    return values_of_statements(conn, ids, "holder_id");
}

// 语句的 evidence_json 指向的 engram id(statement_writer.cpp 的 evidence_json 只写一项)。
constexpr const char* kEngramOfStatement = "json_extract(evidence_json,'$[0].engram_ref')";

// 数 extract() 调用次数、每次都回同一个响应的适配器。
class CountingLLMAdapter final : public extractor::LLMAdapter {
public:
    explicit CountingLLMAdapter(extractor::LLMResponse response)
        : response_(std::move(response)) {}

    extractor::LLMResponse extract(std::string_view, std::string_view) override {
        ++calls_;
        return response_;
    }

    [[nodiscard]] int calls() const { return calls_; }

private:
    extractor::LLMResponse response_;
    int                    calls_ = 0;
};

// 两条语句:Bob 负责 auth(记在写入者名下),以及 B 自己的偏好。开了
// attribute_first_order_mental_to_holder 时,第二条在任何写入者的抽取里都记在 B 名下。
constexpr const char* kPreferenceJson =
    R"JSON([{"holder":"self","holder_perspective":"FIRST_PERSON","subject":"Bob","predicate":"responsible_for","object":"auth","modality":"BELIEVES","polarity":"POS","nesting_depth":0},)JSON"
    R"JSON({"holder":"B","holder_perspective":"FIRST_PERSON","subject":"B","subject_kind":"cognizer","cognizer_kind":"human","predicate":"prefers","object":"tea","modality":"PREFERS","polarity":"POS","nesting_depth":0}])JSON";

std::vector<std::string> sorted(std::vector<std::string> ids) {
    std::sort(ids.begin(), ids.end());
    return ids;
}

}  // namespace

TEST(MemoryEvents0039, LegacyRowsBackfillHolderFromStatements) {
    auto adapter = make_adapter_at_0038();
    auto& conn = adapter->connection();
    // 列表里第一条在本租户查不到(别的租户有同 id 的语句,不算),第二条是 alice 的。
    insert_statement(conn, "other", "s-missing", "mallory");
    insert_statement(conn, "default", "s-alice", "alice");
    insert_0038_event(conn, "k-committed", 0, "committed", R"(["s-missing","s-alice"])");
    insert_0038_event(conn, "k-pending", 1, "pending", "[]");
    insert_0038_event(conn, "k-abandoned", 2, "abandoned", "[]");

    persistence::MigrationRunner(conn.raw()).migrate_to_latest();

    EXPECT_EQ(holder_of(conn, "k-committed"), "alice");
    EXPECT_EQ(holder_of(conn, "k-pending"), "");
    EXPECT_EQ(holder_of(conn, "k-abandoned"), "");
    // 其余列原样复制。
    EXPECT_EQ(text_query(conn, "SELECT statement_ids_json FROM events WHERE key='k-committed'"),
              R"(["s-missing","s-alice"])");
    EXPECT_EQ(event_state(conn, "k-abandoned"), "abandoned");
    EXPECT_EQ(next_seq(conn, "default"), 3);

    std::vector<std::string> pk_columns;
    {
        persistence::Stmt q(conn.raw(),
                            "SELECT name FROM pragma_table_info('events') WHERE pk > 0 "
                            "ORDER BY pk");
        while (q.step_row()) {
            // NOLINTNEXTLINE(cppcoreguidelines-pro-type-reinterpret-cast)
            pk_columns.emplace_back(reinterpret_cast<const char*>(sqlite3_column_text(q.raw(), 0)));
        }
    }
    EXPECT_EQ(pk_columns, (std::vector<std::string>{"tenant_id", "holder_id", "key"}));
    EXPECT_EQ(row_count(conn, "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND "
                              "name='idx_events_state'"),
              1);

    // seq 仍是世界一级的唯一键:另一个 holder、另一个 key,同一个 seq 照样撞约束。
    EXPECT_THROW(insert_event(conn, "default", "k-new", 1, "pending", "bob", "", "bob"),
                 persistence::SqliteError);
}

TEST(MemoryEvents0039, MalformedStatementIdsBackfillEmptyHolderInsteadOfFailing) {
    // 列被外部写坏时迁移不许失败(失败了库就打不开),与 parse_statement_ids 当空一致。
    auto adapter = make_adapter_at_0038();
    auto& conn = adapter->connection();
    insert_statement(conn, "default", "s-alice", "alice");
    insert_0038_event(conn, "k-bad", 0, "committed", "not json");
    insert_0038_event(conn, "k-object", 1, "committed", R"({"first":"s-alice"})");

    persistence::MigrationRunner(conn.raw()).migrate_to_latest();

    EXPECT_EQ(holder_of(conn, "k-bad"), "");
    EXPECT_EQ(event_state(conn, "k-bad"), "committed");
    EXPECT_EQ(holder_of(conn, "k-object"), "");      // 不是数组:当空,不按对象的值去找
}

TEST(MemoryEvents0039, FirstHolderClaimsLegacyRow) {
    auto adapter = make_adapter();
    auto& conn = adapter->connection();
    insert_unclaimed_event(conn, "r1/turn#3", "bob:hi", 4, "pending");
    extractor::FakeLLMAdapter llm;
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});

    // bob 先碰到它:行归 bob,按 pending 重试走(原 seq,不另分配),收成 committed。
    const auto bob = remember_event(*adapter, llm, holder_params("bob"), RememberPrompts{},
                                    "r1/turn#3", "bob:hi", kEpoch);
    EXPECT_FALSE(bob.extraction_failed);
    ASSERT_FALSE(bob.statement_ids.empty());
    EXPECT_EQ(holder_of(conn, "r1/turn#3"), "bob");
    EXPECT_EQ(event_state(conn, "r1/turn#3"), "committed");
    EXPECT_EQ(next_seq(conn, "default"), 5);
    EXPECT_EQ(text_query(conn, "SELECT created_at FROM engrams"), "2026-01-01T00:00:04Z");
    EXPECT_EQ(holders_of_statements(conn, bob.statement_ids), std::set<std::string>{"bob"});

    // 之后 carol 写同一个 key 是一条全新的事件(新 seq、自己的行),不是重入:重入会原样
    // 拿到 bob 的语句 id。
    const auto carol = remember_event(*adapter, llm, holder_params("carol"), RememberPrompts{},
                                      "r1/turn#3", "bob:hi", kEpoch);
    EXPECT_EQ(carol.outcome, "accepted");
    ASSERT_FALSE(carol.statement_ids.empty());   // 否则下面的不相交检查空转
    EXPECT_EQ(row_count(conn, "SELECT COUNT(*) FROM events WHERE key='r1/turn#3'"), 2);
    EXPECT_EQ(row_count(conn, "SELECT seq FROM events WHERE holder_id='carol'"), 5);
    EXPECT_EQ(next_seq(conn, "default"), 6);
    EXPECT_EQ(row_count(conn, "SELECT COUNT(*) FROM events WHERE holder_id='bob' AND "
                              "state='committed' AND seq=4"),
              1);                                       // bob 的行没被动
    for (const auto& stmt_id : carol.statement_ids) {
        EXPECT_EQ(std::ranges::count(bob.statement_ids, stmt_id), 0) << stmt_id;
    }
}

TEST(MemoryEvents0039, AbandonEventClaimsLegacyRow) {
    auto adapter = make_adapter();
    auto& conn = adapter->connection();
    insert_unclaimed_event(conn, "r1/turn#3", "bob:hi", 0, "pending");

    abandon_event(*adapter, "default", "bob", "r1/turn#3");

    EXPECT_EQ(holder_of(conn, "r1/turn#3"), "bob");
    EXPECT_EQ(event_state(conn, "r1/turn#3"), "abandoned");
    // 行已归 bob:carol 名下没有这个 key,也没有可认领的旧行了。
    EXPECT_THROW(abandon_event(*adapter, "default", "carol", "r1/turn#3"), std::out_of_range);

    // 被拒绝的放弃不留认领:committed 旧行抛 EventConflict,事务回滚,行仍无主。
    insert_unclaimed_event(conn, "r1/turn#4", "bob:hi", 1, "committed");
    EXPECT_THROW(abandon_event(*adapter, "default", "bob", "r1/turn#4"), EventConflict);
    EXPECT_EQ(holder_of(conn, "r1/turn#4"), "");
}

TEST(MemoryEvents0039, AbandonedLegacyRowRejectsRememberEventAfterClaim) {
    auto adapter = make_adapter();
    auto& conn = adapter->connection();
    insert_unclaimed_event(conn, "r1/turn#3", "bob:hi", 0, "abandoned");
    extractor::FakeLLMAdapter llm;
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});

    // remember_event 认领后按状态机走到 abandoned → EventConflict。冲突回滚整个短事务,
    // 认领一并撤销:行原样留着,不另插行、不分配 seq。
    const auto msg = conflict_message([&] {
        remember_event(*adapter, llm, holder_params("bob"), RememberPrompts{}, "r1/turn#3",
                       "bob:hi", kEpoch);
    });
    EXPECT_NE(msg.find("already abandoned"), std::string::npos) << msg;
    EXPECT_EQ(holder_of(conn, "r1/turn#3"), "");
    EXPECT_EQ(row_count(conn, "SELECT COUNT(*) FROM events"), 1);
    EXPECT_EQ(next_seq(conn, "default"), 1);

    // abandon_event 对已放弃的行是幂等的,但会认领它;认领之后照样冲突。
    abandon_event(*adapter, "default", "bob", "r1/turn#3");
    EXPECT_EQ(holder_of(conn, "r1/turn#3"), "bob");
    EXPECT_THROW(remember_event(*adapter, llm, holder_params("bob"), RememberPrompts{},
                                "r1/turn#3", "bob:hi", kEpoch),
                 EventConflict);
    EXPECT_EQ(row_count(conn, "SELECT COUNT(*) FROM statements"), 0);
}

TEST(MemoryEventsHolder, SameKeySameTextGivesEachHolderOwnStatements) {
    {
        auto adapter = make_adapter();
        auto& conn = adapter->connection();
        extractor::FakeLLMAdapter llm;
        llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});

        const auto first = remember_event(*adapter, llm, holder_params("A"), RememberPrompts{},
                                          "ep1#3", "B：你好", kEpoch);
        const auto second = remember_event(*adapter, llm, holder_params("B"), RememberPrompts{},
                                           "ep1#3", "B：你好", kEpoch);
        EXPECT_EQ(first.outcome, "accepted");
        EXPECT_EQ(second.outcome, "accepted");
        ASSERT_FALSE(first.statement_ids.empty());
        ASSERT_FALSE(second.statement_ids.empty());
        for (const auto& stmt_id : second.statement_ids) {
            EXPECT_EQ(std::ranges::count(first.statement_ids, stmt_id), 0) << stmt_id;
        }
        EXPECT_EQ(holders_of_statements(conn, first.statement_ids), std::set<std::string>{"A"});
        EXPECT_EQ(holders_of_statements(conn, second.statement_ids), std::set<std::string>{"B"});
        EXPECT_EQ(row_count(conn, "SELECT COUNT(*) FROM events WHERE state='committed'"), 2);
        EXPECT_EQ(next_seq(conn, "default"), 2);
    }
    {
        // 中途失败:两行都留 pending,pending_events 按 seq 列出,各带自己的 holder。
        auto adapter = make_adapter();
        AlwaysFailingLLMAdapter failing;
        ASSERT_TRUE(remember_event(*adapter, failing, holder_params("A"), RememberPrompts{},
                                   "ep1#3", "B：你好", kEpoch).extraction_failed);
        ASSERT_TRUE(remember_event(*adapter, failing, holder_params("B"), RememberPrompts{},
                                   "ep1#3", "B：你好", kEpoch).extraction_failed);

        const auto rows = pending_events(adapter->connection(), "default");
        ASSERT_EQ(rows.size(), 2U);
        EXPECT_EQ(rows[0].key, "ep1#3");
        EXPECT_EQ(rows[0].holder, "A");
        EXPECT_EQ(rows[0].seq, 0);
        EXPECT_EQ(rows[1].key, "ep1#3");
        EXPECT_EQ(rows[1].holder, "B");
        EXPECT_EQ(rows[1].seq, 1);
    }
}

TEST(MemoryEventsHolder, EachHolderGetsOwnEngramAndObservedAt) {
    // 钉住 Task 2.1 已上的按 holder 来源前缀:同一段话写进两个 holder 名下是两个 engram,
    // 各自的时刻取各自事件的 seq。中间夹一条无关事件,让两个 seq 隔开。
    auto adapter = make_adapter();
    auto& conn = adapter->connection();
    extractor::FakeLLMAdapter llm;
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});

    const auto holder_a = remember_event(*adapter, llm, holder_params("A"), RememberPrompts{},
                                         "ep1#3", "B：你好", kEpoch);              // seq 0
    ASSERT_EQ(remember_event(*adapter, llm, holder_params("A"), RememberPrompts{},
                             "ep1#4", "C：再见", kEpoch).outcome,
              "accepted");                                                       // seq 1
    const auto holder_b = remember_event(*adapter, llm, holder_params("B"), RememberPrompts{},
                                         "ep1#3", "B：你好", kEpoch);              // seq 2
    ASSERT_FALSE(holder_a.statement_ids.empty());
    ASSERT_FALSE(holder_b.statement_ids.empty());

    EXPECT_NE(holder_a.engram_ref, holder_b.engram_ref);
    EXPECT_EQ(values_of_statements(conn, holder_a.statement_ids, kEngramOfStatement),
              std::set<std::string>{holder_a.engram_ref});
    EXPECT_EQ(values_of_statements(conn, holder_b.statement_ids, kEngramOfStatement),
              std::set<std::string>{holder_b.engram_ref});
    EXPECT_EQ(values_of_statements(conn, holder_a.statement_ids, "observed_at"),
              std::set<std::string>{"2026-01-01T00:00:00Z"});
    EXPECT_EQ(values_of_statements(conn, holder_b.statement_ids, "observed_at"),
              std::set<std::string>{"2026-01-01T00:00:02Z"});
    // 摘要按 content_hash 引用 engram,不含来源身份:两个 engram 内容相同,content_hash 相同。
    const auto hash_of = [&conn](const std::string& engram_id) {
        return text_query(conn, "SELECT content_hash FROM engrams WHERE id='" + engram_id + "'");
    };
    EXPECT_FALSE(hash_of(holder_a.engram_ref).empty());
    EXPECT_EQ(hash_of(holder_a.engram_ref), hash_of(holder_b.engram_ref));
}

TEST(MemoryEventsHolder, RetryCompletesFromOwnEngramNotOtherHolders) {
    // 开 attribute_first_order_mental_to_holder 时,A 的抽取把「B 偏好茶」记在 B 名下,这条语句
    // 挂在 A 的 engram 上。A、B 的 engram 内容相同(content_hash 相同),按 content_hash 补齐 B
    // 会先碰到 A 的 engram,把 A 抽出来的那条当成 B 自己的结果。补齐必须按 B 的来源身份找。
    auto adapter = make_adapter();
    auto& conn = adapter->connection();
    extractor::ValidationPolicy policy;
    policy.attribute_first_order_mental_to_holder = true;
    CountingLLMAdapter llm(extractor::LLMResponse{.raw_xml = kPreferenceJson, .ok = true});
    const std::string text = "B：我爱喝茶";

    const auto holder_a = remember_event(*adapter, llm, holder_params("A"), RememberPrompts{},
                                         "ep1#3", text, kEpoch, policy);           // seq 0
    ASSERT_EQ(holder_a.outcome, "accepted");
    ASSERT_EQ(holders_of_statements(conn, holder_a.statement_ids),
              (std::set<std::string>{"A", "B"}));
    const int calls_per_write = llm.calls();
    ASSERT_GT(calls_per_write, 0);

    // B 写同一 key 同文本:第 ② 段落了语句,第 ③ 段之前崩溃。行仍是 pending,语句挂在
    // B 自己的 engram 上(来源身份 source_prefix + "h:B:" + sha256(payload)[:16])。
    insert_text_event(conn, "B", "ep1#3", text, 1, "pending");
    auto landed_params = holder_params("B");
    landed_params.source_prefix = "mem-h:B:";
    const std::string payload = "[ep1#3] " + text;
    landed_params.payload.assign(payload.begin(), payload.end());
    landed_params.created_at_iso8601 = "2026-01-01T00:00:01Z";
    const auto landed = remember_all(*adapter, llm, landed_params, RememberPrompts{}, policy);
    ASSERT_EQ(landed.outcome, "accepted");
    ASSERT_NE(landed.engram_ref, holder_a.engram_ref);
    ASSERT_FALSE(landed.statement_ids.empty());
    ASSERT_EQ(llm.calls(), 2 * calls_per_write);
    const int before = row_count(conn, "SELECT COUNT(*) FROM statements");

    const auto retried = remember_event(*adapter, llm, holder_params("B"), RememberPrompts{},
                                        "ep1#3", text, kEpoch, policy);
    EXPECT_EQ(retried.outcome, "idempotent");
    EXPECT_EQ(retried.engram_ref, landed.engram_ref);
    EXPECT_EQ(sorted(retried.statement_ids), sorted(landed.statement_ids));
    EXPECT_EQ(values_of_statements(conn, retried.statement_ids, kEngramOfStatement),
              std::set<std::string>{landed.engram_ref});
    EXPECT_EQ(llm.calls(), 2 * calls_per_write);       // 全程两轮:A 一次、B 一次,补齐不调
    EXPECT_EQ(row_count(conn, "SELECT COUNT(*) FROM statements"), before);
    EXPECT_EQ(text_query(conn, "SELECT state || ' ' || engram_ref FROM events "
                               "WHERE holder_id='B' AND key='ep1#3'"),
              "committed " + landed.engram_ref);
}

TEST(MemoryEventsHolder, LegacyPrefixPendingRowCompletesWithoutReextract) {
    // 0039 之前崩溃留下的写入:engram 的来源身份不带 holder(source_prefix + sha256(payload)[:16]),
    // H 的语句挂在它上面,(H, K) 的行还是 pending。补齐找不到带 holder 的身份时退回旧身份,
    // 只收 holder_id == H 的语句,不再抽取(裁定 R9)。
    auto adapter = make_adapter();
    auto& conn = adapter->connection();
    extractor::FakeLLMAdapter llm;
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});
    const std::string text = "B：你好";

    auto legacy_params = holder_params("H");          // source_prefix 仍是 "mem-",不带 holder
    const std::string payload = "[ep1#3] " + text;
    legacy_params.payload.assign(payload.begin(), payload.end());
    legacy_params.created_at_iso8601 = kEpoch;
    const auto landed = remember_all(*adapter, llm, legacy_params, RememberPrompts{});
    ASSERT_EQ(landed.outcome, "accepted");
    ASSERT_FALSE(landed.statement_ids.empty());
    // 同一个旧 engram 上另挂一条别的 holder 的语句:补齐不许把它收进 H 的结果。
    insert_statement(conn, "default", "s-other-holder", "G");
    {
        persistence::Stmt upd(conn.raw(),
                              "UPDATE statements SET evidence_json=?1 WHERE id='s-other-holder'");
        upd.bind(R"([{"engram_ref":")" + landed.engram_ref + R"(","status":"active"}])");
        upd.step_done();
    }
    insert_text_event(conn, "H", "ep1#3", text, 0, "pending");
    const int before = row_count(conn, "SELECT COUNT(*) FROM statements");

    CountingLLMAdapter counting(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});
    const auto completed = remember_event(*adapter, counting, holder_params("H"),
                                          RememberPrompts{}, "ep1#3", text, kEpoch);
    EXPECT_EQ(counting.calls(), 0);
    EXPECT_EQ(completed.outcome, "idempotent");
    EXPECT_EQ(completed.engram_ref, landed.engram_ref);
    EXPECT_EQ(sorted(completed.statement_ids), sorted(landed.statement_ids));
    EXPECT_EQ(holders_of_statements(conn, completed.statement_ids), std::set<std::string>{"H"});
    EXPECT_EQ(row_count(conn, "SELECT COUNT(*) FROM statements"), before);
    EXPECT_EQ(row_count(conn, "SELECT COUNT(*) FROM engrams"), 1);   // 没另建带 holder 的 engram
    EXPECT_EQ(text_query(conn, "SELECT state FROM events WHERE holder_id='H' AND key='ep1#3'"),
              "committed");
}

// ── 裁定 R14:放弃别的 holder 的 key 时,消息点名主人 ──

namespace {

// 抛出的 std::out_of_range 的消息;没抛则返回空串(调用方随后断言非空)。
template <typename Fn>
std::string out_of_range_message(Fn&& call) {
    try {
        call();
    } catch (const std::out_of_range& err) {
        return err.what();
    }
    return {};
}

}  // namespace

TEST(MemoryEventsHolder, AbandonEventNamesOwningHoldersWhenKeyIsNotThisHolders) {
    // rise 的世界级短连接用 agent "world" 放弃事件。key 记在 pa、pb 名下,(world, K) 与
    // ('', K) 都没有:仍是 out_of_range(绑定译成 KeyError),但消息要点名主人,按 holder_id
    // 升序;别的租户的同名行不算。行一条都不动。
    auto adapter = make_adapter();
    auto& conn = adapter->connection();
    insert_event(conn, "default", "ep1#3", 0, "pending", "bob", "", "pb");
    insert_event(conn, "default", "ep1#3", 1, "pending", "bob", "", "pa");
    insert_event(conn, "other", "ep1#3", 0, "pending", "bob", "", "pz");

    const auto msg = out_of_range_message(
        [&] { abandon_event(*adapter, "default", "world", "ep1#3"); });
    EXPECT_EQ(msg, "abandon_event: key 'ep1#3' is recorded under holder(s) pa, pb, not 'world'");
    EXPECT_EQ(row_count(conn, "SELECT COUNT(*) FROM events WHERE tenant_id='default' AND "
                              "state='pending'"),
              2);
    EXPECT_EQ(row_count(conn, "SELECT COUNT(*) FROM events"), 3);

    // 本租户谁都没有这个 key:保留原来的 "no such key"。
    const auto missing = out_of_range_message(
        [&] { abandon_event(*adapter, "default", "world", "ep1#4"); });
    EXPECT_EQ(missing, "abandon_event: no such key (key=ep1#4)");
}

// ── 裁定 R15:holder 为空一律拒绝,'' 只留给无主旧行 ──

TEST(MemoryEventsHolder, RememberEventRejectsEmptyHolderWithoutWriting) {
    auto adapter = make_adapter();
    auto& conn = adapter->connection();
    CountingLLMAdapter llm(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});

    // 新 key:不插 pending 行、不分配 seq、不抽取。
    EXPECT_THROW(remember_event(*adapter, llm, holder_params(""), RememberPrompts{}, "ep1#3",
                                "bob:hi", kEpoch),
                 std::invalid_argument);
    EXPECT_EQ(row_count(conn, "SELECT COUNT(*) FROM events"), 0);
    EXPECT_EQ(row_count(conn, "SELECT COUNT(*) FROM engrams"), 0);
    EXPECT_EQ(row_count(conn, "SELECT COUNT(*) FROM statements"), 0);

    // 有 ('', K) 的无主旧行:空 holder 不许把它当成自己的行去重试并收成 committed。
    insert_unclaimed_event(conn, "ep1#4", "bob:hi", 0, "pending");
    EXPECT_THROW(remember_event(*adapter, llm, holder_params(""), RememberPrompts{}, "ep1#4",
                                "bob:hi", kEpoch),
                 std::invalid_argument);
    EXPECT_EQ(event_state(conn, "ep1#4"), "pending");
    EXPECT_EQ(holder_of(conn, "ep1#4"), "");
    EXPECT_EQ(row_count(conn, "SELECT COUNT(*) FROM events"), 1);
    EXPECT_EQ(row_count(conn, "SELECT COUNT(*) FROM engrams"), 0);
    EXPECT_EQ(llm.calls(), 0);
}

TEST(MemoryEventsHolder, AbandonEventRejectsEmptyHolderWithoutWriting) {
    auto adapter = make_adapter();
    auto& conn = adapter->connection();
    insert_unclaimed_event(conn, "ep1#3", "bob:hi", 0, "pending");

    // 不是 out_of_range(那是「key 不存在」),也不许把无主行改成 abandoned。
    EXPECT_THROW(abandon_event(*adapter, "default", "", "ep1#3"), std::invalid_argument);
    EXPECT_EQ(event_state(conn, "ep1#3"), "pending");
    EXPECT_EQ(holder_of(conn, "ep1#3"), "");
    EXPECT_THROW(abandon_event(*adapter, "default", "", "ep1#9"), std::invalid_argument);
    EXPECT_EQ(row_count(conn, "SELECT COUNT(*) FROM events"), 1);
}

// ── 复核 M2:旧库已提交行的重入,以及无主行上的冲突回滚认领 ──

TEST(MemoryEvents0039, BackfilledCommittedRowReentryIsIdempotentWithoutExtraction) {
    // 0038 时代 alice 写的一条已提交事件:语句挂在不带 holder 的旧来源身份的 engram 上。
    // 0039 按语句把行回填给 alice;alice 同 key 同文本重入,原样拿回存的结果,不抽取。
    auto adapter = make_adapter_at_0038();
    auto& conn = adapter->connection();
    extractor::FakeLLMAdapter llm;
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});
    const std::string text = "bob:hi";
    auto legacy_params = holder_params("alice");      // source_prefix "mem-",不带 holder
    const std::string payload = "[ep1#3] " + text;
    legacy_params.payload.assign(payload.begin(), payload.end());
    legacy_params.created_at_iso8601 = kEpoch;
    const auto landed = remember_all(*adapter, llm, legacy_params, RememberPrompts{});
    ASSERT_EQ(landed.outcome, "accepted");
    ASSERT_FALSE(landed.statement_ids.empty());
    {
        persistence::Stmt ins(conn.raw(),
            "INSERT INTO events(tenant_id,key,text,interlocutor,text_sha256,engram_ref,"
            "statement_ids_json,seq,state) VALUES('default',?1,?2,'bob',?3,?4,?5,0,'committed')");
        ins.bind("ep1#3", text, crypto::sha256_hex(text), landed.engram_ref,
                 nlohmann::json(landed.statement_ids).dump());
        ins.step_done();
    }

    persistence::MigrationRunner(conn.raw()).migrate_to_latest();
    ASSERT_EQ(holder_of(conn, "ep1#3"), "alice");
    const int before = row_count(conn, "SELECT COUNT(*) FROM statements");

    CountingLLMAdapter counting(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});
    const auto again = remember_event(*adapter, counting, holder_params("alice"),
                                      RememberPrompts{}, "ep1#3", text, kEpoch);
    EXPECT_EQ(again.outcome, "idempotent");
    EXPECT_EQ(again.statement_ids, landed.statement_ids);
    EXPECT_EQ(again.engram_ref, landed.engram_ref);
    EXPECT_EQ(counting.calls(), 0);
    EXPECT_EQ(row_count(conn, "SELECT COUNT(*) FROM statements"), before);
    EXPECT_EQ(row_count(conn, "SELECT COUNT(*) FROM events"), 1);
    EXPECT_EQ(next_seq(conn, "default"), 1);
}

TEST(MemoryEvents0039, ConflictOnUnclaimedPendingRowRollsBackTheClaim) {
    // ('', K) 的 pending 行:alice 认领后状态机抛 EventConflict,短事务回滚,认领一并撤销,
    // 行仍无主——换文本、换说话对象两种冲突都如此。两次都不抽取、不另插行、不分配 seq。
    auto adapter = make_adapter();
    auto& conn = adapter->connection();
    insert_unclaimed_event(conn, "ep1#3", "bob:hi", 0, "pending");   // interlocutor 'bob'
    CountingLLMAdapter llm(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});

    const auto text_msg = conflict_message([&] {
        remember_event(*adapter, llm, holder_params("alice"), RememberPrompts{}, "ep1#3",
                       "bob:bye", kEpoch);
    });
    EXPECT_NE(text_msg.find("different text"), std::string::npos) << text_msg;
    EXPECT_EQ(holder_of(conn, "ep1#3"), "");

    auto carol_params = holder_params("alice");
    carol_params.interlocutor = "carol";
    const auto party_msg = conflict_message([&] {
        remember_event(*adapter, llm, carol_params, RememberPrompts{}, "ep1#3", "bob:hi",
                       kEpoch);
    });
    EXPECT_NE(party_msg.find("different interlocutor"), std::string::npos) << party_msg;
    EXPECT_EQ(holder_of(conn, "ep1#3"), "");

    EXPECT_EQ(event_state(conn, "ep1#3"), "pending");
    EXPECT_EQ(row_count(conn, "SELECT COUNT(*) FROM events"), 1);
    EXPECT_EQ(next_seq(conn, "default"), 1);
    EXPECT_EQ(llm.calls(), 0);
}

// ── 分相入口(Task 4.5 修复轮 1,F1):begin / extract / finish 拼起来与 remember_event 一致 ──
//
// 服务端的 DashboardEngine 只在 begin 与 finish 持引擎锁,extract(纯 LLM)在锁外。下面每个用例在两个库上跑同一组
// 调用:一个走单体 remember_event,一个走三段;返回值、收据行与库摘要逐项相等。

namespace {

RememberOutcome split_remember_event(persistence::SqliteAdapter& adapter, extractor::LLMAdapter& llm,
                                     std::string_view key, std::string_view text,
                                     const RememberParams& base = event_params()) {
    auto begun = remember_event_begin(adapter, base, key, text, kEpoch);
    if (begun.done) { return *begun.done; }
    const auto extracted = remember_event_extract(adapter, llm, begun, RememberPrompts{});
    return remember_event_finish(adapter, llm, begun, extracted);
}

void expect_same_outcome(const RememberOutcome& mono, const RememberOutcome& split) {
    EXPECT_EQ(mono.outcome, split.outcome);
    EXPECT_EQ(mono.extraction_failed, split.extraction_failed);
    EXPECT_EQ(mono.failure_category, split.failure_category);
    EXPECT_EQ(mono.statement_ids.size(), split.statement_ids.size());
    EXPECT_EQ(mono.engram_ref.empty(), split.engram_ref.empty());
    EXPECT_EQ(mono.source_preserved, split.source_preserved);
    EXPECT_EQ(mono.structured_claims_persisted, split.structured_claims_persisted);
    EXPECT_EQ(mono.holder_id, split.holder_id);
}

std::string library_of(persistence::SqliteAdapter& adapter) {
    return digest(adapter.connection(), "default");
}

}  // namespace

TEST(MemoryEventsSplit, NewKeyMatchesMonolithic) {
    auto mono = make_adapter();
    auto split = make_adapter();
    extractor::FakeLLMAdapter llm;
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});
    const auto want = remember_event(*mono, llm, event_params(), RememberPrompts{}, "k1", "bob:hi", kEpoch);
    const auto got = split_remember_event(*split, llm, "k1", "bob:hi");
    expect_same_outcome(want, got);
    EXPECT_EQ(event_state(split->connection(), "k1"), "committed");
    EXPECT_EQ(library_of(*mono), library_of(*split));
}

TEST(MemoryEventsSplit, CommittedReentryIsDoneAtBeginWithoutExtraction) {
    auto adapter = make_adapter();
    extractor::FakeLLMAdapter llm;
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});
    const auto first = split_remember_event(*adapter, llm, "k1", "bob:hi");
    const auto begun = remember_event_begin(*adapter, event_params(), "k1", "bob:hi", kEpoch);
    ASSERT_TRUE(begun.done.has_value());                 // 不必抽取
    EXPECT_EQ(begun.done->outcome, "idempotent");
    EXPECT_EQ(begun.done->statement_ids, first.statement_ids);
    EXPECT_EQ(next_seq(adapter->connection(), "default"), 1);
}

TEST(MemoryEventsSplit, ConflictsThrowAtBeginBeforeAnyWrite) {
    auto adapter = make_adapter();
    extractor::FakeLLMAdapter llm;
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});
    (void)split_remember_event(*adapter, llm, "k1", "bob:hi");
    const int engrams = row_count(adapter->connection(), "SELECT COUNT(*) FROM engrams");
    EXPECT_THROW((void)remember_event_begin(*adapter, event_params(), "k1", "other", kEpoch), EventConflict);
    AlwaysFailingLLMAdapter failing;
    (void)split_remember_event(*adapter, failing, "k2", "bob:pending");      // 留 pending
    auto other = event_params();
    other.interlocutor = "carol";
    EXPECT_THROW((void)remember_event_begin(*adapter, other, "k2", "bob:pending", kEpoch), EventConflict);
    abandon_event(*adapter, "default", kHolder, "k2");
    EXPECT_THROW((void)remember_event_begin(*adapter, event_params(), "k2", "bob:pending", kEpoch),
                 EventConflict);
    EXPECT_EQ(row_count(adapter->connection(), "SELECT COUNT(*) FROM engrams"), engrams + 1);
    EXPECT_THROW((void)remember_event_begin(*adapter, RememberParams{}, "k3", "x", kEpoch),
                 std::invalid_argument);                 // 空 holder:零读写
}

TEST(MemoryEventsSplit, FailedExtractionLeavesPendingLikeMonolithic) {
    auto mono = make_adapter();
    auto split = make_adapter();
    AlwaysFailingLLMAdapter failing;
    const auto want = remember_event(*mono, failing, event_params(), RememberPrompts{}, "k1", "bob:hi", kEpoch);
    const auto got = split_remember_event(*split, failing, "k1", "bob:hi");
    expect_same_outcome(want, got);
    ASSERT_EQ(pending_events(split->connection(), "default").size(), 1U);
    EXPECT_EQ(pending_events(split->connection(), "default")[0].failure_category,
              pending_events(mono->connection(), "default")[0].failure_category);
    EXPECT_EQ(library_of(*mono), library_of(*split));
}

TEST(MemoryEventsSplit, PendingRetryCompletesFromEngramAtBegin) {
    auto adapter = make_adapter();
    AlwaysFailingLLMAdapter failing;
    (void)split_remember_event(*adapter, failing, "k1", "bob:hi");
    // 模拟「② 落了语句、③ 没写成」:按旧身份(不带 holder)落库,同 RememberEventCompletesFromEngramWithoutReextracting。
    extractor::FakeLLMAdapter llm;
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});
    auto landed_params = event_params();
    const std::string payload = "[k1] bob:hi";
    landed_params.payload.assign(payload.begin(), payload.end());
    landed_params.created_at_iso8601 = kEpoch;
    const auto landed = remember_all(*adapter, llm, landed_params, RememberPrompts{});
    ASSERT_FALSE(landed.statement_ids.empty());
    // 补齐在 begin 里完成(只读库、不调 LLM),但收据要等 finish 写:begin 返回待收尾的结果,不是 done。
    auto begun = remember_event_begin(*adapter, event_params(), "k1", "bob:hi", kEpoch);
    ASSERT_FALSE(begun.done.has_value());
    EXPECT_FALSE(begun.needs_extraction());
    const auto extracted = remember_event_extract(*adapter, failing, begun, RememberPrompts{});
    const auto done = remember_event_finish(*adapter, failing, begun, extracted);
    EXPECT_EQ(done.outcome, "idempotent");
    EXPECT_FALSE(done.extraction_failed);
    EXPECT_EQ(event_state(adapter->connection(), "k1"), "committed");
}

TEST(MemoryEventsSplit, AbandonBetweenBeginAndFinishIsEventConflict) {
    auto adapter = make_adapter();
    extractor::FakeLLMAdapter llm;
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});
    auto begun = remember_event_begin(*adapter, event_params(), "k1", "bob:hi", kEpoch);
    ASSERT_TRUE(begun.needs_extraction());
    const auto extracted = remember_event_extract(*adapter, llm, begun, RememberPrompts{});
    abandon_event(*adapter, "default", kHolder, "k1");   // 锁外期间被放弃
    const std::string before = library_of(*adapter);
    const int statements = row_count(adapter->connection(), "SELECT COUNT(*) FROM statements");
    EXPECT_THROW((void)remember_event_finish(*adapter, llm, begun, extracted), EventConflict);
    EXPECT_EQ(event_state(adapter->connection(), "k1"), "abandoned");
    // 修复轮 2 N1：零写入。语句不落进一个已经放弃的 key 下面。
    EXPECT_EQ(row_count(adapter->connection(), "SELECT COUNT(*) FROM statements"), statements);
    EXPECT_EQ(statements, 0);
    EXPECT_EQ(library_of(*adapter), before);
}

// 修复轮 2 N1：begin 之后,另一个调用以同一个 key 完整跑完并提交(客户端超时重放)。本次 finish 在事务里重读到 committed,
// 返回行里存的结果(idempotent),bundle 不提交,零写入,语句表里没有孤儿。
TEST(MemoryEventsSplit, FinishAfterAnotherCallCommittedTheKeyIsIdempotentWithoutWriting) {
    auto adapter = make_adapter();
    extractor::FakeLLMAdapter llm;
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});
    auto first = remember_event_begin(*adapter, event_params(), "k1", "bob:hi", kEpoch);
    ASSERT_TRUE(first.needs_extraction());
    const auto first_extracted = remember_event_extract(*adapter, llm, first, RememberPrompts{});
    // 重放：同一个 key、同一段文本，从头跑完。行仍是 pending，补齐找不到语句 → 照常抽取、提交。engram 已由第一次的 begin
    // 写好，所以 prepare 的 outcome 是 idempotent（证据重入），语句照常写出、收据 committed。
    const auto winner = remember_event(*adapter, llm, event_params(), RememberPrompts{}, "k1", "bob:hi", kEpoch);
    ASSERT_FALSE(winner.extraction_failed);
    ASSERT_FALSE(winner.statement_ids.empty());
    ASSERT_EQ(event_state(adapter->connection(), "k1"), "committed");
    const std::string before = library_of(*adapter);
    const int statements = row_count(adapter->connection(), "SELECT COUNT(*) FROM statements");
    const auto late = remember_event_finish(*adapter, llm, first, first_extracted);
    EXPECT_EQ(late.outcome, "idempotent");
    EXPECT_FALSE(late.extraction_failed);
    EXPECT_EQ(late.statement_ids, winner.statement_ids);
    EXPECT_EQ(late.engram_ref, winner.engram_ref);
    EXPECT_EQ(row_count(adapter->connection(), "SELECT COUNT(*) FROM statements"), statements);
    EXPECT_EQ(library_of(*adapter), before);
    EXPECT_EQ(event_state(adapter->connection(), "k1"), "committed");
    // 没有孤儿：每条语句都在收据里。
    persistence::Stmt ids(adapter->connection().raw(), "SELECT statement_ids_json FROM events WHERE key='k1'");
    ASSERT_TRUE(ids.step_row());
    // NOLINTNEXTLINE(cppcoreguidelines-pro-type-reinterpret-cast)
    const auto receipt = nlohmann::json::parse(reinterpret_cast<const char*>(sqlite3_column_text(ids.raw(), 0)));
    EXPECT_EQ(static_cast<int>(receipt.size()), statements);
}

// 抽取失败的那一支同样先重读：重读时行已 committed 就返回 idempotent，不写 failure_category。
TEST(MemoryEventsSplit, FailedFinishAfterAnotherCallCommittedIsIdempotent) {
    auto adapter = make_adapter();
    AlwaysFailingLLMAdapter failing;
    auto first = remember_event_begin(*adapter, event_params(), "k1", "bob:hi", kEpoch);
    const auto failed_extracted = remember_event_extract(*adapter, failing, first, RememberPrompts{});
    extractor::FakeLLMAdapter llm;
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});
    const auto winner = remember_event(*adapter, llm, event_params(), RememberPrompts{}, "k1", "bob:hi", kEpoch);
    ASSERT_FALSE(winner.statement_ids.empty());
    ASSERT_EQ(event_state(adapter->connection(), "k1"), "committed");
    const std::string before = library_of(*adapter);
    const auto late = remember_event_finish(*adapter, failing, first, failed_extracted);
    EXPECT_EQ(late.outcome, "idempotent");
    EXPECT_FALSE(late.extraction_failed);
    EXPECT_EQ(late.statement_ids, winner.statement_ids);
    EXPECT_EQ(library_of(*adapter), before);
    EXPECT_EQ(row_count(adapter->connection(), "SELECT COUNT(*) FROM events WHERE failure_category<>''"), 0);
}

// clear_tenant 把行删掉之后再 finish：抛 EventConflict，零写入。
TEST(MemoryEventsSplit, FinishAfterTheRowWasClearedThrowsWithoutWriting) {
    auto adapter = make_adapter();
    extractor::FakeLLMAdapter llm;
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});
    auto begun = remember_event_begin(*adapter, event_params(), "k1", "bob:hi", kEpoch);
    const auto extracted = remember_event_extract(*adapter, llm, begun, RememberPrompts{});
    {
        persistence::Stmt del(adapter->connection().raw(), "DELETE FROM events WHERE tenant_id='default'");
        del.step_done();
    }
    const std::string before = library_of(*adapter);
    try {
        (void)remember_event_finish(*adapter, llm, begun, extracted);
        ADD_FAILURE() << "no EventConflict";
    } catch (const EventConflict& err) {
        EXPECT_EQ(std::string(err.what()).find("may already be stored"), std::string::npos) << err.what();
    }
    EXPECT_EQ(row_count(adapter->connection(), "SELECT COUNT(*) FROM statements"), 0);
    EXPECT_EQ(library_of(*adapter), before);
}

TEST(MemoryEventsSplit, ExtractTouchesNoDatabase) {
    auto adapter = make_adapter();
    CountingLLMAdapter llm(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});
    auto begun = remember_event_begin(*adapter, event_params(), "k1", "bob:hi", kEpoch);
    const std::string before = library_of(*adapter);
    const int events = row_count(adapter->connection(), "SELECT COUNT(*) FROM events");
    (void)remember_event_extract(*adapter, llm, begun, RememberPrompts{});
    EXPECT_EQ(library_of(*adapter), before);
    EXPECT_EQ(row_count(adapter->connection(), "SELECT COUNT(*) FROM events"), events);
    EXPECT_GT(llm.calls(), 0);
}


// 最后修复波 A4（裁定 R96 M1）：begin 记下 text_sha256 与 seq；finish 重读到 committed 比 sha，重读到 pending 比 seq，任一不同
// 都抛 EventConflict、零写入。场景：A 完成 begin；clear_tenant 把行删掉；B 用同一个 key、不同的文本重建这一行。
namespace {

void clear_events(persistence::SqliteAdapter& adapter) {
    persistence::Stmt del(adapter.connection().raw(), "DELETE FROM events WHERE tenant_id='default'");
    del.step_done();
}

std::string receipt_of(persistence::Connection& conn, std::string_view key) {
    persistence::Stmt q(conn.raw(),
                        "SELECT state || '|' || text_sha256 || '|' || seq || '|' || statement_ids_json || '|' || "
                        "COALESCE(failure_category,'') FROM events WHERE tenant_id='default' AND key=?1");
    q.bind(key);
    if (!q.step_row()) {
        return {};
    }
    // NOLINTNEXTLINE(cppcoreguidelines-pro-type-reinterpret-cast)
    return reinterpret_cast<const char*>(sqlite3_column_text(q.raw(), 0));
}

}  // namespace

TEST(MemoryEventsSplit, FinishAfterTheKeyWasRecreatedWithAnotherTextAndCommittedThrows) {
    auto adapter = make_adapter();
    extractor::FakeLLMAdapter llm;
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});
    auto begun_a = remember_event_begin(*adapter, event_params(), "k1", "bob:hi", kEpoch);
    ASSERT_TRUE(begun_a.needs_extraction());
    const auto extracted_a = remember_event_extract(*adapter, llm, begun_a, RememberPrompts{});
    clear_events(*adapter);                                       // clear_tenant 把收据行删了
    const auto winner = remember_event(*adapter, llm, event_params(), RememberPrompts{}, "k1",
                                       "bob:something else", kEpoch);
    ASSERT_FALSE(winner.statement_ids.empty());
    ASSERT_EQ(event_state(adapter->connection(), "k1"), "committed");
    const std::string receipt = receipt_of(adapter->connection(), "k1");
    const std::string before = library_of(*adapter);
    const int statements = row_count(adapter->connection(), "SELECT COUNT(*) FROM statements");
    EXPECT_THROW((void)remember_event_finish(*adapter, llm, begun_a, extracted_a), EventConflict);
    EXPECT_EQ(receipt_of(adapter->connection(), "k1"), receipt);  // B 的收据不变
    EXPECT_EQ(row_count(adapter->connection(), "SELECT COUNT(*) FROM statements"), statements);
    EXPECT_EQ(library_of(*adapter), before);                       // B 的语句不变、A 一条不写
}

TEST(MemoryEventsSplit, FinishAfterTheKeyWasRecreatedAndIsStillPendingThrowsWithoutTouchingIt) {
    auto adapter = make_adapter();
    extractor::FakeLLMAdapter llm;
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});
    // 先占一个序号，让 B 重建的那一行拿到与 A 不同的 seq（clear 之后 next_seq 从 0 重算，再插一行无关的 key 垫上）。
    auto begun_a = remember_event_begin(*adapter, event_params(), "k1", "bob:hi", kEpoch);
    const auto extracted_a = remember_event_extract(*adapter, llm, begun_a, RememberPrompts{});
    clear_events(*adapter);
    insert_event(adapter->connection(), "default", "pad", 0, "abandoned");
    AlwaysFailingLLMAdapter failing;
    const auto parked = remember_event(*adapter, failing, event_params(), RememberPrompts{}, "k1", "bob:hi",
                                       kEpoch);                    // B 同文本、停在 pending（seq 1）
    ASSERT_TRUE(parked.extraction_failed);
    ASSERT_EQ(event_state(adapter->connection(), "k1"), "pending");
    const std::string receipt = receipt_of(adapter->connection(), "k1");
    ASSERT_NE(receipt.find("|1|"), std::string::npos) << receipt;
    const std::string before = library_of(*adapter);
    const int statements = row_count(adapter->connection(), "SELECT COUNT(*) FROM statements");
    EXPECT_THROW((void)remember_event_finish(*adapter, llm, begun_a, extracted_a), EventConflict);
    EXPECT_EQ(receipt_of(adapter->connection(), "k1"), receipt);  // failure_category 没被改写，仍是 pending
    EXPECT_EQ(row_count(adapter->connection(), "SELECT COUNT(*) FROM statements"), statements);
    EXPECT_EQ(library_of(*adapter), before);
}

TEST(MemoryEventsSplit, FinishOnTheSameRowItBeganStillCommits) {
    // 对照：没被删、没被重建（sha 与 seq 都对得上）时照常提交。
    auto adapter = make_adapter();
    extractor::FakeLLMAdapter llm;
    llm.set_default_response(extractor::LLMResponse{.raw_xml = kCannedJson, .ok = true});
    insert_event(adapter->connection(), "default", "pad", 0, "abandoned");
    auto begun = remember_event_begin(*adapter, event_params(), "k1", "bob:hi", kEpoch);
    const auto extracted = remember_event_extract(*adapter, llm, begun, RememberPrompts{});
    const auto out = remember_event_finish(*adapter, llm, begun, extracted);
    EXPECT_EQ(out.outcome, "accepted");
    EXPECT_EQ(event_state(adapter->connection(), "k1"), "committed");
}

}  // namespace starling::memoryops
