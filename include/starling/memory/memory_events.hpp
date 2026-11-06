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
//    补齐分支先按该 payload 的 engram content_hash 找 engrams 行,找到且有语句的
//    evidence_json 指向它、holder_id 相同时,直接把这批 id(按 rowid 升序)当结果,
//    不调 LLM;否则照 remember_all 走一遍(用行里存的原文,不是调用方这次传的)。
// ③ 短事务:仅当证据真入库(outcome 为 accepted/idempotent)且抽取未失败,行才改
//    committed,填 engram_ref 与 statement_ids_json、清空 failure_category。其余情形
//    (抽取失败,或 no_store / rejected / source_missing 这三种未入库的 outcome——它们的
//    extraction_failed 都是 false)行一律留 pending 并写 failure_category(抽取失败写原
//    类别,未入库写 "not_stored:<outcome>";有 engram_ref 时一并写)。返回值的
//    failure_category 与行里记的一致(未入库时 remember_all 不填这个字段,这里补上),
//    不因失败而抛:留着 pending 行才让调用方看得见,下次调用照原 seq 重试,记忆不会静默
//    丢失。remember_all 抛异常时行同样留 pending,异常原样上抛。
//    两条收尾 UPDATE 都带 AND state='pending' 并查受影响行数:② 在事务外,期间这行可能
//    已被 abandon_event 改成终态 'abandoned',或被 clear_tenant 删掉。零行时抛
//    EventConflict,消息点明语句可能已入库。不设这道围栏就会把终态覆写回 committed,
//    复活一个已判终局的 key(设计源 §8:放弃是终局),或在行已不存在时静默收尾、让调用
//    方以为收据写成了。
//
// base 提供 tenant_id / holder_id / interlocutor / adapter_name / source_prefix;
// base 的 payload 与 created_at_iso8601 被忽略(由 key+text 与 epoch+seq 推出)。
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

// 放弃 holder 名下的一个 key:一个短事务,把 state='pending' 的行改成终态 'abandoned'。
// 放弃是终局,之后这个 holder 再对同一个 key remember_event 一律抛 EventConflict(不论
// 文本是否相同);别的 holder 的同名 key 不受影响。行按 (tenant, holder, key) 找,找不到
// 时与 remember_event 一样认领 (tenant, '', key) 的无主旧行;抛出时认领一并回滚。
//
//   pending   → UPDATE state='abandoned'(failure_category 原样留着,供事后查原因)
//   abandoned → 不动,返回(幂等)
//   committed → 抛 EventConflict:语句已经落库,放弃改不掉它,不能让收据说谎
//   无此行    → 抛 std::out_of_range:未知 key 多半是调用方拼错了,不静默。
//               std::out_of_range 不继承 EventConflict;pybind11 默认把它译成
//               IndexError,绑定层在转发时改译成 KeyError(见 memory_abandon_event)。
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
