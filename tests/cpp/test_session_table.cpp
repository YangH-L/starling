// 会话、句柄、写占用与空闲判定（spec §11，Task 4.2）；写入准入与只读冻结的「被会话占用写入」一维（裁定 R49、R51）。
// 时间全由测试传入（now_ms），C++ 不读墙钟。
#include "starling/governance/session_table.hpp"

#include "starling/governance/registry_db.hpp"
#include "starling/governance/world_store.hpp"
#include "starling/persistence/connection.hpp"

#include <gtest/gtest.h>

#include <algorithm>
#include <cstdint>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

using starling::governance::HandleInfo;
using starling::governance::read_only_freeze_for;
using starling::governance::require_writable;
using starling::governance::SessionConfig;
using starling::governance::SessionTable;
using starling::governance::WorldRecord;
using starling::governance::WorldStoreError;
using starling::persistence::ReadOnlyFreeze;

constexpr std::int64_t kLease = 30000;
constexpr std::int64_t kDay = 86400000;

// 调用抛 WorldStoreError 时返回它的 code；不抛返回空串。
template <typename Call>
std::string code_of(Call&& call) {
    try {
        call();
    } catch (const WorldStoreError& err) {
        return err.code();
    }
    return "";
}

std::vector<std::string> sorted(std::vector<std::string> ids) {
    std::sort(ids.begin(), ids.end());
    return ids;
}

bool is_lower_hex(const std::string& text) {
    return std::all_of(text.begin(), text.end(), [](char chr) {
        return (chr >= '0' && chr <= '9') || (chr >= 'a' && chr <= 'f');
    });
}

// connect 的应答带租约与心跳间隔（Task 4.5）：心跳间隔是租约的三分之一（spec §11：30 秒租约、10 秒心跳）。
TEST(SessionTable, LeaseAndHeartbeatIntervals) {
    const SessionTable defaults(SessionConfig{});
    EXPECT_EQ(defaults.lease_ms(), kLease);
    EXPECT_EQ(defaults.heartbeat_ms(), 10000);
    const SessionTable custom(SessionConfig{.lease_ms = 900, .idle_close_ms = 0});
    EXPECT_EQ(custom.lease_ms(), 900);
    EXPECT_EQ(custom.heartbeat_ms(), 300);
    const SessionTable tiny(SessionConfig{.lease_ms = 2, .idle_close_ms = 0});
    EXPECT_EQ(tiny.heartbeat_ms(), 1);              // 至少 1 毫秒
}

// 30 s 不心跳就过期（边界：距上次心跳 >= lease_ms 即过期）；过期后 heartbeat 抛 session_expired。
TEST(SessionTable, ConnectHeartbeatExpire) {
    SessionTable table(SessionConfig{});
    const auto sid = table.connect(0);
    EXPECT_EQ(sid.size(), 32U);
    EXPECT_TRUE(is_lower_hex(sid)) << sid;

    table.heartbeat(sid, 10000);
    table.heartbeat(sid, 10000 + kLease - 1);   // 距上次 29999 ms：还活着，续到 69999
    const auto hid = table.open_handle(sid, "w1", "alice", false, 40000);
    EXPECT_TRUE(table.expire(39999 + kLease - 1).empty());

    EXPECT_EQ(code_of([&] { table.heartbeat(sid, 39999 + kLease); }), "session_expired");
    EXPECT_EQ(table.expire(39999 + kLease), std::vector<std::string>{hid});
    EXPECT_EQ(code_of([&] { table.heartbeat(sid, 39999 + kLease + 1); }), "session_expired");
    // 已过期是终态：时间就算倒退，也不会复活。
    EXPECT_EQ(code_of([&] { table.heartbeat(sid, 40000); }), "session_expired");
    EXPECT_EQ(code_of([&] { table.heartbeat("no-such-session", 0); }), "session_expired");
}

// 心跳乱序到达时取最晚的一次，到期时间不会被改短。
TEST(SessionTable, HeartbeatKeepsTheLatestRenewal) {
    SessionTable table(SessionConfig{});
    const auto sid = table.connect(0);
    table.heartbeat(sid, 20000);
    table.heartbeat(sid, 5000);
    EXPECT_FALSE(table.world_write_held("w", 0));
    const auto hid = table.open_handle(sid, "w", "alice", false, 20000);
    EXPECT_TRUE(table.world_write_held("w", 20000 + kLease - 1));
    EXPECT_FALSE(table.world_write_held("w", 20000 + kLease));
    EXPECT_EQ(table.expire(20000 + kLease), std::vector<std::string>{hid});
}

// 第二个会话开可写句柄抛 world_held，开只读句柄可以；它照样能写别的世界。
TEST(SessionTable, SecondWriterGetsWorldHeld) {
    SessionTable table(SessionConfig{});
    const auto first = table.connect(0);
    const auto second = table.connect(0);
    table.open_handle(first, "w", "alice", false, 0);

    EXPECT_EQ(code_of([&] { table.open_handle(second, "w", "bob", false, 1); }), "world_held");
    const auto reader = table.open_handle(second, "w", "bob", true, 1);
    const HandleInfo info = table.resolve(reader, 1);
    EXPECT_EQ(info.session_id, second);
    EXPECT_EQ(info.world, "w");
    EXPECT_EQ(info.holder, "bob");
    EXPECT_TRUE(info.read_only);

    EXPECT_NO_THROW(table.open_handle(second, "w2", "bob", false, 1));
    EXPECT_NO_THROW(table.open_handle(first, "w", "carol", false, 1));   // 同一会话再开可写句柄
    EXPECT_EQ(code_of([&] { table.open_handle(first, "w2", "alice", false, 1); }), "world_held");
}

// Review Focus 第 4 条：原会话租约过期后（不论 expire 跑没跑），新会话可写打开成功；原会话的句柄 resolve 抛
// session_expired，不是 handle_closed，客户端据此知道要重新 connect。
TEST(SessionTable, ExpiredSessionReleasesWriteHold) {
    SessionTable table(SessionConfig{});
    const auto old_sid = table.connect(0);
    const auto old_handle = table.open_handle(old_sid, "w", "alice", false, 0);
    const auto new_sid = table.connect(20000);
    EXPECT_EQ(code_of([&] { table.open_handle(new_sid, "w", "bob", false, kLease - 1); }),
              "world_held");

    // 原会话在 30000 过期，expire 还没跑：写占用已经不算它的。
    const auto new_handle = table.open_handle(new_sid, "w", "bob", false, kLease);
    EXPECT_EQ(code_of([&] { (void)table.resolve(old_handle, kLease); }), "session_expired");
    EXPECT_EQ(table.resolve(new_handle, kLease).holder, "bob");

    EXPECT_EQ(table.expire(kLease), std::vector<std::string>{old_handle});
    EXPECT_EQ(code_of([&] { (void)table.resolve(old_handle, kLease + 1); }), "session_expired");
    EXPECT_EQ(code_of([&] { table.call_enter(old_handle, kLease + 1); }), "session_expired");
    EXPECT_EQ(code_of([&] { table.heartbeat(old_sid, kLease + 1); }), "session_expired");
    EXPECT_TRUE(table.world_write_held_by_other("w", old_sid, kLease + 1));
    EXPECT_FALSE(table.world_write_held_by_other("w", new_sid, kLease + 1));
}

// 审查 I1 路径一（裁定 R57）：B 在 A 的租约边界上拿到写占用之后，A 时间戳更早、却迟到的心跳不能让 A 复活，
// 否则两个会话同时占着一个世界的写入。
TEST(SessionTable, LateHeartbeatCannotReviveAfterAnotherSessionTookTheHold) {
    SessionTable table(SessionConfig{});
    const auto sid_a = table.connect(0);
    const auto sid_b = table.connect(20000);
    table.open_handle(sid_a, "w", "alice", false, 0);
    table.open_handle(sid_b, "w", "bob", false, kLease);   // A 在 30000 过期，B 拿到写占用

    EXPECT_EQ(code_of([&] { table.heartbeat(sid_a, kLease - 1); }), "session_expired");
    EXPECT_FALSE(table.world_write_held_by_other("w", sid_b, kLease + 1));
    EXPECT_TRUE(table.world_write_held_by_other("w", sid_a, kLease + 1));
    EXPECT_EQ(code_of([&] { table.open_handle(sid_a, "w", "alice", false, kLease - 1); }),
              "session_expired");
}

// 审查 I1 路径二：resolve 已经报了 session_expired，之后迟到的心跳也被拒绝，句柄不会重新可用。
TEST(SessionTable, LateHeartbeatCannotReviveAfterResolveSawExpiry) {
    SessionTable table(SessionConfig{});
    const auto sid = table.connect(0);
    const auto hid = table.open_handle(sid, "w", "alice", false, 0);
    EXPECT_EQ(code_of([&] { (void)table.resolve(hid, kLease); }), "session_expired");
    EXPECT_EQ(code_of([&] { table.heartbeat(sid, kLease - 1); }), "session_expired");
    EXPECT_EQ(code_of([&] { (void)table.resolve(hid, kLease + 1); }), "session_expired");
    EXPECT_EQ(code_of([&] { (void)table.resolve(hid, kLease - 1); }), "session_expired");
    EXPECT_FALSE(table.world_write_held("w", kLease - 1));
}

// 裁定 R57：任何一条持锁路径看出过期，过期就成了终态。逐条路径验证：先用一个过了边界的 now 观察一次，
// 再用边界之前的 now 心跳，必须被拒绝。
TEST(SessionTable, EveryObservationOfExpiryIsTerminal) {
    using Observe = void (*)(SessionTable&, const std::string&, const std::string&);
    const std::vector<std::pair<const char*, Observe>> paths = {
        {"world_write_held",
         [](SessionTable& table, const std::string&, const std::string&) {
             (void)table.world_write_held("w", kLease);
         }},
        {"world_write_held_by_other",
         [](SessionTable& table, const std::string&, const std::string&) {
             (void)table.world_write_held_by_other("w", "", kLease);
         }},
        {"world_has_live_handles",
         [](SessionTable& table, const std::string&, const std::string&) {
             (void)table.world_has_live_handles("w", kLease);
         }},
        {"may_close_world",
         [](SessionTable& table, const std::string&, const std::string&) {
             (void)table.may_close_world("w", false, kLease + kDay);
         }},
        {"call_enter",
         [](SessionTable& table, const std::string&, const std::string& hid) {
             (void)code_of([&] { table.call_enter(hid, kLease); });
         }},
        {"open_handle",
         [](SessionTable& table, const std::string& sid, const std::string&) {
             (void)code_of([&] { table.open_handle(sid, "w2", "alice", true, kLease); });
         }},
        {"expire",
         [](SessionTable& table, const std::string&, const std::string&) {
             (void)table.expire(kLease);
         }},
    };
    for (const auto& [name, observe] : paths) {
        SessionTable table(SessionConfig{});
        const auto sid = table.connect(0);
        const auto hid = table.open_handle(sid, "w", "alice", false, 0);
        observe(table, sid, hid);
        EXPECT_EQ(code_of([&] { table.heartbeat(sid, kLease - 1); }), "session_expired") << name;
        EXPECT_FALSE(table.world_write_held("w", kLease - 1)) << name;
    }
}

// in_flight 计数正确；close_handle 不等在途调用（等待是调用方的事），之后 resolve 抛 handle_closed。
// 关掉的句柄上没结束的调用照样算进世界的在途数，直到 call_exit。
TEST(SessionTable, CloseHandleWaitsInFlightIsCallersJob) {
    SessionTable table(SessionConfig{.lease_ms = kLease, .idle_close_ms = 1000});
    const auto sid = table.connect(0);
    const auto hid = table.open_handle(sid, "w", "alice", false, 0);
    EXPECT_EQ(table.in_flight(hid), 0);
    table.call_enter(hid, 1);
    table.call_enter(hid, 2);
    EXPECT_EQ(table.in_flight(hid), 2);
    table.call_exit(hid, 3);
    EXPECT_EQ(table.in_flight(hid), 1);

    table.close_handle(hid);   // 立即返回，还有 1 个在途
    EXPECT_EQ(code_of([&] { (void)table.resolve(hid, 4); }), "handle_closed");
    EXPECT_EQ(code_of([&] { table.call_enter(hid, 4); }), "handle_closed");
    EXPECT_EQ(table.in_flight(hid), 1);
    EXPECT_FALSE(table.world_has_live_handles("w", 4));
    EXPECT_FALSE(table.may_close_world("w", false, 100000));   // 在途调用挡着

    table.call_exit(hid, 5);   // 关掉的句柄、过期的会话都不挡退出
    EXPECT_EQ(table.in_flight(hid), 0);
    EXPECT_THROW(table.call_exit(hid, 6), std::logic_error);
    EXPECT_TRUE(table.may_close_world("w", false, 1006));
    EXPECT_EQ(code_of([&] { (void)table.resolve(hid, 7); }), "handle_closed");
}

// 没有配对的 call_enter 时 call_exit 抛 logic_error；在途调用跨过会话过期照样能退出。
TEST(SessionTable, CallExitNeedsAMatchingEnter) {
    SessionTable table(SessionConfig{});
    const auto sid = table.connect(0);
    const auto hid = table.open_handle(sid, "w", "alice", false, 0);
    EXPECT_THROW(table.call_exit(hid, 1), std::logic_error);
    EXPECT_THROW(table.call_exit("no-such-handle", 1), std::logic_error);
    EXPECT_EQ(code_of([&] { table.call_enter("no-such-handle", 1); }), "handle_closed");

    table.call_enter(hid, 1);
    EXPECT_EQ(code_of([&] { table.call_enter(hid, kLease); }), "session_expired");
    EXPECT_EQ(table.in_flight(hid), 1);
    EXPECT_NO_THROW(table.call_exit(hid, kLease + 5));
    EXPECT_EQ(table.in_flight(hid), 0);
}

// 同一会话在同一世界开两个句柄、绑不同 holder（也可以重复绑同一个）。
TEST(SessionTable, MultipleHandlesSameWorldSameSession) {
    SessionTable table(SessionConfig{});
    const auto sid = table.connect(0);
    const auto other = table.connect(0);
    const auto alice = table.open_handle(sid, "w", "alice", false, 0);
    const auto bob = table.open_handle(sid, "w", "bob", false, 0);
    const auto alice_again = table.open_handle(sid, "w", "alice", false, 0);
    EXPECT_EQ((std::set<std::string>{alice, bob, alice_again}).size(), 3U);
    EXPECT_EQ(table.resolve(alice, 1).holder, "alice");
    EXPECT_EQ(table.resolve(bob, 1).holder, "bob");
    EXPECT_EQ(table.resolve(alice_again, 1).holder, "alice");
    EXPECT_EQ(table.resolve(bob, 1).session_id, sid);
    EXPECT_EQ(table.resolve(bob, 1).world, "w");
    EXPECT_FALSE(table.resolve(bob, 1).read_only);

    // 写占用是会话级的：关掉一个句柄，剩下的可写句柄仍占着。
    table.close_handle(alice);
    table.close_handle(alice_again);
    EXPECT_TRUE(table.world_write_held("w", 1));
    EXPECT_EQ(code_of([&] { table.open_handle(other, "w", "carol", false, 1); }), "world_held");
    EXPECT_EQ(table.resolve(bob, 1).holder, "bob");
    table.close_handle(bob);
    EXPECT_FALSE(table.world_write_held("w", 1));
    EXPECT_NO_THROW(table.open_handle(other, "w", "carol", false, 1));
}

// may_close_world：没有活句柄、没有在途调用、最后活动超过 idle_close_ms、不是默认世界，四条逐一否定。
TEST(SessionTable, MayCloseWorldRules) {
    SessionTable table(SessionConfig{.lease_ms = kLease, .idle_close_ms = 1000});
    const auto sid = table.connect(0);
    EXPECT_FALSE(table.may_close_world("never-seen", false, kDay));   // 表里没有记录：兜底不关

    const auto writer = table.open_handle(sid, "w", "alice", false, 0);
    table.heartbeat(sid, 5000);
    EXPECT_FALSE(table.may_close_world("w", false, 5000));   // 否定「没有活句柄」
    table.close_handle(writer);
    EXPECT_TRUE(table.may_close_world("w", false, 5000));    // 四条都满足
    EXPECT_FALSE(table.may_close_world("w", true, 5000));    // 否定「不是默认世界」

    // 只读句柄也是活句柄（审查 M1）：空闲已超时、会话还活着（心跳在 5000，活到 35000），只靠这个只读句柄挡住。
    const auto reader = table.open_handle(sid, "w", "alice", true, 5000);
    EXPECT_FALSE(table.may_close_world("w", false, 6001));
    table.close_handle(reader);
    EXPECT_TRUE(table.may_close_world("w", false, 6001));
    EXPECT_FALSE(table.may_close_world("w", false, 6000));   // 否定「超过空闲时限」：正好 1000 不算超过

    const auto caller = table.open_handle(sid, "w", "alice", true, 6001);
    table.call_enter(caller, 6001);
    table.close_handle(caller);
    EXPECT_FALSE(table.may_close_world("w", false, 20000));  // 否定「没有在途调用」
    table.call_exit(caller, 6002);
    EXPECT_FALSE(table.may_close_world("w", false, 7002));   // call_exit 也算活动
    EXPECT_TRUE(table.may_close_world("w", false, 7003));

    // 会话过期之后，它的句柄不再算活句柄。
    table.open_handle(sid, "w", "alice", false, 7003);
    EXPECT_FALSE(table.may_close_world("w", false, 5000 + kLease - 1));
    EXPECT_TRUE(table.may_close_world("w", false, 5000 + kLease));
}

// 裁定 R50：只被 touch_world 过、没有句柄的世界（看板打开的）过了空闲时限可以关；时限内再 touch 一次就又不能关。
TEST(SessionTable, TouchWorldDrivesIdleWithoutHandles) {
    SessionTable table(SessionConfig{.lease_ms = kLease, .idle_close_ms = 1000});
    table.touch_world("w", 100);
    EXPECT_FALSE(table.may_close_world("w", false, 1100));
    EXPECT_TRUE(table.may_close_world("w", false, 1101));
    table.touch_world("w", 1050);
    EXPECT_FALSE(table.may_close_world("w", false, 1101));
    EXPECT_FALSE(table.may_close_world("w", false, 2050));
    EXPECT_TRUE(table.may_close_world("w", false, 2051));
    table.touch_world("w", 500);   // 乱序的旧时间不把最后活动往回拨
    EXPECT_TRUE(table.may_close_world("w", false, 2051));
    EXPECT_FALSE(table.may_close_world("w", true, 2051));
    EXPECT_FALSE(table.may_close_world("other", false, 2051));
}

// 裁定 R49 (a)：world_write_held = 此刻有没有任何会话在这个世界上持有活着的可写句柄。
TEST(SessionTable, WorldWriteHeldTracksLiveWritableHandles) {
    SessionTable table(SessionConfig{});
    const auto sid = table.connect(0);
    EXPECT_FALSE(table.world_write_held("w", 0));

    const auto reader = table.open_handle(sid, "w", "alice", true, 0);
    EXPECT_FALSE(table.world_write_held("w", 0));            // 只读句柄不算
    EXPECT_TRUE(table.world_has_live_handles("w", 0));

    const auto writer = table.open_handle(sid, "w", "alice", false, 0);
    EXPECT_TRUE(table.world_write_held("w", 0));             // 可写句柄
    EXPECT_FALSE(table.world_write_held("w2", 0));
    EXPECT_FALSE(table.world_write_held_by_other("w", sid, 0));
    EXPECT_TRUE(table.world_write_held_by_other("w", "", 0));   // Task 5.1 的「被客户端占用」
    table.close_handle(writer);
    EXPECT_FALSE(table.world_write_held("w", 0));            // 关闭之后

    const auto writer2 = table.open_handle(sid, "w", "alice", false, 1);
    EXPECT_TRUE(table.world_write_held("w", kLease - 1));
    EXPECT_FALSE(table.world_write_held("w", kLease));       // 过期之后（expire 还没跑）
    EXPECT_FALSE(table.world_has_live_handles("w", kLease));
    EXPECT_EQ(sorted(table.expire(kLease)), sorted({reader, writer2}));
    EXPECT_FALSE(table.world_write_held("w", kLease));
    EXPECT_TRUE(table.expire(kLease + 1).empty());

    // disconnect 释放该会话的全部句柄。
    const auto later = table.connect(kLease);
    const auto held = table.open_handle(later, "w", "bob", false, kLease);
    EXPECT_TRUE(table.world_write_held("w", kLease));
    table.disconnect(later);
    EXPECT_FALSE(table.world_write_held("w", kLease));
    EXPECT_EQ(code_of([&] { (void)table.resolve(held, kLease); }), "handle_closed");
    EXPECT_EQ(code_of([&] { table.heartbeat(later, kLease); }), "session_expired");
    EXPECT_EQ(code_of([&] { table.open_handle(later, "w", "bob", true, kLease); }),
              "session_expired");
    EXPECT_NO_THROW(table.disconnect(later));          // 未知 id 什么都不做
    EXPECT_NO_THROW(table.close_handle("no-such-handle"));
}

// 裁定 R52：过期留下的墓碑让 resolve 一直报 session_expired；超过 24 小时的墓碑由 expire 清掉，之后报 handle_closed。
// 还有在途调用的墓碑不清，等 call_exit。
TEST(SessionTable, ExpiredTombstonesArePurgedAfterADay) {
    SessionTable table(SessionConfig{});
    const auto sid = table.connect(0);
    const auto idle = table.open_handle(sid, "w", "alice", false, 0);
    const auto busy = table.open_handle(sid, "w", "bob", false, 0);
    table.call_enter(busy, 1);
    EXPECT_EQ(sorted(table.expire(kLease)), sorted({idle, busy}));

    EXPECT_TRUE(table.expire(kLease + kDay).empty());   // 正好 24 小时：不清
    EXPECT_EQ(code_of([&] { (void)table.resolve(idle, kLease + kDay); }), "session_expired");
    EXPECT_TRUE(table.expire(kLease + kDay + 1).empty());   // 清墓碑不算「释放」
    EXPECT_EQ(code_of([&] { (void)table.resolve(idle, kLease + kDay + 1); }), "handle_closed");

    EXPECT_EQ(code_of([&] { (void)table.resolve(busy, kLease + kDay + 1); }), "session_expired");
    EXPECT_EQ(table.in_flight(busy), 1);
    table.call_exit(busy, kLease + kDay + 2);
    table.expire(kLease + kDay + 3);
    EXPECT_EQ(code_of([&] { (void)table.resolve(busy, kLease + kDay + 3); }), "handle_closed");
}

// 客户端 close_handle 或 disconnect 清掉墓碑，之后 resolve 报 handle_closed。
TEST(SessionTable, CloseOrDisconnectClearsTombstones) {
    SessionTable table(SessionConfig{});
    const auto sid = table.connect(0);
    const auto first = table.open_handle(sid, "w", "alice", false, 0);
    const auto second = table.open_handle(sid, "w", "bob", true, 0);
    table.expire(kLease);
    table.close_handle(first);
    EXPECT_EQ(code_of([&] { (void)table.resolve(first, kLease); }), "handle_closed");
    EXPECT_EQ(code_of([&] { (void)table.resolve(second, kLease); }), "session_expired");
    table.disconnect(sid);
    EXPECT_EQ(code_of([&] { (void)table.resolve(second, kLease); }), "handle_closed");
}

// 裁定 R54、R60：forget_world 删掉这个世界的最后活动与墓碑；同名世界重建后从「没有记录」开始，不继承旧的空闲时间。
// 还有活句柄（可写或只读）或在途调用时抛 world_in_use，什么都不删。
TEST(SessionTable, ForgetWorldDropsActivityAndTombstones) {
    SessionTable table(SessionConfig{.lease_ms = kLease, .idle_close_ms = 1000});
    EXPECT_NO_THROW(table.forget_world("never-seen", 0));

    const auto sid = table.connect(0);
    const auto reader = table.open_handle(sid, "w", "alice", true, 0);
    EXPECT_EQ(code_of([&] { table.forget_world("w", 1); }), "world_in_use");   // 只读活句柄
    table.close_handle(reader);
    const auto writer = table.open_handle(sid, "w", "alice", false, 0);
    EXPECT_EQ(code_of([&] { table.forget_world("w", 1); }), "world_in_use");   // 可写活句柄
    EXPECT_TRUE(table.world_write_held("w", 1));                                // 拒绝时什么都没删

    const auto other = table.open_handle(sid, "w2", "alice", false, 0);
    table.expire(kLease);   // writer、other 都成墓碑
    EXPECT_EQ(code_of([&] { (void)table.resolve(writer, kLease); }), "session_expired");
    EXPECT_TRUE(table.may_close_world("w", false, kLease));
    table.forget_world("w", kLease);
    EXPECT_EQ(code_of([&] { (void)table.resolve(writer, kLease); }), "handle_closed");
    EXPECT_FALSE(table.may_close_world("w", false, kLease + kDay));   // 没有记录：兜底不关
    // 别的世界不受影响。
    EXPECT_EQ(code_of([&] { (void)table.resolve(other, kLease); }), "session_expired");
    EXPECT_TRUE(table.may_close_world("w2", false, kLease));

    // 同名重建：不继承旧的最后活动，从 touch 的那一刻起算。
    table.touch_world("w", kLease + 5000);
    EXPECT_FALSE(table.may_close_world("w", false, kLease + 6000));
    EXPECT_TRUE(table.may_close_world("w", false, kLease + 6001));
}

// 有在途调用的句柄（关闭了或成了墓碑）挡住 forget_world；call_exit 归零之后可以忘掉。
TEST(SessionTable, ForgetWorldWaitsForInFlightCalls) {
    SessionTable table(SessionConfig{.lease_ms = kLease, .idle_close_ms = 1000});
    const auto sid = table.connect(0);
    const auto hid = table.open_handle(sid, "w", "alice", false, 0);
    table.call_enter(hid, 1);
    table.close_handle(hid);
    EXPECT_EQ(code_of([&] { table.forget_world("w", 2); }), "world_in_use");
    EXPECT_EQ(table.in_flight(hid), 1);
    table.call_exit(hid, 2);
    EXPECT_NO_THROW(table.forget_world("w", 3));
    EXPECT_FALSE(table.may_close_world("w", false, kDay));

    const auto busy = table.open_handle(sid, "w", "alice", false, 3);
    table.call_enter(busy, 3);
    table.expire(kLease);
    EXPECT_EQ(code_of([&] { table.forget_world("w", kLease); }), "world_in_use");
    table.call_exit(busy, kLease + 1);
    table.forget_world("w", kLease + 1);
    EXPECT_EQ(code_of([&] { (void)table.resolve(busy, kLease + 1); }), "handle_closed");
}

// 裁定 R60：forget_world 收 now_ms，判活与 may_close_world 一致。租约已过、但还没有任何调用看出它过期的会话
// 不挡删除，不用先 expire；顺带把它标死，之后迟到的旧心跳也被拒绝。
TEST(SessionTable, ForgetWorldJudgesLivenessAtNow) {
    SessionTable table(SessionConfig{.lease_ms = kLease, .idle_close_ms = 1000});
    const auto sid = table.connect(0);
    const auto writer = table.open_handle(sid, "w", "alice", false, 0);
    EXPECT_EQ(code_of([&] { table.forget_world("w", kLease - 1); }), "world_in_use");
    EXPECT_NO_THROW(table.forget_world("w", kLease));   // 没调 expire，也没别的调用看过这个会话
    EXPECT_EQ(code_of([&] { (void)table.resolve(writer, kLease); }), "handle_closed");
    EXPECT_FALSE(table.may_close_world("w", false, kLease + kDay));
    EXPECT_EQ(code_of([&] { table.heartbeat(sid, kLease - 1); }), "session_expired");
}

// 裁定 R59：可写句柄只要 handle_live(now) 或 in_flight > 0 就占着写入，不论状态（活着、已关、已过期）。
// 会话过期而它的写调用还在提交时，另一个会话不能接手写入，冻结判定也必须给 Shared。
TEST(SessionTable, InFlightWriteOnExpiredSessionKeepsTheHold) {
    SessionTable table(SessionConfig{});
    const auto sid_a = table.connect(0);
    const auto sid_b = table.connect(20000);
    const auto writer = table.open_handle(sid_a, "w", "alice", false, 0);
    table.call_enter(writer, 1000);

    EXPECT_TRUE(table.world_write_held("w", kLease));                  // A 已过期，写调用还在途
    EXPECT_TRUE(table.world_write_held_by_other("w", sid_b, kLease));
    EXPECT_TRUE(table.world_write_held_by_other("w", "", kLease));
    EXPECT_EQ(code_of([&] { table.open_handle(sid_b, "w", "bob", false, kLease); }), "world_held");
    EXPECT_NO_THROW(table.open_handle(sid_b, "w", "bob", true, kLease));   // 只读照样能开
    EXPECT_EQ(table.expire(kLease), std::vector<std::string>{writer});
    EXPECT_TRUE(table.world_write_held("w", kLease + 1));              // 成了墓碑也一样

    table.call_exit(writer, kLease + 2);
    EXPECT_FALSE(table.world_write_held("w", kLease + 2));
    EXPECT_FALSE(table.world_write_held_by_other("w", sid_b, kLease + 2));
    EXPECT_NO_THROW(table.open_handle(sid_b, "w", "bob", false, kLease + 2));
}

// 裁定 R59：关掉一个还有写调用在途的可写句柄，写占用留到 call_exit。
TEST(SessionTable, InFlightWriteOnClosedHandleKeepsTheHold) {
    SessionTable table(SessionConfig{});
    const auto sid_a = table.connect(0);
    const auto sid_b = table.connect(0);
    const auto writer = table.open_handle(sid_a, "w", "alice", false, 0);
    table.call_enter(writer, 1);
    table.close_handle(writer);

    EXPECT_TRUE(table.world_write_held("w", 2));
    EXPECT_TRUE(table.world_write_held_by_other("w", sid_b, 2));
    EXPECT_FALSE(table.world_write_held_by_other("w", sid_a, 2));   // 是 A 自己占着
    EXPECT_EQ(code_of([&] { table.open_handle(sid_b, "w", "bob", false, 2); }), "world_held");
    EXPECT_FALSE(table.world_has_live_handles("w", 2));             // 「活句柄」的定义不变

    table.call_exit(writer, 3);
    EXPECT_FALSE(table.world_write_held("w", 3));
    EXPECT_NO_THROW(table.open_handle(sid_b, "w", "bob", false, 3));
}

// 裁定 R59 的边界：只读句柄的在途调用不占写入；会话过期时没有在途调用，写占用照样立即释放（Review Focus 第 4 条）。
TEST(SessionTable, InFlightReadDoesNotHoldWrite) {
    SessionTable table(SessionConfig{});
    const auto sid_a = table.connect(0);
    const auto sid_b = table.connect(20000);
    const auto reader = table.open_handle(sid_a, "w", "alice", true, 0);
    table.call_enter(reader, 1);
    table.close_handle(reader);
    EXPECT_FALSE(table.world_write_held("w", 2));
    EXPECT_NO_THROW(table.open_handle(sid_b, "w", "bob", false, 2));
    table.call_exit(reader, 3);

    const auto sid_c = table.connect(0);
    table.open_handle(sid_c, "w2", "carol", false, 0);
    const auto idle_writer = table.open_handle(sid_c, "w2", "carol", false, 0);
    table.call_enter(idle_writer, 1);
    table.call_exit(idle_writer, 2);
    EXPECT_FALSE(table.world_write_held("w2", kLease));
    EXPECT_NO_THROW(table.open_handle(sid_b, "w2", "bob", false, kLease + 999));   // B 活到 49999
}

TEST(SessionTable, OpenHandleValidatesItsInputs) {
    SessionTable table(SessionConfig{});
    const auto sid = table.connect(0);
    EXPECT_THROW(table.open_handle(sid, "", "alice", false, 0), std::invalid_argument);
    EXPECT_THROW(table.open_handle(sid, "w", "", false, 0), std::invalid_argument);
    EXPECT_EQ(code_of([&] { table.open_handle("no-such-session", "w", "alice", false, 0); }),
              "session_expired");
    EXPECT_EQ(code_of([&] { table.open_handle(sid, "w", "alice", false, kLease); }),
              "session_expired");
    EXPECT_EQ(code_of([&] { (void)table.resolve("no-such-handle", 0); }), "handle_closed");
}

TEST(SessionTable, ConfigIsValidated) {
    EXPECT_THROW(SessionTable(SessionConfig{.lease_ms = 0, .idle_close_ms = 1}),
                 std::invalid_argument);
    EXPECT_THROW(SessionTable(SessionConfig{.lease_ms = 1, .idle_close_ms = -1}),
                 std::invalid_argument);
    EXPECT_NO_THROW(SessionTable(SessionConfig{.lease_ms = 1, .idle_close_ms = 0}));
    const SessionConfig defaults;
    EXPECT_EQ(defaults.lease_ms, 30000);
    EXPECT_EQ(defaults.idle_close_ms, 600000);
}

// id 是 16 字节随机数的十六进制（32 位小写），会话与句柄各不重复。
TEST(SessionTable, IdsAreRandomHex) {
    SessionTable table(SessionConfig{});
    std::set<std::string> seen;
    for (int round = 0; round < 200; ++round) {
        const auto sid = table.connect(0);
        const auto hid = table.open_handle(sid, "w" + std::to_string(round), "alice", false, 0);
        for (const auto& value : {sid, hid}) {
            EXPECT_EQ(value.size(), 32U);
            EXPECT_TRUE(is_lower_hex(value)) << value;
            EXPECT_TRUE(seen.insert(value).second) << value;
        }
    }
}

WorldRecord record_of(bool seed, bool read_only, bool reclaimable) {
    WorldRecord rec;
    rec.name = "w";
    rec.seed = seed;
    rec.read_only = read_only || seed;
    rec.reclaimable = reclaimable;
    return rec;
}

// 裁定 R51：写入准入。held_by_other 为真抛 world_held；否则 decide_open_mode(rec, for_client_write) 为 ReadOnly 抛
// world_read_only；都不是就通过。普通世界、可回收世界、种子 × held × for_client_write 全部组合。
TEST(WorldWriteAdmission, RequireWritableMatrix) {
    struct Case {
        const char* kind;
        WorldRecord rec;
        bool for_client_write;
        bool held_by_other;
        const char* expected;
    };
    const auto plain = record_of(false, false, false);
    const auto rollout = record_of(false, false, true);
    const auto seed = record_of(true, false, false);
    const std::vector<Case> cases = {
        {"plain", plain, false, false, ""},
        {"plain", plain, true, false, ""},
        {"plain", plain, false, true, "world_held"},
        {"plain", plain, true, true, "world_held"},
        {"reclaimable", rollout, false, false, "world_read_only"},
        {"reclaimable", rollout, true, false, ""},
        {"reclaimable", rollout, false, true, "world_held"},   // 看板写入被客户端占用的可回收世界
        {"reclaimable", rollout, true, true, "world_held"},
        {"seed", seed, false, false, "world_read_only"},
        {"seed", seed, true, false, "world_read_only"},
        {"seed", seed, false, true, "world_held"},
        {"seed", seed, true, true, "world_held"},
    };
    for (const auto& item : cases) {
        EXPECT_EQ(code_of([&] {
                      require_writable(item.rec, item.for_client_write, item.held_by_other);
                  }),
                  item.expected)
            << item.kind << " for_client_write=" << item.for_client_write
            << " held_by_other=" << item.held_by_other;
    }
}

// 裁定 R49 (c)：只有 decide_open_mode(rec, false) == ReadOnly 且没被会话占用写入时才 Frozen，其余 Shared。
TEST(WorldWriteAdmission, FreezeIsSharedWhileHeldForWrite) {
    const auto rollout = record_of(false, false, true);
    EXPECT_EQ(read_only_freeze_for(rollout), ReadOnlyFreeze::Frozen);   // 默认 held_for_write=false
    EXPECT_EQ(read_only_freeze_for(rollout, false), ReadOnlyFreeze::Frozen);
    EXPECT_EQ(read_only_freeze_for(rollout, true), ReadOnlyFreeze::Shared);

    const auto seed = record_of(true, false, false);   // 种子不该有写者，判定照样保守
    EXPECT_EQ(read_only_freeze_for(seed, false), ReadOnlyFreeze::Frozen);
    EXPECT_EQ(read_only_freeze_for(seed, true), ReadOnlyFreeze::Shared);

    const auto frozen = record_of(false, true, false);
    EXPECT_EQ(read_only_freeze_for(frozen, true), ReadOnlyFreeze::Shared);

    const auto plain = record_of(false, false, false);
    EXPECT_EQ(read_only_freeze_for(plain, false), ReadOnlyFreeze::Shared);
    EXPECT_EQ(read_only_freeze_for(plain, true), ReadOnlyFreeze::Shared);
}

}  // namespace
