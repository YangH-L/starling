#pragma once
// Memory events — 按调用方给的稳定 key 记一次写入的收据(迁移 0038 的 events 表;0039 起
// 主键是 (tenant_id, holder_id, key),同一个世界里每个 holder 各有各的事件行)。
//
// rise 的记忆写入要幂等:同一个 key 重放不会再抽取一次。收据行记下 key、原文、
// 说话对象、序号和状态(pending / committed / abandoned)。本头文件放读侧(序号时钟
// next_seq 与待处理清单 pending_events)与写侧(remember_event 与 abandon_event)。
//
// 状态机与查询放在 C++(Starling CLAUDE.md 的边界);Python 绑定只转发,门面只把
// 行包成 PendingEvent。
#include <cstdint>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

#include "starling/extractor/llm_adapter.hpp"
#include "starling/extractor/statement_validator.hpp"
#include "starling/memory/memory_ops.hpp"
#include "starling/persistence/connection.hpp"
#include "starling/persistence/sqlite_adapter.hpp"

namespace starling::memoryops {

// 同一个 key 被用来写另一段文本、重试 pending 行时换了说话对象,或该 key 已被放弃
// (state='abandoned')。收据行是「这个 key 写的是这段文本、对着这位说话对象」的承诺,
// 改文本会让幂等失去意义,换说话对象会让语句落进另一个 scope_parties,所以这里不静默
// 覆盖。三种情形的消息各不相同(different text / different interlocutor / abandoned)。
// std::runtime_error 子类 → pybind register_exception → starling.EventConflict。
class EventConflict : public std::runtime_error {
public:
    explicit EventConflict(const std::string& what) : std::runtime_error(what) {}
};

// events 表里一条 state='pending' 的行,只带调用方恢复所需的字段。holder 是这行的
// holder_id(0039 回填不出主人的旧行是 '',直到被某个 holder 认领)。
struct PendingEventRow {
    std::string key;
    std::string text;
    std::string interlocutor;
    std::int64_t seq = 0;
    std::string failure_category;
    std::string holder;
};

// 本租户下一个可用序号:COALESCE(MAX(seq) + 1, 0)。含 abandoned 行,所以序号只增
// 不复用;空库为 0,第一条事件的 seq 就是 0。
[[nodiscard]] std::int64_t next_seq(persistence::Connection& conn, std::string_view tenant);

// 本租户(整个世界,所有 holder)state='pending' 的行,按世界一级的 seq 升序。
// committed 与 abandoned 不返回。
[[nodiscard]] std::vector<PendingEventRow> pending_events(persistence::Connection& conn,
                                                          std::string_view tenant);

// 按调用方给的稳定 key 记一次写入,崩溃安全、可重放(设计源 §8)。三段:
//
// ① 短事务:按 (tenant_id, holder_id, key) 读行,holder 取 base.holder_id。读不到时若有
//    (tenant_id, '', key) 的无主旧行(迁移 0039 回填不出 holder),在同一事务里把它的
//    holder_id 改成本次的 holder(认领,设计源 §4.3),再按下面的状态机走;状态机抛
//    EventConflict 时事务回滚,认领一并撤销。无行 → seq = next_seq(世界一级),插
//    state='pending' 的行(holder_id、text、interlocutor、text_sha256)后提交。
//    有行且 state='abandoned',或 text_sha256 与本次文本不同 → 抛 EventConflict。
//    state='committed' → 直接返回 outcome="idempotent" 与行里存的 engram_ref /
//    statement_ids,一次 LLM 都不调(不抽取,所以不比对 interlocutor)。
//    state='pending' → 本次的 interlocutor 必须与行里存的相同,不同则抛 EventConflict
//    (同 key 换说话对象是调用方的 bug,语句会落进另一个 scope_parties,不静默纠正);
//    相同则进补齐分支,抽取用行里存的 interlocutor。
// ② 事务外(抽取要走网络,绝不持写事务):payload 是 "[" + key + "] " + text
//    (key 作为前缀写进 Starling 的文本,设计源 4.3),时刻是 epoch_iso + seq 秒。
//    engram 的来源身份前缀是 base.source_prefix + "h:" + holder + ":":同一段话写进两个
//    holder 名下是两个 engram,各自抽取、各得各的语句。
//    补齐分支按来源身份(adapter_name + source_item_id)找 engram,不按 content_hash
//    (同内容可以有多个 engram,每个 holder 一个):先试带 holder 的身份,再试不带 holder
//    的旧身份 base.source_prefix + sha256(payload)[:16](0039 之前崩溃留下的写入)。第一个
//    有语句的 evidence_json 指向它、holder_id 相同的 engram,就把这批 id(按 rowid 升序)
//    当结果,不调 LLM;都没有则照 remember_all 走一遍(用行里存的原文,不是调用方这次传的,
//    来源身份带 holder)。
// ③ 短事务:仅当证据真入库(outcome 为 accepted/idempotent)且抽取未失败,行才改
//    committed,填 engram_ref 与 statement_ids_json、清空 failure_category。其余情形
//    (抽取失败,或 no_store / rejected / source_missing 这三种未入库的 outcome——它们的
//    extraction_failed 都是 false)行一律留 pending 并写 failure_category(抽取失败写原
//    类别,未入库写 "not_stored:<outcome>";有 engram_ref 时一并写)。返回值的
//    failure_category 与行里记的一致(未入库时 remember_all 不填这个字段,这里补上),
//    不因失败而抛:留着 pending 行才让调用方看得见,下次调用照原 seq 重试,记忆不会静默
//    丢失。remember_all 抛异常时行同样留 pending,异常原样上抛。
//    ③ 是一个事务,先在事务里重读收据行,再决定提交还是放弃(修复轮 2 N1):② 在事务外,期间
//    同一个 key 可能被别的调用改过。
//      - 行已 committed(同 key、同文本的另一次调用先跑完,例如客户端超时后的重放):返回行里
//        存的结果(outcome="idempotent"、行里的 engram_ref 与 statement_ids),本次抽取的
//        结果不提交,零写入。所以两次调用并发时,后完成的那次拿到 idempotent,不抛。
//      - 行已 abandoned,或已被 clear_tenant 删掉:零写入,抛 EventConflict(消息说明什么都
//        没写)。语句不会落进一个已放弃的 key 下面。
//      - 仍是 pending:语句(remember_commit_all,嵌套成 SAVEPOINT)与收据在同一个事务里提交。
//    两条收尾 UPDATE 仍带 AND state='pending' 并查受影响行数,作兜底:绝不把终态覆写回
//    committed(设计源 §8:放弃是终局)。
//
// base 提供 tenant_id / holder_id / interlocutor / adapter_name / source_prefix;
// base 的 payload 与 created_at_iso8601 被忽略(由 key+text 与 epoch+seq 推出)。
// base.holder_id 为空 → 抛 std::invalid_argument(pybind 译成 ValueError),在任何 DB 读写
// 之前:'' 是 0039 无主旧行的标记,不能当 holder 用(裁定 R15,与 remember_holders 一致)。
// 重试 pending 行时用行里原来的 seq、text 与 interlocutor,时钟不漂移,也不另分配序号。
// 走 SqliteAdapter(而非裸 Connection)以便经过写准入门。
RememberOutcome remember_event(persistence::SqliteAdapter& adapter,
                               extractor::LLMAdapter& llm,
                               const RememberParams& base,
                               const RememberPrompts& prompts,
                               std::string_view key,
                               std::string_view text,
                               std::string_view epoch_iso,
                               const extractor::ValidationPolicy& policy = {});

// ── 分相入口(记忆服务第 4 期 Task 4.5 修复轮 1):remember_event 的三段,可以分开调 ──
//
// remember_event 就是依次调这三段(单一语义源,进程内 Memory 的行为不变)。服务端的引擎只在 begin 与 finish 持引擎锁,
// extract 在锁外:抽取走网络,几十秒起,同一世界的读不能排在它后面(spec §12)。
//
//   begin   第 1 段的短事务(读行、认领、占位;冲突在这里抛)+ 补齐分支(只读库,不调 LLM)+ remember_prepare(写 engram)。
//           committed 的行直接给出 done(idempotent),之后两段都不用调。
//   extract 只调 LLM(remember_extract_all),不碰库。needs_extraction() 为 false 时什么都不做,返回空 bundle。
//   finish  一个事务:先重读收据行(见上面的 ③)。committed 且 text_sha256 与 begin 时相同 → idempotent、零写入;
//           abandoned 或不在 → EventConflict、零写入;pending 且 seq 与 begin 时相同 → remember_commit_all(要抽取时)与
//           收据 UPDATE 一起提交。committed 而 sha 不同、pending 而 seq 不同 → EventConflict、零写入(裁定 R96 M1)。
//           这两项比对认不全「行被删后同一个 key 又被重建」,见 RememberEventBegun 里 text_sha256 / seq 的注释(裁定 R102)。
//
// RememberEventBegun 在三段之间原样传递,调用方不解构。begin 与 finish 必须在同一个 adapter 上调。
struct RememberEventBegun {
    std::optional<RememberOutcome> done;       // 有值 = 已完成(committed 重入),不必再调 extract / finish
    RememberParams params;                     // 第 2 段的参数(payload、时刻、带 holder 的来源前缀、行里的说话对象)
    std::string key;
    RememberPrepared prepared;                 // remember_prepare 的结果(补齐时为空)
    std::optional<RememberOutcome> completed;  // 补齐分支找回的结果(有值时不抽取,finish 只写收据)
    // begin 读到 / 写下的那一行的 text_sha256 与 seq(最后修复波 A4,裁定 R96 M1):finish 重读时拿它们比,认出一部分「行被删后
    // 同一个 key 又被重建」的情形。限制(裁定 R102):
    //   - clear_tenant 删掉本租户全部 events 行,next_seq 是 MAX(seq)+1,所以清空之后 seq 从 0 重新编号,会复用;
    //   - committed 分支比 sha 是可靠的:重建那次写的是别的文本,sha 一定不同,抛;写的是同一段文本时 finish 返回重建那次的
    //     收据(idempotent),与同 key 重放的语义一致,不写任何东西;
    //   - pending 分支比 seq 只能认出「清空之后、重建这个 key 之前已经有过别的写入」的情形(重建的行因此拿到别的 seq)。清空之后
    //     第一条写入就是重建这个 key 时,两边 seq 相同(多半都是 0),比不出来:finish 照常收尾,可能改写重建行的 failure_category。
    //   真正的修法要给行一个删除重建后不复用的身份(nonce 列或清空代数),要做迁移,推迟(裁定 R102)。
    std::string text_sha256;
    std::int64_t seq = 0;
    [[nodiscard]] bool needs_extraction() const {
        return !done && !completed && prepared.should_extract;
    }
};

// 第 1 段 + 补齐 + prepare。错误同 remember_event 的第 1 段:空 holder → std::invalid_argument(零读写);
// 冲突 → EventConflict(认领随事务回滚)。
[[nodiscard]] RememberEventBegun remember_event_begin(persistence::SqliteAdapter& adapter,
                                                      const RememberParams& base,
                                                      std::string_view key,
                                                      std::string_view text,
                                                      std::string_view epoch_iso);

// 第 2 段:纯 LLM。不持事务,不读写库。
[[nodiscard]] RememberLlmBundle remember_event_extract(persistence::SqliteAdapter& adapter,
                                                       extractor::LLMAdapter& llm,
                                                       const RememberEventBegun& begun,
                                                       const RememberPrompts& prompts,
                                                       const extractor::ValidationPolicy& policy = {});

// 第 3 段:commit + 收据。返回值与 remember_event 相同。
[[nodiscard]] RememberOutcome remember_event_finish(persistence::SqliteAdapter& adapter,
                                                    extractor::LLMAdapter& llm,
                                                    const RememberEventBegun& begun,
                                                    const RememberLlmBundle& extracted,
                                                    const extractor::ValidationPolicy& policy = {});

// 放弃 holder 名下的一个 key:一个短事务,把 state='pending' 的行改成终态 'abandoned'。
// 放弃是终局,之后这个 holder 再对同一个 key remember_event 一律抛 EventConflict(不论
// 文本是否相同);别的 holder 的同名 key 不受影响。行按 (tenant, holder, key) 找,找不到
// 时与 remember_event 一样认领 (tenant, '', key) 的无主旧行;抛出时认领一并回滚。
//
//   pending   → UPDATE state='abandoned'(failure_category 原样留着,供事后查原因)
//   abandoned → 不动,返回(幂等)
//   committed → 抛 EventConflict:语句已经落库,放弃改不掉它,不能让收据说谎
//   无此行    → 抛 std::out_of_range:未知 key 多半是调用方拼错了,不静默。key 在本租户
//               别的 holder 名下时,消息点名这些 holder(按 holder_id 升序,裁定 R14),
//               例如 "abandon_event: key 'K' is recorded under holder(s) pa, pb, not 'world'";
//               谁都没有这个 key 才是 "no such key"。
//               std::out_of_range 不继承 EventConflict;pybind11 默认把它译成
//               IndexError,绑定层在转发时改译成 KeyError(见 memory_abandon_event)。
//   holder 为空 → 抛 std::invalid_argument(ValueError),在任何 DB 读写之前(裁定 R15)。
//
// 只改事件行,不碰 statements 与 engrams。注意:pending 行并不保证没有语句——
// remember_event 的第 2 段(抽取与入库)跑完、第 3 段写收据前崩溃时,语句与 engram 已经
// 落了库,而行还是 pending。放弃这样一行,那批语句就留在 library 里,而它的 key 已经死
// 了:之后 remember_event 一律抛 EventConflict,再也不会走补齐分支把它收成 committed。
// 清掉这些语句不在本函数的职责里(要另设计一条回收路径),调用方需知道这个后果。
// seq 不回收:next_seq 的 MAX(seq)+1 含 abandoned 行。
// 走 SqliteAdapter 以便经过写准入门(门前抛 = 零 DB 写)。
void abandon_event(persistence::SqliteAdapter& adapter,
                   std::string_view tenant,
                   std::string_view holder,
                   std::string_view key);

}  // namespace starling::memoryops
