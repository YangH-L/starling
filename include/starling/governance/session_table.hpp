#pragma once

// session_table.hpp — 记忆服务的客户端会话、句柄、写占用、在途调用与空闲判定（spec §11，Task 4.2）。
//
// 全部状态机在这里，Python 只把 HTTP 请求转成调用，并把当前时间传进来：每个方法收 now_ms，C++ 不读墙钟。
// now_ms 应取单调时钟（Python 用 time.monotonic），时间倒退不报错，按下面各条的 max 规则处理。
//
// 会话：connect 分配 session_id，租约 lease_ms。只有 heartbeat 续约（取 max，乱序的旧心跳不缩短租约），
// 调用不续约。距最后一次心跳 >= lease_ms 即过期；过期是终态，过期后的 heartbeat 与 open_handle 抛
// session_expired，客户端要重新 connect。disconnect 释放该会话的全部句柄。
//
// 句柄：open_handle 在会话下登记一个句柄，绑定 world 与 holder。同一个会话可以在同一个世界开多个句柄，
// holder 可以相同。句柄「活着」= 没关闭，且所属会话此刻没过期（不论 expire 跑没跑）。
//
// 写占用：一个世界任一时刻至多一个会话持有活着的可写句柄；别的会话开可写句柄抛 world_held，只读句柄可以开。
// 原会话的租约一过期，写占用立即不算它的（expire 还没跑也一样，Review Focus 第 4 条）。
//
// 过期与墓碑：expire(now) 把过期会话的句柄标成墓碑并返回这些 handle_id（同时释放写占用），会话条目删掉。
// 对墓碑 resolve / call_enter 抛 session_expired（不是 handle_closed），客户端据此知道要重新 connect、
// 不自动重开句柄。墓碑由 close_handle、disconnect 清掉，或者由 expire 在它过期 24 小时之后清掉
// （now - expired_at > 86400000，裁定 R52）；清掉之后按未知句柄处理，抛 handle_closed。
//
// 在途调用：Python 在每次门面调用前后报 call_enter / call_exit。call_enter 按 resolve 的规则检查句柄；
// call_exit 不检查（句柄关了、会话过期了，在途调用照样要能退出）。句柄关闭或过期后，它还没结束的调用
// 照样算进世界的在途数，直到 call_exit；这样的句柄条目（含墓碑）留到在途数归零。close_handle 不等在途调用，
// 等待由调用方负责。
//
// 空闲判定（may_close_world）：没有活句柄、没有在途调用、now - 最后活动 > idle_close_ms、不是默认世界。
// 最后活动由 open_handle、call_enter、call_exit、touch_world 更新（取 max，裁定 R50）；看板没有会话，
// 由调用方在引擎打开时和看板每次按世界取租约时调 touch_world。表里没有记录的世界不判可关（兜底）。
//
// id：session_id 与 handle_id 都是 std::random_device 取 16 字节、转成 32 位小写十六进制。
//
// 线程安全：每个公开方法持内部的一把互斥锁，可在多线程间共享。
// 错误：业务错误抛 WorldStoreError（world_held / session_expired / handle_closed）；参数错误抛
// std::invalid_argument；没有配对 call_enter 的 call_exit 抛 std::logic_error（调用方的程序错误）。

#include <cstdint>
#include <map>
#include <mutex>
#include <random>
#include <string>
#include <vector>

namespace starling::governance {

struct SessionConfig {
    std::int64_t lease_ms = 30000;        // 会话租约；> 0
    std::int64_t idle_close_ms = 600000;  // 空闲多久可关世界；>= 0
};

struct HandleInfo {
    std::string session_id, world, holder;
    bool read_only = false;
};

class SessionTable {
public:
    // lease_ms <= 0 或 idle_close_ms < 0 抛 std::invalid_argument。
    explicit SessionTable(SessionConfig cfg);

    // 返回新的 session_id。
    std::string connect(std::int64_t now_ms);
    // 续约。未知、已断开或已过期的会话抛 session_expired。
    void heartbeat(const std::string& session_id, std::int64_t now_ms);
    // 释放该会话的全部句柄（含已过期留下的墓碑），删掉会话。未知 id 什么都不做。
    void disconnect(const std::string& session_id);

    // 返回新的 handle_id。world 或 holder 为空抛 std::invalid_argument；会话未知或已过期抛 session_expired；
    // 可写打开而别的会话持有这个世界的活可写句柄抛 world_held。更新世界的最后活动。
    std::string open_handle(const std::string& session_id, const std::string& world,
                            const std::string& holder, bool read_only, std::int64_t now_ms);
    // 释放一个句柄（含墓碑）；有在途调用时条目留到 call_exit 归零。未知或已关闭的 id 什么都不做。
    void close_handle(const std::string& handle_id);
    // 未知或已关闭抛 handle_closed；会话已过期（含墓碑）抛 session_expired。
    [[nodiscard]] HandleInfo resolve(const std::string& handle_id, std::int64_t now_ms) const;

    // 按 resolve 的规则检查，然后在途数加一、更新世界的最后活动。
    void call_enter(const std::string& handle_id, std::int64_t now_ms);
    // 在途数减一、更新世界的最后活动；不看句柄与会话的状态。在途数为 0 或句柄未知抛 std::logic_error。
    void call_exit(const std::string& handle_id, std::int64_t now_ms);
    // 这个句柄此刻的在途调用数；未知句柄为 0。
    [[nodiscard]] std::int64_t in_flight(const std::string& handle_id) const;

    // 回收过期会话：返回本次变成墓碑的 handle_id（被释放的句柄），并清掉过期超过 24 小时、没有在途调用的墓碑。
    std::vector<std::string> expire(std::int64_t now_ms);
    // 记一次世界活动（看板打开、看板按世界取租约）。
    void touch_world(const std::string& world, std::int64_t now_ms);

    // 这个世界此刻有没有活句柄（可写或只读）。
    [[nodiscard]] bool world_has_live_handles(const std::string& world, std::int64_t now_ms) const;
    // 此刻有没有任何会话在这个世界上持有活着的可写句柄（裁定 R49 (a)）。
    [[nodiscard]] bool world_write_held(const std::string& world, std::int64_t now_ms) const;
    // 此刻有没有 session_id 之外的会话持有这个世界的活可写句柄。session_id 传空串即「任何会话」。
    [[nodiscard]] bool world_write_held_by_other(const std::string& world,
                                                 const std::string& session_id,
                                                 std::int64_t now_ms) const;
    // 空闲判定，规则见文件头。
    [[nodiscard]] bool may_close_world(const std::string& world, bool is_default,
                                       std::int64_t now_ms) const;

private:
    enum class HandleState : std::uint8_t { Open, Closed, Expired };
    struct Session {
        std::int64_t last_heartbeat_ms = 0;
        bool dead = false;   // heartbeat 已发现它过期（过期是终态，时间倒退也不复活）
    };
    struct Handle {
        HandleInfo info;
        HandleState state = HandleState::Open;
        std::int64_t expired_at_ms = 0;
        std::int64_t in_flight = 0;
    };

    [[nodiscard]] bool session_live(const std::string& session_id, std::int64_t now_ms) const;
    [[nodiscard]] bool handle_live(const Handle& handle, std::int64_t now_ms) const;
    [[nodiscard]] const Handle& checked_handle(const std::string& handle_id,
                                               std::int64_t now_ms) const;
    [[nodiscard]] bool write_held_by_other(const std::string& world, const std::string& session_id,
                                           std::int64_t now_ms) const;
    [[nodiscard]] std::string fresh_id();
    void note_activity(const std::string& world, std::int64_t now_ms);
    // 把句柄从「开着」或「墓碑」变成「已关闭」：没有在途调用就删掉条目，有就留着等 call_exit。
    void release_handle(std::map<std::string, Handle>::iterator pos);

    SessionConfig cfg_;
    mutable std::mutex mutex_;
    std::random_device entropy_;
    std::map<std::string, Session> sessions_;
    std::map<std::string, Handle> handles_;
    std::map<std::string, std::int64_t> last_activity_ms_;   // 世界名 → 最后活动
};

}  // namespace starling::governance
