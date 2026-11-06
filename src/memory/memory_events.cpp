#include "starling/memory/memory_events.hpp"

#include <sqlite3.h>

#include <cstdio>
#include <ctime>
#include <format>
#include <stdexcept>
#include <utility>

#include <nlohmann/json.hpp>

#include "starling/crypto/sha256.hpp"
#include "starling/governance/write_gate.hpp"
#include "starling/persistence/stmt.hpp"

namespace starling::memoryops {

namespace {

// 读一列 TEXT;NULL 当空串(events 的这几列都是 NOT NULL,这里只是防御)。
std::string column_string(sqlite3_stmt* stmt, int col) {
    // NOLINTNEXTLINE(cppcoreguidelines-pro-type-reinterpret-cast)
    const auto* text = reinterpret_cast<const char*>(sqlite3_column_text(stmt, col));
    return text != nullptr ? std::string(text) : std::string();
}

// events 表里一条行的全部字段(写侧用)。found=false 表示这个 key 还没有行。
struct EventRow {
    bool found = false;
    std::string state;
    std::string text;
    std::string text_sha256;
    std::string engram_ref;              // 列可空,NULL 读成空串
    std::string statement_ids_json = "[]";
    std::int64_t seq = 0;
    std::string interlocutor;            // 首次写入时的说话对象;重试以它为准
};

EventRow read_event(persistence::Connection& conn, std::string_view tenant,
                    std::string_view holder, std::string_view key) {
    persistence::Stmt stmt(conn.raw(),
                           "SELECT state, text, text_sha256, COALESCE(engram_ref,''), "
                           "statement_ids_json, seq, interlocutor "
                           "FROM events WHERE tenant_id=?1 AND holder_id=?2 AND key=?3");
    stmt.bind(tenant, holder, key);
    EventRow row;
    if (!stmt.step_row()) {
        return row;
    }
    row.found              = true;
    row.state              = column_string(stmt.raw(), 0);
    row.text               = column_string(stmt.raw(), 1);
    row.text_sha256        = column_string(stmt.raw(), 2);
    row.engram_ref         = column_string(stmt.raw(), 3);
    row.statement_ids_json = column_string(stmt.raw(), 4);
    row.seq                = sqlite3_column_int64(stmt.raw(), 5);
    row.interlocutor       = column_string(stmt.raw(), 6);
    return row;
}

// 按 (tenant, holder, key) 读行;读不到再找 (tenant, '', key) 的无主旧行(迁移 0039 回填
// 不出 holder 的那些),找到就把它的 holder_id 改成本次的 holder,再当作自己的行返回
// (设计源 §4.3 的认领规则:旧库都是一库一 holder,第一个碰到它的 holder 就是它的主人)。
// 必须在调用方的写事务里调:认领与随后的状态机判断同进同退——状态机抛 EventConflict
// 时事务回滚,认领也一并撤销,被拒绝的调用不在库里留下任何改动。
// 两个调用方都先经 require_holder 拒绝了空 holder;下面的 holder.empty() 只是防御。
EventRow read_or_claim_event(persistence::Connection& conn, std::string_view tenant,
                             std::string_view holder, std::string_view key) {
    EventRow row = read_event(conn, tenant, holder, key);
    if (row.found || holder.empty()) {
        return row;
    }
    row = read_event(conn, tenant, /*holder=*/"", key);
    if (!row.found) {
        return row;
    }
    persistence::Stmt claim(conn.raw(),
                            "UPDATE events SET holder_id=?1 "
                            "WHERE tenant_id=?2 AND holder_id='' AND key=?3");
    claim.bind(holder, tenant, key);
    claim.step_done();
    return row;
}

std::vector<std::string> parse_statement_ids(const std::string& json_text) {
    std::vector<std::string> ids;
    const auto parsed = nlohmann::json::parse(json_text, nullptr, /*allow_exceptions=*/false);
    if (!parsed.is_array()) {
        return ids;   // 列被外部写坏时当空,不崩
    }
    for (const auto& item : parsed) {
        if (item.is_string()) {
            ids.push_back(item.get<std::string>());
        }
    }
    return ids;
}

// 事件时钟:epoch_iso 加 seq 秒(设计源 4.3——时刻由事件行的 seq 推出,
// 所以同一 key 重试时时戳不漂移)。解析不出来就原样返回,坏时戳不致崩。
std::string add_seconds_to_iso(std::string_view epoch_iso, std::int64_t seconds) {
    // 非 const:const 的串在 return 时无法自动移动(performance-no-automatic-move)。
    std::string iso(epoch_iso);
    int year = 0;
    int month = 0;
    int day = 0;
    int hour = 0;
    int minute = 0;
    int second = 0;
    // 有意用 sscanf 且不改 strtol:返回值 != 6 即解析失败,失败就原样返回坏时戳(见上方
    // 注释),字段在失败时保持初值 0、不被使用,所以「不报转换错误」在这里不是缺陷。
    // 换成 strtol 要手写分隔符与逐段范围校验,会动这个有意的容错行为。
    // NOLINTNEXTLINE(cert-err34-c,bugprone-unchecked-string-to-number-conversion,cppcoreguidelines-pro-type-vararg)
    if (std::sscanf(iso.c_str(), "%d-%d-%dT%d:%d:%dZ", &year, &month, &day, &hour, &minute,
                    &second) != 6) {
        return iso;
    }
    std::tm parts{};
    parts.tm_year = year - 1900;
    parts.tm_mon = month - 1;
    parts.tm_mday = day;
    parts.tm_hour = hour;
    parts.tm_min = minute;
    parts.tm_sec = second;
    const std::time_t epoch = timegm(&parts) + static_cast<std::time_t>(seconds);
    std::tm out{};
    gmtime_r(&epoch, &out);
    return std::format("{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z",
                       out.tm_year + 1900, out.tm_mon + 1, out.tm_mday,
                       out.tm_hour, out.tm_min, out.tm_sec);
}

// 本租户来源身份为 (adapter_name, source_item_id) 的 engram id;没有则空串。来源身份的
// 唯一索引 idx_engrams_source_identity 还含 source_version 与 chunk_index,remember_prepare
// 把这两项固定为 "1" 与 0,这里同样绑定,与索引一致。
std::string engram_with_source_item_id(persistence::Connection& conn,
                                       std::string_view tenant,
                                       std::string_view adapter_name,
                                       std::string_view source_item_id) {
    persistence::Stmt stmt(conn.raw(),
                           "SELECT id FROM engrams WHERE tenant_id=?1 AND adapter_name=?2 "
                           "AND source_item_id=?3 AND source_version='1' AND chunk_index=0 "
                           "ORDER BY rowid ASC LIMIT 1");
    stmt.bind(tenant, adapter_name, source_item_id);
    return stmt.step_row() ? column_string(stmt.raw(), 0) : std::string();
}

// evidence_json 指向 engram_id、holder 相同的语句 id,按 rowid 升序。匹配写法与
// src/bus/statement_writer.cpp 的 find_existing_in_chunk 一致(LIKE 扫 "engram_ref":"<id>")。
std::vector<std::string> statements_for_engram(persistence::Connection& conn,
                                               std::string_view tenant,
                                               std::string_view holder,
                                               std::string_view engram_id) {
    persistence::Stmt stmt(conn.raw(),
                           "SELECT id FROM statements WHERE tenant_id=?1 AND holder_id=?2 "
                           "AND evidence_json LIKE ?3 ORDER BY rowid ASC");
    const std::string like_pat =
        std::string(R"(%"engram_ref":")") + std::string(engram_id) + "\"%";
    stmt.bind(tenant, holder, like_pat);
    std::vector<std::string> ids;
    while (stmt.step_row()) {
        ids.push_back(column_string(stmt.raw(), 0));
    }
    return ids;
}

// holder 为空一律拒绝(裁定 R15),在任何 DB 读写之前。'' 是迁移 0039 给回填不出主人的
// 旧行留的标记:拿 '' 当 holder,read_or_claim_event 第一步就读到无主旧行,把它当成自己
// 的行去重试或放弃,认领规则形同虚设。与 remember_holders 对空 holder 的拒绝同一口径
// (那里逐个回 holder_identity_failure,这里是单个调用,直接抛)。
void require_holder(std::string_view holder, const char* caller) {
    if (holder.empty()) {
        throw std::invalid_argument(std::string(caller) + ": holder must be nonempty");
    }
}

// 本租户里记着这个 key 的 holder,按 holder_id 升序。abandon_event 在 (holder, key) 与
// ('', key) 都找不到时用它写消息:key 在别的 holder 名下,多半是调用方用错了句柄。
std::vector<std::string> holders_owning_key(persistence::Connection& conn,
                                            std::string_view tenant, std::string_view key) {
    persistence::Stmt stmt(conn.raw(),
                           "SELECT holder_id FROM events WHERE tenant_id=?1 AND key=?2 "
                           "ORDER BY holder_id");
    stmt.bind(tenant, key);
    std::vector<std::string> owners;
    while (stmt.step_row()) {
        owners.push_back(column_string(stmt.raw(), 0));
    }
    return owners;
}

// abandon_event 找不到行时的消息。key 在别的 holder 名下就点名它们(裁定 R14):rise 的
// 世界级短连接以 agent "world" 放弃事件,只说 "no such key" 会让人以为 key 拼错了。
std::string missing_key_message(persistence::Connection& conn, std::string_view tenant,
                                std::string_view holder, std::string_view key) {
    const auto owners = holders_owning_key(conn, tenant, key);
    if (owners.empty()) {
        return "abandon_event: no such key (key=" + std::string(key) + ")";
    }
    std::string names;
    for (const auto& owner : owners) {
        if (!names.empty()) {
            names += ", ";
        }
        names += owner;
    }
    return "abandon_event: key '" + std::string(key) + "' is recorded under holder(s) " +
           names + ", not '" + std::string(holder) + "'";
}

// 第 3 段收尾的 UPDATE 只许改 state='pending' 的行,改完必须查受影响行数。零行只有
// 两种成因,都发生在第 2 段(抽取走网络,事务外):abandon_event 把这行改成了终态
// 'abandoned',或 clear_tenant 把这行删了。两种都不能静默收尾——
//   · 静默提交会把 'abandoned' 翻回 'committed',让一个已判终局的 key 复活
//     (设计源 §8:放弃是终局)。rise 的触发路径是会话清理放弃了某实例的 pending
//     事件,而这一轮的写入还在途中。
//   · 静默跳过(行已被删)则让调用方以为收据写成了,而库里压根没有这行。
// 故抛 EventConflict。语句可能已经落库了(第 2 段先于本段),消息里说清这一点。
// 受影响行数照全仓既有写法取 sqlite3_changes(连接):见 pipeline_run_store.cpp 的
// claim、pipeline_ledger.cpp 的 record_attempt、commitment_engine.cpp。
void require_pending_row_updated(persistence::Connection& conn, std::string_view key) {
    if (sqlite3_changes(conn.raw()) != 0) {
        return;
    }
    throw EventConflict(
        "remember_event: key was abandoned or removed while the write was in flight; "
        "statements may already be stored (key=" + std::string(key) + ")");
}

}  // namespace

std::int64_t next_seq(persistence::Connection& conn, std::string_view tenant) {
    persistence::Stmt stmt(conn.raw(),
                           "SELECT COALESCE(MAX(seq) + 1, 0) FROM events WHERE tenant_id=?1");
    stmt.bind(tenant);
    stmt.step_row();
    return sqlite3_column_int64(stmt.raw(), 0);
}

std::vector<PendingEventRow> pending_events(persistence::Connection& conn,
                                            std::string_view tenant) {
    persistence::Stmt stmt(conn.raw(),
                           "SELECT key, text, interlocutor, seq, failure_category, holder_id "
                           "FROM events WHERE tenant_id=?1 AND state='pending' "
                           "ORDER BY seq ASC");
    stmt.bind(tenant);
    std::vector<PendingEventRow> rows;
    while (stmt.step_row()) {
        PendingEventRow row;
        row.key = column_string(stmt.raw(), 0);
        row.text = column_string(stmt.raw(), 1);
        row.interlocutor = column_string(stmt.raw(), 2);
        row.seq = sqlite3_column_int64(stmt.raw(), 3);
        row.failure_category = column_string(stmt.raw(), 4);
        row.holder = column_string(stmt.raw(), 5);
        rows.push_back(std::move(row));
    }
    return rows;
}

RememberOutcome remember_event(persistence::SqliteAdapter& adapter,
                               extractor::LLMAdapter& llm,
                               const RememberParams& base,
                               const RememberPrompts& prompts,
                               std::string_view key,
                               std::string_view text,
                               std::string_view epoch_iso,
                               const extractor::ValidationPolicy& policy) {
    require_holder(base.holder_id, "remember_event");   // 零 DB 读写
    governance::require_write_admission(adapter);   // 门前抛 = 零 DB 写
    auto& conn = adapter.connection();
    const std::string text_sha = crypto::sha256_hex(text);

    // ── 第 1 段:短事务,读收据行(按 holder,必要时认领无主旧行),无行则占位。
    //    事务在本段末尾结束。 ──
    EventRow row;
    bool retrying_pending = false;   // 只有重试已存在的 pending 行才进补齐分支
    {
        persistence::TransactionGuard txn(conn);
        row = read_or_claim_event(conn, base.tenant_id, base.holder_id, key);
        if (row.found) {
            if (row.state == "abandoned") {
                throw EventConflict(
                    "remember_event: key already abandoned; pick a new key (key=" +
                    std::string(key) + ")");
            }
            if (row.text_sha256 != text_sha) {
                throw EventConflict(
                    "remember_event: key already records a different text (key=" +
                    std::string(key) + ")");
            }
            if (row.state == "committed") {
                // 已提交:取行里存的结果,一次 LLM 都不调(第 2、3 段都不走)。
                RememberOutcome done;
                done.holder_id     = base.holder_id;
                done.engram_ref    = row.engram_ref;
                done.statement_ids = parse_statement_ids(row.statement_ids_json);
                done.outcome       = "idempotent";
                done.source_preserved = !row.engram_ref.empty();
                done.structured_claims_persisted = !done.statement_ids.empty();
                txn.commit();
                return done;
            }
            // state == "pending":重试,用行里原来的 seq、原文与说话对象,不另分配序号。
            // interlocutor 决定抽取出的语句落在哪个 scope_parties 下,而 key 的契约是
            // 「标识一次写入」:同 key 换了说话对象是调用方的 bug(重入时说话对象来自当时
            // 的会话上下文,不是从库里读的),静默沿用任何一边都会把它藏起来。与 text 不一致
            // 同一口径:拒绝,并让消息一眼能和文本不一致、已放弃区分开。
            if (row.interlocutor != base.interlocutor) {
                throw EventConflict(
                    "remember_event: key already records a different interlocutor "
                    "(key=" + std::string(key) + ", stored=\"" + row.interlocutor +
                    "\", given=\"" + base.interlocutor + "\")");
            }
            retrying_pending = true;
        } else {
            row.found        = true;
            row.state        = "pending";
            row.text         = std::string(text);
            row.interlocutor = base.interlocutor;
            row.text_sha256  = text_sha;
            row.seq          = next_seq(conn, base.tenant_id);
            persistence::Stmt ins(
                conn.raw(),
                "INSERT INTO events(tenant_id,holder_id,key,text,interlocutor,text_sha256,"
                "seq,state) VALUES(?1,?2,?3,?4,?5,?6,?7,'pending')");
            ins.bind(base.tenant_id, base.holder_id, key, row.text, row.interlocutor, text_sha,
                     row.seq);
            ins.step_done();
        }
        txn.commit();
    }

    // ── 第 2 段:事务外。抽取要走网络,这里绝不持写事务。 ──
    // payload 用行里存的原文(重试时与首次一致),key 作为前缀(设计源 4.3)。
    // interlocutor 同样取行里的值,而不是 base 的:此刻两者必然相等(不等的重试已在第 1
    // 段抛了),显式取行里的,是让「重试沿用首次写入的说话对象」这件事不依赖那个前提,
    // 以后谁改了上面的比对,这里也不会悄悄退回到用本次调用的值。
    // 来源身份按 holder 分开(设计源 §4.3,裁定 R6):engram 按 source_prefix + sha256(payload)
    // 去重,不带 holder 时两个 holder 写同一段话会共用一个 engram,后写者被当成重入、
    // 抽取的 span key 也撞上先写者的,一条语句都写不出来。带上 holder,各得各的 engram。
    const std::string payload_text = "[" + std::string(key) + "] " + row.text;
    RememberParams params = base;
    params.interlocutor = row.interlocutor;
    params.source_prefix = base.source_prefix + "h:" + base.holder_id + ":";
    params.payload.assign(payload_text.begin(), payload_text.end());
    params.created_at_iso8601 = add_seconds_to_iso(epoch_iso, row.seq);

    RememberOutcome result;
    bool completed_from_engram = false;
    // 补齐分支(只在重试已有 pending 行时走):② 落了语句、③ 没写成。按来源身份找回
    // 上次的 engram,挂在它上面、holder 相同的语句就是上次抽取的成果——直接拿来用,不再
    // 抽取一次。不按 content_hash 找:同一段话写进几个 holder 名下就有几个 engram,内容
    // 相同,按内容找会拿到别的 holder 的 engram(设计源 §4.3)。
    // 依次试两个来源身份,第一个挂着本 holder 语句的 engram 就是结果:先试带 holder 的
    // 身份;它没有 engram、或 engram 上没有本 holder 的语句时,再试旧身份
    // base.source_prefix + sha256(payload)[:16](0039 之前的写入不带 holder,在那时崩溃的
    // pending 行的语句挂在旧身份的 engram 上;裁定 R9)。两处都只收本 holder 的语句。
    // 带 holder 的 engram 可能存在却是空的(首次抽取失败时第 ② 段已把它建好),这时也要
    // 试旧身份,否则旧身份上已落库的语句会被重抽一遍。
    // 新 key 不进这里:哪怕同一段文本早先被裸 remember 写过,新 key 也该照自己的前缀
    // 走一遍(不认领别人写的语句)。
    if (retrying_pending) {
        for (const auto& prefix : {params.source_prefix, base.source_prefix}) {
            const std::string engram_id = engram_with_source_item_id(
                conn, base.tenant_id, base.adapter_name, engram_source_item_id(prefix, params.payload));
            if (engram_id.empty()) {
                continue;
            }
            auto ids = statements_for_engram(conn, base.tenant_id, base.holder_id, engram_id);
            if (ids.empty()) {
                continue;
            }
            result.holder_id     = base.holder_id;
            result.engram_ref    = engram_id;
            result.statement_ids = std::move(ids);
            result.outcome       = "idempotent";
            result.source_preserved = true;
            result.structured_claims_persisted = true;
            completed_from_engram = true;
            break;
        }
    }
    if (!completed_from_engram) {
        // remember_all 抛异常时行留 pending(不 catch),异常原样上抛。
        result = remember_all(adapter, llm, params, prompts, policy);
    }

    // ── 第 3 段:短事务,收尾写收据行。 ──
    // 只有「证据真入库了」且「抽取没失败」才算提交。只看 extraction_failed 不够:
    // no_store / rejected 在 remember_commit 之前就早退,source_missing 返回默认构造的
    // RememberOutcome,三者的 extraction_failed 都是 false(这个标记的含义只是「证据已
    // 入库但抽取 LLM 失败」,见 memory_ops.hpp 的注释)。若据此标 committed,这个 key
    // 以后每次调用都回 idempotent + 空语句,记忆静默丢失(设计源 §9 禁止)。stored 的
    // 判据与 memory_ops.cpp 里 converse_commit 算 remember_ok 的那一处一致。
    // 两条 UPDATE 都带 AND state='pending' 并查受影响行数(require_pending_row_updated):
    // 第 2 段在事务外,期间这行可能已被 abandon_event 改成终态、或被 clear_tenant 删掉,
    // 无围栏时本段会把 'abandoned' 覆写回 'committed',复活一个已判终局的 key。
    const bool stored = (result.outcome == "accepted" || result.outcome == "idempotent");
    {
        persistence::TransactionGuard txn(conn);
        if (!stored || result.extraction_failed) {
            // 行留 pending:下次调用照原 seq 重试,不另分配序号。本支不因失败而抛——留着
            // pending 行才让 rise 看得见(pending_events 带 failure_category,门②据此记 T2
            // 失败);只有行已不是 pending(被放弃或删了)才抛。
            const std::string category = result.extraction_failed
                ? result.failure_category : ("not_stored:" + result.outcome);
            // 返回值也带上这个原因:未入库时 remember_all 的 failure_category 是空的
            // (那条路径不填),只记进行里会让调用方拿到一个空原因——又是一种静默。
            // 行里记什么就返回什么,与抽取失败那一支的既有不变式一致。
            if (result.failure_category.empty()) {
                result.failure_category = category;
            }
            persistence::Stmt upd(conn.raw(),
                                  "UPDATE events SET failure_category=?1, "
                                  "engram_ref=COALESCE(NULLIF(?2,''), engram_ref) "
                                  "WHERE tenant_id=?3 AND holder_id=?4 AND key=?5 "
                                  "AND state='pending'");
            upd.bind(category, result.engram_ref, base.tenant_id, base.holder_id, key);
            upd.step_done();
            require_pending_row_updated(conn, key);
        } else {
            persistence::Stmt upd(conn.raw(),
                                  "UPDATE events SET state='committed', engram_ref=?1, "
                                  "statement_ids_json=?2, failure_category='' "
                                  "WHERE tenant_id=?3 AND holder_id=?4 AND key=?5 "
                                  "AND state='pending'");
            upd.bind(result.engram_ref,
                     nlohmann::json(result.statement_ids).dump(),
                     base.tenant_id, base.holder_id, key);
            upd.step_done();
            require_pending_row_updated(conn, key);
        }
        txn.commit();
    }
    return result;
}

void abandon_event(persistence::SqliteAdapter& adapter,
                   std::string_view tenant,
                   std::string_view holder,
                   std::string_view key) {
    require_holder(holder, "abandon_event");        // 零 DB 读写
    governance::require_write_admission(adapter);   // 门前抛 = 零 DB 写
    auto& conn = adapter.connection();

    // 读状态与改状态在同一个事务里:TransactionGuard 的最外层是 BEGIN IMMEDIATE,
    // 读到 pending 之后别的写者插不进来改成 committed。两条抛出路径都没 commit,
    // 守卫析构时回滚,什么都不留(无主旧行的认领也一并撤销)。
    persistence::TransactionGuard txn(conn);
    const EventRow row = read_or_claim_event(conn, tenant, holder, key);
    if (!row.found) {
        // 仍是 out_of_range(绑定译成 KeyError);key 在别的 holder 名下时消息点名它们。
        throw std::out_of_range(missing_key_message(conn, tenant, holder, key));
    }
    if (row.state == "committed") {
        throw EventConflict(
            "abandon_event: key already committed, cannot abandon (key=" +
            std::string(key) + ")");
    }
    if (row.state == "pending") {
        persistence::Stmt upd(conn.raw(),
                              "UPDATE events SET state='abandoned' "
                              "WHERE tenant_id=?1 AND holder_id=?2 AND key=?3 "
                              "AND state='pending'");
        upd.bind(tenant, holder, key);
        upd.step_done();
    }
    // state == "abandoned":已是终态,什么都不改(幂等)。
    //
    // 本函数只改事件行,不碰 statements 与 engrams。注意:pending 行并不保证没有语句。
    // 第 2 段(抽取与入库)跑完、第 3 段写收据前崩溃时,语句与 engram 已经落了库,而行
    // 还是 pending。放弃这样一行,那批语句就留在 library 里,而它的 key 已经死了:
    // 之后 remember_event 一律抛 EventConflict,再也不会走补齐分支把它收成 committed。
    // 清掉这些语句不在本函数的职责里(要另设计一条回收路径),调用方需知道这个后果。
    txn.commit();
}

}  // namespace starling::memoryops
