#pragma once
// Memory ops — remember / tick_all 管线编排(2026-06-11 边界归位第二件,
// 继 Working Set 之后)。
//
// 此前编排在 python/starling/_memory_core.py:幂等键派生
// (source_prefix + sha256(payload)[:16])、入库策略默认值(USER_INPUT/
// WHOLE_RECORD/byte_preserving)、以及「先 engram 后抽取、仅
// accepted/idempotent 才抽取」的顺序规则——按边界判据(换绑定语言需重写)
// 全部属核心语义。Python 仅剩签名归一(datetime→ISO)与绑定转发。
#include <array>
#include <cstdint>
#include <filesystem>
#include <map>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "starling/embedding/embedding_worker.hpp"
#include "starling/extractor/extractor.hpp"
#include "starling/extractor/episodic_extractor.hpp"
#include "starling/extractor/llm_adapter.hpp"
#include "starling/extractor/statement_validator.hpp"
#include "starling/governance/stage_timer.hpp"
#include "starling/persistence/sqlite_adapter.hpp"
#include "starling/prospective/policy_engine.hpp"
#include "starling/retrieval/retrieval_planner.hpp"
#include "starling/retrieval/semantic_retriever.hpp"
#include "starling/runtime_health.hpp"

namespace starling::memoryops {

struct RememberParams {
    std::string tenant_id;
    std::string holder_id;            // agent/self
    std::string interlocutor;         // 可空;非空时抽取带 scope_parties
    std::string adapter_name;         // 来源标识:"facade" / "dashboard"
    std::string source_prefix;        // 幂等键前缀:"mem-" / "dash-"
    std::string created_at_iso8601;   // Python 侧 datetime→ISO(签名归一)
    std::vector<std::uint8_t> payload;
};

struct RememberOutcome {
    std::string holder_id;
    std::string engram_ref;                   // 空 = 未入库(no_store/rejected)
    std::vector<std::string> statement_ids;   // 本次新写入的语句
    std::string outcome;                      // accepted/idempotent/no_store/rejected/
                                              // source_missing(prepare 后证据 engram 已被
                                              // clear_tenant 删除,commit 零写入)
    bool extraction_failed = false;           // 证据已入库但抽取 LLM 失败(Extractor
                                              // 吞失败返回 FAILED 而非抛异常)——供
                                              // converse 区分「抽取失败」与「抽取空」。
    std::string failure_category;
    std::string catalog_version;
    std::map<std::string, std::size_t> accepted_by_predicate;
    std::map<std::string, std::size_t> rejected_by_predicate;
    bool source_preserved = false;
    bool structured_claims_persisted = false;
    std::string failure_detail;
    std::string receipt;                      // native three-channel receipt
};

// 方案2 三相拆分(2026-07-12,remember extraction 出锁):prepare(engram 写)/
// extract_llm(纯 LLM,锁外无事务)/ commit(persist txn 写 + 写后泵)。host 在
// prepare/commit 段持引擎锁、extract 段释放——写事务不再跨 extraction 网络。
// remember() 本身内联三相(单一语义源:分调用与单体逐字段 parity,见
// test_remember_phases.cpp)。C++ 原语是单次 extraction;belief/episodic/gf 的
// 三管线编排在 host MemoryCore(option B:本 slice 只让 belief+gf 出锁)。
struct RememberPrepared {
    std::string engram_ref;         // 空 = 未入库(no_store/rejected)
    std::string outcome;            // accepted/idempotent/no_store/rejected
    bool        should_extract = false;  // accepted||idempotent → 继续 extract+commit
    // #6(review 加固):prepare 时刻快照;commit 以它为权威(而非另传 created_at),
    // 镜像 ConversePrepared.created_at_iso8601——即使 C++/绑定直呼者对 prepare/commit
    // 各传不同 now,落库时戳也不分叉。Python bundle 亦从此读(不再自持 created_iso)。
    std::string created_at_iso8601;
};

struct RememberPrompts {
    std::string belief;
    std::string episodic;
    std::string general_fact;
};

// Opaque host hand-off between the network-only and transaction-only phases.
// The order and failure policy of all three extraction channels live in Core.
struct RememberLlmBundle {
    extractor::ExtractionLlmResult belief;
    extractor::EpisodicLlmResult episodic;
    extractor::ExtractionLlmResult general_fact;
};

// 相位 1(锁内短):require_write_admission fail-fast + append_evidence(engram 写)。
RememberPrepared remember_prepare(persistence::SqliteAdapter& adapter,
                                  const RememberParams& params);

// 相位 2(锁外无事务):构 Extractor → extract_llm(纯 LLM + parse,零 DB)。
extractor::ExtractionLlmResult extract_llm(persistence::SqliteAdapter& adapter,
                                           extractor::LLMAdapter& llm,
                                           std::string_view prompt_template,
                                           const RememberParams& params,
                                           const extractor::ValidationPolicy& policy = {});

// 相位 3(锁内短):require_write_admission(捕中途转 DRAINING)+ Extractor::persist
// (txn 写 statements/attempt/events)+ 写后泵。should_extract=false 时直接回
// prepared 的 outcome/engram_ref(no_store/rejected 短路,不 persist、不泵)。
// prepare 与 commit 之间(锁外抽取段)若租户被 clear_tenant 清空,证据 engram 已不
// 存在:回 outcome="source_missing" 且零写入,绝不写出证据悬空的语句。
RememberOutcome remember_commit(persistence::SqliteAdapter& adapter,
                                extractor::LLMAdapter& llm,
                                const RememberParams& params,
                                const RememberPrepared& prepared,
                                const extractor::ExtractionLlmResult& llm_result,
                                const extractor::ValidationPolicy& policy = {});

// 标准写管线:内容确定性幂等键 → Bus::append_evidence(证据入库)→
// 仅 accepted/idempotent 时 Extractor::run(LLM 抽取)→ 写后泵一次
// (SubscriberPump::run_post_write:投影/信念/再巩固/在线回放/策略)。
// 泵的生产宿主在这里而非 Bus::write——生产语句写经 Extractor→StatementWriter,
// 不经 Bus::write,挂在那里的泵在 remember 路径永不运行(P2.o 实测根因)。
// prompt_template 由调用方注入(prompt 是配置数据,单一源在
// python/starling/extractor/prompts.py)。
RememberOutcome remember(persistence::SqliteAdapter& adapter,
                         extractor::LLMAdapter& llm,
                         std::string_view prompt_template,
                         const RememberParams& params,
                         const extractor::ValidationPolicy& policy = {});

RememberLlmBundle remember_extract_all(
    persistence::SqliteAdapter& adapter, extractor::LLMAdapter& llm,
    const RememberParams& params, const RememberPrompts& prompts,
    const extractor::ValidationPolicy& policy = {});

// Serialize all lock-free extraction channels without exposing their opaque
// C++ representation to language bindings.
std::string remember_bundle_receipt(const RememberLlmBundle& extracted);

// R3.5 native multi-holder orchestration. Each holder owns an independent
// prepare/extract/commit boundary; a holder failure is returned in its result
// and does not prevent later holders from running.
std::vector<RememberOutcome> remember_holders(
        persistence::SqliteAdapter& adapter,
        extractor::LLMAdapter& llm,
        const RememberPrompts& prompts,
        const std::vector<RememberParams>& holders,
        const extractor::ValidationPolicy& policy = {});

RememberOutcome remember_commit_all(
    persistence::SqliteAdapter& adapter, extractor::LLMAdapter& llm,
    const RememberParams& params, const RememberPrepared& prepared,
    const RememberLlmBundle& extracted,
    const extractor::ValidationPolicy& policy = {});

RememberOutcome remember_all(
    persistence::SqliteAdapter& adapter, extractor::LLMAdapter& llm,
    const RememberParams& params, const RememberPrompts& prompts,
    const extractor::ValidationPolicy& policy = {});

// ── converse — chat-with-memory turn (Phase 2c) ──
struct ConverseParams {
    std::string tenant_id;
    std::string holder_id;            // querier (recall) + holder (remember) = agent/self
    std::string interlocutor;         // 可空;沉淀对话时带 scope_parties
    std::string adapter_name;         // 来源标识:"dashboard"
    std::string source_prefix;        // 幂等键前缀:"dash-"
    std::string created_at_iso8601;   // 签名归一(datetime→ISO)
    std::string message;              // 用户本轮输入
    int recall_k = 6;                 // 注入的相关记忆条数上限
    // 形态 S：左脑原文 lane。默认 nullopt = 关闭，对话提示与改动前一致。
    // 只持有 index/embedder 的指针，调用方须在 converse_prepare 期间持有它们。
    std::optional<retrieval::SourceLaneDeps> source_lane;
};

struct ConverseOutcome {
    std::string reply;                       // ok=false 时空(generate 失败)
    bool ok = false;                         // generate 成功 → 有回复
    std::string error;                       // ok=false 时的错误码
    std::string context_pack;                // 注入的记忆(带标签),供轨迹展示
    bool abstained = false;                  // recall 主动拒答
    std::vector<std::string> statement_ids;  // 本轮沉淀的语句
    bool remember_ok = false;                // false → 回复保留但记忆未落库
    std::string remember_error;              // remember 失败原因(可观测)
    // 2b:本轮「回复生成」成本(chat_llm.generate 这一段;remember 的抽取成本
    // 待 extraction_attempt 持久化后单列)。真模型填充,Fake/stub 留 0。
    int gen_prompt_tokens     = 0;
    int gen_completion_tokens = 0;
    int gen_total_tokens      = 0;
    int gen_latency_ms        = 0;
};

// 带记忆的聊天轮(三段式,决策 A):recall(RetrievalPlanner,只读)→ 注入
// context_pack → chat_llm.generate_stream(网络,不持写事务)→ remember 对话
// (extraction_llm,写)。失败语义:generate 失败 → ok=false 且无回复;
// remember 失败 → 回复保留(ok=true)且 remember_ok=false——用户已看到的回复
// 绝不因事后抽取失败而丢。网络 generate 期间不持任何写事务。
//
// on_token(可空):流式回调。非空时,回复在「第二段」生成期间逐 token 经
// on_token 增量回传(WS token stream);空 = 不流式(等价旧 generate 路径)。
// 流式只发生在第二段(不持写事务的网络段),沉淀(第四段写)仍在整段回复流完
// 之后,故 no-write-txn-across-generate 与「回复绝不因 remember 失败而丢」两条
// 不变式都不受流式影响。on_token 不改变返回的 ConverseOutcome(reply 仍是完整
// 文本,成本/落库结果照常),只是把同样的文本在生成时先推一遍。
ConverseOutcome converse(persistence::SqliteAdapter& adapter,
                         extractor::LLMAdapter& chat_llm,
                         extractor::LLMAdapter& extraction_llm,
                         retrieval::SemanticRetriever& semantic,
                         std::string_view extraction_prompt,
                         const ConverseParams& params,
                         const extractor::ValidationPolicy& policy = {},
                         const extractor::TokenSink& on_token = {});

// converse 三相拆分(2026-07-05,锁外生成 slice 第一步):prepare(相位 1+2,只读
// recall + inject)/ commit(相位 3 失败短路+成本填充 + 相位 4 write remember)。
// host 后续会在 prepare/commit 之间持锁外调用 chat_llm.generate_stream——三相
// 拆分让「持写事务的锁」不再跨网络生成段。converse() 本身内联三相,行为字节级
// 不变(单一语义源:分调用与单体调用逐字段 parity,见 test_converse_phases.cpp)。
struct ConversePrepared {
    std::string prompt;         // 拼好待发给 chat_llm 的完整 prompt(围栏+召回+问题)
    std::string context_pack;   // 注入的记忆(带标签),原样透传给 commit 供轨迹展示
    bool abstained = false;     // recall 阶段的主动拒答标记,原样透传
    // prepare 时刻快照;commit 以此为权威时刻(而非 commit 调用时另传的
    // params.created_at_iso8601),保证幂等键/召回种子与 prepare 阶段同刻——
    // 即使调用方在 prepare/commit 两次传入的 now 发生漂移,也不产生分叉。
    std::string created_at_iso8601;
    // 本轮是否要求「先引用、后作答」(召回里出现了 [SOURCE] 行)。为 true 时模型
    // 会先写一个 <evidence> 块；commit 与流式回调都要把它从用户可见回复里剥掉。
    bool grounded = false;
};

// 先引用、后作答（弃答修复 v2，见 docs/eval/2026-10-23-abstention-v2-diagnosis.md）。
//
// 失败机制：讲用户过去某件事 X 的细节时，模型拿 X 的外围原话（X 存在、何时、和谁）
// 拼出「X 的细节」。v1 用独立判定拦截，但判定没有作答时的推理能力，把综合/推理题也
// 判成「未陈述」。这里把核对合进作答本身：先摘出与所问对象相关的原话，再作答；
// 讲用户过去的具体细节时只能说摘出的原话里有的东西。推理、计算、建议不受此约束。
inline constexpr std::string_view kEvidenceOpen  = "<evidence>";
inline constexpr std::string_view kEvidenceClose = "</evidence>";
inline constexpr std::string_view kGroundedAnswerInstruction =
    "\n\nBefore you reply, write an <evidence> block. Inside it, name the specific "
    "thing the user is asking about, then copy the recalled lines that are about that "
    "thing itself — not lines that only mention the same person, place, date or topic. "
    "If no recalled line is about it, write \"none\". Close the block with </evidence>, "
    "then write your reply. The user never sees the block.\n"
    "In the reply, any specific detail you state about the user's own past — what was "
    "said, done, planned, decided or felt — must appear in the lines you copied. If the "
    "user asked for such details and the block has none, say what the recalled memory "
    "does contain about the topic and that the specific details were not recorded; do "
    "not fill them in from general knowledge or from advice given at the time. "
    "Reasoning, calculations, summaries and suggestions built from the copied lines "
    "are fine.\nAssistant:";

// 总结类请求豁免先引用后作答。
//
// BEAM 两个数据集上 summarization 都明显变差（−0.17 / −0.11），机制是证据块挤占回答：
// 总结要覆盖大量记录，摘录本身就很长，回答因此被压短。总结的价值在于覆盖面，
// 不在于逐条引证，所以对这类请求不加证据块（提示与先引用后作答之前逐字节一致）。
//
// 判定只看用户本轮输入，按产品语义列出「要求概括/总览」的说法，做子串匹配（ASCII 部分
// 不区分大小写）。英文表在看任何评测题面之前写成并提交，评测只用来检验它，不用来调它。
//
// 中文（简体与繁体）按同一产品语义补入。没有中文评测数据，这部分**未经任何评测检验**。
// 带「一下」的说法是为了避开「回顾性」「梳理」作名词时的误判。误判的代价是该轮
// 不加证据块，即退回到先引用后作答之前的行为，不会比改动前更差。
inline constexpr auto kSummaryRequestMarkers = std::to_array<std::string_view>({
    "summarize", "summarise", "summary", "summaries", "recap", "overview",
    "give me a rundown", "sum up", "sum it up", "in a nutshell",
    "high-level picture", "big picture",
    "总结", "概括", "概述", "汇总", "小结", "归纳", "总览", "复盘",
    "梳理一下", "回顾一下",
    "總結", "匯總", "總覽", "歸納", "復盤", "回顧一下",
});

// message 是否为总结类请求（不区分大小写）。
bool is_summary_request(std::string_view message);

// 去掉回复开头的 <evidence> 块。
// - 没写块：原样返回（模型没遵从格式，回复本身仍可用）。
// - 块已闭合：返回块之后的作答。
// - 块没闭合：返回 nullopt。这说明生成在摘录阶段就被截断、还没开始作答；原样返回
//   会把半截摘录当成回复泄漏给用户。converse_commit 把它当作无回复的失败轮。
std::optional<std::string> strip_evidence_block(std::string_view reply);

// 流式版的 strip_evidence_block：把开头的证据块挡在下游之外，其后的 delta 原样透传。
// 与 strip_evidence_block 同一判据：开头不是开标签就整段放行；块到结束都没闭合，
// finish() 时丢弃（不泄漏）。
class EvidenceStreamFilter {
public:
    explicit EvidenceStreamFilter(extractor::TokenSink downstream);
    void operator()(std::string_view delta);
    void finish();

private:
    enum class State : std::uint8_t { kUndecided, kInBlock, kPassthrough };
    void pass_through(std::string_view delta);
    bool decide_opening();               // false = 尚未进入块（继续攒或已放行）
    void release_after_close();
    void flush_as_passthrough();
    extractor::TokenSink downstream_;
    std::string buffer_;
    State state_ = State::kUndecided;
    bool skip_leading_ws_ = false;
};

// 相位 1(recall)+ 相位 2(inject):只读,不持写事务。开头即 fail-fast 写门校验——
// 若门已关(DRAINING/UNREADY),在烧生成段网络成本之前就抛 WriteGateRejected,
// 避免「白烧一次 LLM 调用后才发现无法沉淀」。
ConversePrepared converse_prepare(persistence::SqliteAdapter& adapter,
                                  retrieval::SemanticRetriever& semantic,
                                  const ConverseParams& params);

// 相位 3(generate 失败短路 + 成本填充)+ 相位 4(remember 写)。调用方在
// prepare 与 commit 之间自行调用 chat_llm.generate_stream(不持写事务的网络段)。
// commit 不再另查一次 write admission——remember() 内部的 require_write_admission
// 已经覆盖「prepare 时门开、生成期间门关(DRAINING)」的中途翻转:门关时
// remember 抛 WriteGateRejected,被下面的 catch 降级为 remember_ok=false +
// remember_error(回复绝不因此丢,失败语义 A)。
ConverseOutcome converse_commit(persistence::SqliteAdapter& adapter,
                                extractor::LLMAdapter& extraction_llm,
                                std::string_view extraction_prompt,
                                const ConverseParams& params,
                                const ConversePrepared& prepared,
                                const extractor::LLMResponse& gen_resp,
                                const extractor::ValidationPolicy& policy = {});

// 二阶提示注入防御:中和召回文本里的围栏定界符 token,使存储数据无法伪造
// <recalled_memory> 开/闭标签提前闭合围栏(converse 拼 prompt 前调用)。导出以便单测。
std::string neutralize_recall_fence(std::string_view context_pack);

struct TickOutcome {
    int embedded = 0;
    int fired = 0;
    int broken = 0;
    int auto_withdrawn = 0;
    // P2.o 周期维护:回放巩固 + 投影兜底 + 出箱收敛。
    int replay_sampled = 0;   // 本批 idle 回放采样数
    int consolidated = 0;     // volatile→consolidated 晋升数(op_compress)
    int ttl_archived = 0;     // VOLATILE TTL 超期归档数
    int projected = 0;        // 投影兜底批处理事件数
    int dispatched = 0;       // 出箱 pending→delivered 数(in_process 消费者)
    // P3.c1 Phase 3b: per-stage wall-clock timings, in execution order — 8 stages
    // (embed/policy/common_ground/replay_oscillation_guard/replay_ttl_sweep/
    // replay_idle/projection/outbox). metadata_only trace tier.
    std::vector<governance::StageTiming> stage_timings_ms;
    // P3.c LW.2: stages skipped by load-shedding this tick (LOCKED L8/OQ-LW.6).
    // Same string labels as stage_timings_ms; a stage is in exactly one of the
    // two. Empty = nothing shed (READY). Excluded from the host idle-heartbeat
    // predicate (LW.3).
    std::vector<std::string> stages_skipped;
};

// 周期维护(P2.o 扩展):嵌入一批待向量语句 → 承诺触发器 tick → grounding
// 滞后冲账(P2.j)→ 回放维护(振荡防护 → volatile TTL sweep → idle 批)→
// 投影兜底批 → 出箱派发(Accept-all "in_process" 消费者:嵌入式单进程无外部
// 消费者,五个进程内消费者均按 consumer_checkpoints 推进且不过滤
// dispatch_status,故标记 delivered 安全;delivered=进程内交付完成)。
// worker/policy 由调用方持有注入(embedder 可热换,见 DashboardEngine)。
// health (P3.c LW.2): gates each stage through governance::should_run_stage;
// defaults to READY (behavior-preserving — existing 4-arg callers unchanged).
// skip_embed(裁定 R54):调用方判定嵌入端点此刻不能用(例如继承来的 provider 地址已改,
// endpoint_changed)时为 true:只跳过 Embed 阶段,记进 stages_skipped("embed"),其余阶段
// 照常按 health 门控运行(承诺触发、grounding、回放、投影、出箱不因嵌入不可用而停)。
TickOutcome tick_all(persistence::SqliteAdapter& adapter,
                     embedding::EmbeddingWorker& worker,
                     prospective::PolicyEngine& policy,
                     std::string_view now_iso,
                     RuntimeHealth health = RuntimeHealth::READY,
                     bool skip_embed = false);

// 只跑嵌入阶段:反复调 worker.tick_one_batch 直到积压排空,累加各批的成功与失败数。
// 与 tick_all 的区别:不碰承诺触发、共同基础、回放、投影、出箱,也不看健康门控
// (调用方要的是"让新语句可被召回",不是周期维护)。
// 停止条件:某一批嵌入 0 条即停。只剩失败行时,第一批全部失败后就返回,同一次调用
// 不会反复重试它们(重试留给下次调用,受 WorkerConfig::max_retry 限制);一批里有成功
// 有失败则继续下一批。嵌入失败只计数不抛(tick_one_batch 自己记失败行与重试次数);
// SQLite 层错误照常向上抛。只统计语句分支的 embedded/failed,来源 lane 的计数不计入,
// 也不影响停止判断。
struct EmbedPendingOutcome {
    int embedded = 0;
    int failed = 0;
};

EmbedPendingOutcome embed_pending(persistence::SqliteAdapter& adapter,
                                  embedding::EmbeddingWorker& worker,
                                  std::string_view now_iso);

// P3.b2:逻辑删除一批 statements(→forgotten),返回实际转换计数。
int forget(persistence::SqliteAdapter& adapter, std::string_view tenant,
           const std::vector<std::string>& ids, std::string_view now_iso);

// 清空单个租户的全部记忆(dashboard 设置页「危险区」)。与 forget 不同,这是物理删除:
// 单事务删掉该租户所有租户作用域行(engram/语句/事件/向量/投影/边/承诺/共同基础/
// 再巩固窗/认知体关系/梦境日志等),保留 kind='self' 的认知体(二阶 ToM 依赖 self)。
// 全局表(迁移记录、出箱序号、订阅者游标)不动——它们不属于任何租户,且游标只会前进,
// 新事件序号总比游标大,清空后各订阅者照常工作。其他租户零影响。
// 梦境日志(replay_ledger.tenant_id,迁移 0036,可空):删本租户的行;跨租户批次与旧行
// 的 tenant_id 为 NULL,仅在库里已无其他租户数据时才一并删除,否则保留。
// 门前抛 WriteGateRejected(零写);tenant_id 为空抛 std::invalid_argument。
// index:向量不在 SQLite 里的后端(zvec HNSW collection)传入,事务提交后逐条
// best-effort remove;SQLite blob 后端传 nullptr(向量就是 statement_vectors 行,
// 已随事务删除)。remove 失败只计数不抛——SQLite 已清空,残留向量会被检索的 SQL
// 精过滤(JOIN statements)滤掉,不可见,只占空间。
struct ClearTenantOutcome {
    std::map<std::string, std::int64_t> deleted;   // 表名 → 删除行数(每张表都有键,含 0)
    std::int64_t total = 0;
    std::int64_t vectors_removed = 0;              // index 非空时成功 remove 的条数
    std::int64_t vector_remove_failed = 0;
};
ClearTenantOutcome clear_tenant(persistence::SqliteAdapter& adapter,
                                std::string_view tenant_id,
                                vector::VectorIndex* index = nullptr);

// 片 6 干预集:人工审批 review_requested → approved(守卫幂等、tenant-scoped),返回转换计数(0/1)。
// reject 不在此 —— reject = forget(→forgotten 终态)。
int approve_review(persistence::SqliteAdapter& adapter, std::string_view tenant,
                   std::string_view stmt_id, std::string_view now_iso);

// P3.a3 再巩固显式触发:发 reconsolidate.requested 事件,engine 异步开窗。返回 outbox
// event_id。(边界归位:原写逻辑在 bind_09 lambda,本 slice 提取入核心以受门管辖。)
std::string request_reconsolidation(persistence::SqliteAdapter& adapter,
                                    std::string_view tenant_id,
                                    std::string_view stmt_id,
                                    std::string_view request_id,
                                    std::string_view now_iso);

// 给整个 library 文件留一份自洽的单文件副本(rise 每轮部署前的起点副本,回滚时覆盖回去)。
//
// 副本由 VACUUM INTO 生成:rollback 日志模式、没有 -wal/-shm 旁路文件,拷走这一个文件
// 就是完整的库。流程:
//   1. dest 上已有任何东西(文件、目录、悬空符号链接)→ 抛 std::filesystem::filesystem_error
//      (errc::file_exists),dest 与 <dest>.partial 都不碰。
//   2. 清掉上次崩溃留下的 <dest>.partial,再 VACUUM INTO 写到 <dest>.partial。
//   3. 只读打开 <dest>.partial 跑 PRAGMA integrity_check;不是 "ok" 就删掉 .partial 并抛
//      std::runtime_error("backup integrity_check failed")。
//   4. 再确认一次 dest 仍不存在(第 2 步可能很久),然后 fs::rename 到 dest。
// 任何一步失败都不留 <dest>.partial,也不覆盖已有的 dest。dest 为空路径抛
// std::invalid_argument。dest 的上级目录须已存在,不会代建;不存在时 VACUUM INTO 失败,
// 抛 persistence::SqliteError。
//
// 用 adapter 自己的连接跑 VACUUM INTO(内存库也能备份),所以调用时这条连接上不能有未
// 提交的事务:VACUUM 在事务内会失败,抛 persistence::SqliteError。副本包含库里所有租户
// 的数据,不只 adapter 所属的那一个。
void backup_to(persistence::SqliteAdapter& adapter, const std::filesystem::path& dest);

}  // namespace starling::memoryops
