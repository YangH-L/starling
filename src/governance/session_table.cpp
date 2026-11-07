// session_table.cpp — 会话、句柄、写占用、在途调用与空闲判定（spec §11）。规则见 session_table.hpp 文件头。
#include "starling/governance/session_table.hpp"

#include "starling/governance/world_store.hpp"

#include <algorithm>
#include <array>
#include <iterator>
#include <stdexcept>

namespace starling::governance {

namespace {

// 墓碑留多久（裁定 R52）：过期超过这么久、没有在途调用的墓碑由 expire 清掉。
constexpr std::int64_t kTombstoneKeepMs = 86400000;
constexpr std::size_t kIdBytes = 16;

}  // namespace

SessionTable::SessionTable(SessionConfig cfg) : cfg_(cfg) {
    if (cfg_.lease_ms <= 0) { throw std::invalid_argument("lease_ms must be > 0"); }
    if (cfg_.idle_close_ms < 0) { throw std::invalid_argument("idle_close_ms must be >= 0"); }
}

// 16 字节随机数 → 32 位小写十六进制：每次从 random_device 取 32 位，四次凑满 16 字节。熵源由标准库决定：
//  - libc++（Darwin）：arc4random（__config 在 Apple 平台定义 _LIBCPP_USING_ARC4_RANDOM）。
//  - libstdc++（Linux CI 上的 clang-17 默认用它）：默认 token 依次试 rdseed、rdrand（二者都要求 Intel/AMD 且
//    CPUID 有对应位）、darn（POWER9）、arc4random、getentropy、/dev/urandom，第一个可用的胜出；都不可用时构造抛
//    std::runtime_error（GCC 13 libstdc++-v3/src/c++11/random.cc 的 _M_init）。只有编译期一个熵源都没配上时，
//    libstdc++ 才退回确定性的 mt19937；Linux 与 Darwin 都不属于这种情况。
// 调用方持锁（random_device 的 operator() 不保证线程安全）。
std::string SessionTable::fresh_id() {
    static constexpr std::array<char, 16> kDigits = {'0', '1', '2', '3', '4', '5', '6', '7',
                                                     '8', '9', 'a', 'b', 'c', 'd', 'e', 'f',};
    std::string out;
    out.reserve(kIdBytes * 2);
    for (std::size_t word = 0; word < kIdBytes / 4; ++word) {
        const auto bits = static_cast<std::uint32_t>(entropy_());
        for (unsigned shift = 32; shift > 0; shift -= 4) {
            out.push_back(kDigits.at((bits >> (shift - 4)) & 0xFU));
        }
    }
    return out;
}

// 裁定 R57：任何持锁路径（heartbeat、open_handle、resolve、call_enter、world_write_held*、world_has_live_handles、
// may_close_world、expire）第一次用 now_ms 看出会话已过期，就把它标成 dead。从此过期是终态：别的会话可能已据此拿到
// 写占用，客户端可能已收到 session_expired，时间戳更早却迟到的心跳不能再让它复活。dead 是 mutable，只在持 mutex_ 时改。
bool SessionTable::session_live(const std::string& session_id, std::int64_t now_ms) const {
    const auto pos = sessions_.find(session_id);
    if (pos == sessions_.end() || pos->second.dead) { return false; }
    if (now_ms - pos->second.last_heartbeat_ms >= cfg_.lease_ms) {
        pos->second.dead = true;
        return false;
    }
    return true;
}

bool SessionTable::handle_live(const Handle& handle, std::int64_t now_ms) const {
    return handle.state == HandleState::Open && session_live(handle.info.session_id, now_ms);
}

const SessionTable::Handle& SessionTable::checked_handle(const std::string& handle_id,
                                                         std::int64_t now_ms) const {
    const auto pos = handles_.find(handle_id);
    if (pos == handles_.end() || pos->second.state == HandleState::Closed) {
        throw WorldStoreError("handle_closed", handle_id);
    }
    const Handle& handle = pos->second;
    if (handle.state == HandleState::Expired || !session_live(handle.info.session_id, now_ms)) {
        throw WorldStoreError("session_expired", "the session of handle " + handle_id);
    }
    return handle;
}

// 裁定 R59：可写句柄活着，或者还有在途调用（不论 Open、Closed、Expired），都算占着写入。
bool SessionTable::write_held_by_other(const std::string& world, const std::string& session_id,
                                       std::int64_t now_ms) const {
    return std::ranges::any_of(handles_, [&](const auto& entry) {
        const Handle& handle = entry.second;
        return !handle.info.read_only && handle.info.world == world &&
               handle.info.session_id != session_id &&
               (handle.in_flight > 0 || handle_live(handle, now_ms));
    });
}

void SessionTable::note_activity(const std::string& world, std::int64_t now_ms) {
    auto [pos, inserted] = last_activity_ms_.try_emplace(world, now_ms);
    if (!inserted) { pos->second = std::max(pos->second, now_ms); }
}

void SessionTable::release_handle(std::map<std::string, Handle>::iterator pos) {
    if (pos->second.in_flight > 0) {
        pos->second.state = HandleState::Closed;
    } else {
        handles_.erase(pos);
    }
}

std::string SessionTable::connect(std::int64_t now_ms) {
    const std::scoped_lock lock(mutex_);
    std::string session_id = fresh_id();
    while (sessions_.contains(session_id)) { session_id = fresh_id(); }
    sessions_.emplace(session_id, Session{.last_heartbeat_ms = now_ms, .dead = false});
    return session_id;
}

void SessionTable::heartbeat(const std::string& session_id, std::int64_t now_ms) {
    const std::scoped_lock lock(mutex_);
    if (!session_live(session_id, now_ms)) { throw WorldStoreError("session_expired", session_id); }
    Session& session = sessions_.at(session_id);
    session.last_heartbeat_ms = std::max(session.last_heartbeat_ms, now_ms);
}

void SessionTable::disconnect(const std::string& session_id) {
    const std::scoped_lock lock(mutex_);
    sessions_.erase(session_id);
    for (auto pos = handles_.begin(); pos != handles_.end();) {
        const auto next = std::next(pos);
        if (pos->second.info.session_id == session_id && pos->second.state != HandleState::Closed) {
            release_handle(pos);
        }
        pos = next;
    }
}

std::string SessionTable::open_handle(const std::string& session_id, const std::string& world,
                                      const std::string& holder, bool read_only,
                                      std::int64_t now_ms) {
    if (world.empty()) { throw std::invalid_argument("world must not be empty"); }
    if (holder.empty()) { throw std::invalid_argument("holder must not be empty"); }
    const std::scoped_lock lock(mutex_);
    if (!session_live(session_id, now_ms)) { throw WorldStoreError("session_expired", session_id); }
    if (!read_only && write_held_by_other(world, session_id, now_ms)) {
        throw WorldStoreError("world_held", world);
    }
    std::string handle_id = fresh_id();
    while (handles_.contains(handle_id)) { handle_id = fresh_id(); }
    handles_.emplace(handle_id,
                     Handle{.info = HandleInfo{.session_id = session_id, .world = world,
                                               .holder = holder, .read_only = read_only,},
                            .state = HandleState::Open, .expired_at_ms = 0, .in_flight = 0,});
    note_activity(world, now_ms);
    return handle_id;
}

void SessionTable::close_handle(const std::string& handle_id) {
    const std::scoped_lock lock(mutex_);
    const auto pos = handles_.find(handle_id);
    if (pos == handles_.end() || pos->second.state == HandleState::Closed) { return; }
    release_handle(pos);
}

HandleInfo SessionTable::resolve(const std::string& handle_id, std::int64_t now_ms) const {
    const std::scoped_lock lock(mutex_);
    return checked_handle(handle_id, now_ms).info;
}

void SessionTable::call_enter(const std::string& handle_id, std::int64_t now_ms) {
    const std::scoped_lock lock(mutex_);
    static_cast<void>(checked_handle(handle_id, now_ms));
    Handle& handle = handles_.at(handle_id);
    ++handle.in_flight;
    note_activity(handle.info.world, now_ms);
}

void SessionTable::call_exit(const std::string& handle_id, std::int64_t now_ms) {
    const std::scoped_lock lock(mutex_);
    const auto pos = handles_.find(handle_id);
    if (pos == handles_.end() || pos->second.in_flight <= 0) {
        throw std::logic_error("call_exit without a matching call_enter: " + handle_id);
    }
    --pos->second.in_flight;
    note_activity(pos->second.info.world, now_ms);
    if (pos->second.in_flight == 0 && pos->second.state == HandleState::Closed) {
        handles_.erase(pos);
    }
}

std::int64_t SessionTable::in_flight(const std::string& handle_id) const {
    const std::scoped_lock lock(mutex_);
    const auto pos = handles_.find(handle_id);
    return pos == handles_.end() ? 0 : pos->second.in_flight;
}

std::vector<std::string> SessionTable::expire(std::int64_t now_ms) {
    const std::scoped_lock lock(mutex_);
    std::vector<std::string> released;
    for (auto& [handle_id, handle] : handles_) {
        if (handle.state == HandleState::Open && !session_live(handle.info.session_id, now_ms)) {
            handle.state = HandleState::Expired;
            handle.expired_at_ms = now_ms;
            released.push_back(handle_id);
        }
    }
    for (auto pos = sessions_.begin(); pos != sessions_.end();) {
        pos = session_live(pos->first, now_ms) ? std::next(pos) : sessions_.erase(pos);
    }
    for (auto pos = handles_.begin(); pos != handles_.end();) {
        const Handle& handle = pos->second;
        const bool stale = handle.state == HandleState::Expired && handle.in_flight == 0 &&
                           now_ms - handle.expired_at_ms > kTombstoneKeepMs;
        pos = stale ? handles_.erase(pos) : std::next(pos);
    }
    return released;
}

void SessionTable::touch_world(const std::string& world, std::int64_t now_ms) {
    const std::scoped_lock lock(mutex_);
    note_activity(world, now_ms);
}

bool SessionTable::world_has_live_handles(const std::string& world, std::int64_t now_ms) const {
    const std::scoped_lock lock(mutex_);
    return std::ranges::any_of(handles_, [&](const auto& entry) {
        return entry.second.info.world == world && handle_live(entry.second, now_ms);
    });
}

bool SessionTable::world_write_held(const std::string& world, std::int64_t now_ms) const {
    const std::scoped_lock lock(mutex_);
    return write_held_by_other(world, "", now_ms);   // session_id 从不为空，"" 即任何会话
}

bool SessionTable::world_write_held_by_other(const std::string& world,
                                             const std::string& session_id,
                                             std::int64_t now_ms) const {
    const std::scoped_lock lock(mutex_);
    return write_held_by_other(world, session_id, now_ms);
}

bool SessionTable::may_close_world(const std::string& world, bool is_default,
                                   std::int64_t now_ms) const {
    if (is_default) { return false; }
    const std::scoped_lock lock(mutex_);
    const auto activity = last_activity_ms_.find(world);
    if (activity == last_activity_ms_.end()) { return false; }   // 没有记录：兜底不关
    if (now_ms - activity->second <= cfg_.idle_close_ms) { return false; }
    return std::ranges::none_of(handles_, [&](const auto& entry) {
        const Handle& handle = entry.second;
        return handle.info.world == world && (handle.in_flight > 0 || handle_live(handle, now_ms));
    });
}

void SessionTable::forget_world(const std::string& world, std::int64_t now_ms) {
    const std::scoped_lock lock(mutex_);
    // 判活与 may_close_world 一致（裁定 R60）；handle_live 经 session_live，顺带把已过期的会话标死。
    const bool in_use = std::ranges::any_of(handles_, [&](const auto& entry) {
        const Handle& handle = entry.second;
        return handle.info.world == world && (handle.in_flight > 0 || handle_live(handle, now_ms));
    });
    if (in_use) {
        throw WorldStoreError("world_in_use", world + " still has live handles or in-flight calls");
    }
    last_activity_ms_.erase(world);
    std::erase_if(handles_, [&](const auto& entry) { return entry.second.info.world == world; });
}

}  // namespace starling::governance
