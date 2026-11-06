"""Shared embedded-runtime command core for `starling.Memory` and
`starling.dashboard.DashboardEngine`.

Both surfaces previously carried near-identical copies of remember / recall /
tick / working-set assembly (~115 duplicated lines) and had already drifted
twice: the engine's working set lost the affect section, and the facade used a
non-deterministic idempotency key. This module is the single implementation;
the two wrappers keep only their construction policy (stub vs config-driven
adapters) and output shapes (dataclasses/ContextBlock vs JSON dicts).

NOT thread-safe by itself — DashboardEngine serializes calls with its own lock;
the embedded facade is single-user by contract.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from starling import _core
from starling.extractor.config import ExtractionConfig


def _build_policy(extraction):
    """Forward to the binding mapping; C++ owns policy validation."""
    return extraction.to_native_policy()


def _make_vector_index(backend: str, dim: int, store_path):
    """P3.b1 phase 5 Task 5.3:按 vector_backend 创建向量后端句柄(回滚留好)。

    sqlite(默认):SqliteBlobVectorIndex(暴力 cosine + SQL scope,任意维)。
    zvec:ZvecVectorIndex(HNSW,需 STARLING_VECTOR_ZVEC 构建的 _core);collection
         维度固定,需 store_path + dim。向量数据持久化在 store_path,重建句柄即 reopen。
    """
    if backend == "zvec":
        if not hasattr(_core, "ZvecVectorIndex"):
            raise RuntimeError(
                "vector_backend='zvec' requires a STARLING_VECTOR_ZVEC build of _core")
        path = store_path or os.path.expanduser("~/.starling/vectors")
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        return _core.ZvecVectorIndex(path, int(dim))
    if backend == "sqlite":
        return _core.SqliteBlobVectorIndex()
    raise ValueError(
        f"unknown vector_backend={backend!r}; expected 'sqlite' or 'zvec'")


class LLMNotConfigured(RuntimeError):
    """remember() requires an llm adapter (make_stub_llm / make_openai_llm /
    make_anthropic_llm, or the dashboard provider factory)."""


class RecallDegradedError(RuntimeError):
    """recall(strict=True) 时查询词嵌入失败:语义召回退化成只剩词法结果,
    调用方不能把它当成"没有相关记忆"。信息里不带端点与密钥。"""


def parse_now(now: "Optional[str | datetime]") -> datetime:
    """Coerce an optional ISO-8601 string (or an already-resolved datetime)
    into a tz-aware datetime.

    `for_user_input` expects a datetime (it formats via `_iso`). Default to
    the current UTC time when no override is given. The datetime pass-through
    lets a caller resolve `now` ONCE and hand the same instant to two
    downstream calls (e.g. DashboardEngine._converse_phased resolving `now`
    once for both converse_prepare and converse_commit, whose idempotency
    key/recall hash must share the same created_at).
    """
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, datetime):
        return now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    s = now.replace("Z", "+00:00") if now.endswith("Z") else now
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# 左脑 lane 开放的 intent（形态 S spec §2）。converse 固定走 FACT_LOOKUP。
_SOURCE_LANE_INTENTS = frozenset({"FACT_LOOKUP", "HISTORY", "PREFERENCE", "ABSTAIN_CHECK"})


class MemoryCore:
    """Command core over ONE runtime/WAL writer.

    `adapter_name`/`source_prefix` parametrize the engram source identity so
    the facade ("facade"/"mem-") and the dashboard ("dashboard"/"dash-") keep
    their historical idempotency namespaces.
    """

    def __init__(self, rt, *, agent: str, tenant_id: str, llm=None,
                 adapter_name: str, source_prefix: str,
                 vector_backend: str = "sqlite", vector_store_path=None,
                 extraction: "ExtractionConfig | None" = None):
        self.rt = rt
        self.agent = agent
        self.tenant = tenant_id
        self.llm = llm           # extraction adapter (remember); also chat fallback
        self.chat_llm = None     # chat adapter (converse); None → falls back to self.llm
        self.consolidation_llm = None  # #38-C: consolidation role; None → deterministic gist (no LLM judge)
        self.gist_thresholds = {}      # #38-C v2: {min_holders,min_replay_count,min_confidence} overrides; {} → C++ defaults
        self.adapter_name = adapter_name
        self.source_prefix = source_prefix
        self._extraction = extraction or ExtractionConfig()
        # Shared connection handle for the extractor (keep-alive in binding).
        self.conn = rt.adapter.connection()
        # 向量后端在 set_embedder 时按 embedder 维度创建(zvec collection 需固定 dim)。
        self._vector_backend = vector_backend
        self._vector_store_path = vector_store_path
        self.idx = None
        self.policy = _core.PolicyEngine(rt.adapter)
        # Manual commitment transitions (片6): conn-free engine over the same single
        # adapter; fulfill/withdraw are ACTIVE-guarded in C++. Held under the engine
        # lock by the dashboard caller (single writer).
        self.commitment_engine = _core.CommitmentEngine(rt.adapter)
        self.emb = None
        self.semantic = None
        self.completor = None
        self.worker = None
        # 形态 S 左脑 lane。enable_source_lane() 开启；None = 关闭（读路径不检索原文、
        # worker 不嵌入原文）。只在当前 embedder 与标定一致时才真正生效，见
        # _rebuild_source_lane。
        self._source_lane_cfg: Optional[dict] = None
        self.source_idx = None
        self.source_lane = None              # _core.SourceLaneDeps | None
        self.source_lane_inactive_reason = ""

    def set_embedder(self, emb) -> None:
        """(Re)build the embedder-dependent components as one consistent set,
        so the embedder that WRITES vectors (EmbeddingWorker) and the one that
        READS them (SemanticRetriever) never diverge."""
        self.emb = emb
        # 按后端 + embedder 维度创建向量句柄(数据持久化,重建句柄即 reopen)。
        self.idx = _make_vector_index(self._vector_backend, emb.dim(),
                                      self._vector_store_path)
        self.semantic = _core.SemanticRetriever(self.rt.adapter, emb, self.idx)
        self.completor = _core.PatternCompletor(self.rt.adapter, self.semantic)
        self._rebuild_source_lane()

    def enable_source_lane(self, *, tau_source: float, model: str, dim: int,
                           k_source: int = 30, chunk_byte_budget: int = 32000,
                           k_max: int = 200) -> None:
        """开启左脑 lane（形态 S）。``tau_source`` 只对标定时的嵌入模型有效，
        因此同时记下 ``model``/``dim``：当前 embedder 与之不符时 lane 不生效
        （读写都不碰原文），原因见 ``source_lane_inactive_reason``。"""
        if not (tau_source > 0):
            raise ValueError("source lane requires a calibrated tau_source (> 0)")
        self._source_lane_cfg = {"tau_source": float(tau_source), "model": model,
                                 "dim": int(dim), "k_source": int(k_source),
                                 "chunk_byte_budget": int(chunk_byte_budget),
                                 "k_max": int(k_max)}
        if self.emb is not None:
            self._rebuild_source_lane()

    def _rebuild_source_lane(self) -> None:
        """按当前 embedder 重建 worker 与 lane 依赖。

        worker 只在 lane 生效时带 source 分支：嵌入原文要花真金白银的嵌入调用，
        lane 关闭时不该替一条不读的通道付费。"""
        cfg = self._source_lane_cfg
        emb = self.emb
        self.source_lane = None
        self.source_idx = None
        reason = ""
        if cfg is None:
            reason = "disabled"
        elif emb.model() != cfg["model"] or emb.dim() != cfg["dim"]:
            # 阈值是相似度分界线，换模型后分数分布不同，旧阈值没有意义。
            reason = (f"embedder {emb.model()}/{emb.dim()} != calibrated "
                      f"{cfg['model']}/{cfg['dim']}")
        else:
            self.source_idx = _core.SqliteSourceVectorIndex()
            self.source_lane = _core.SourceLaneDeps(
                self.source_idx, emb, cfg["tau_source"], cfg["k_max"],
                cfg["chunk_byte_budget"], cfg["k_source"])
        self.source_lane_inactive_reason = reason
        if self.source_idx is not None:
            self.worker = _core.EmbeddingWorker(self.rt.adapter, emb, self.idx,
                                                self.source_idx)
        else:
            self.worker = _core.EmbeddingWorker(self.rt.adapter, emb, self.idx)

    def remember(self, text: str, *, holder=None, interlocutor=None, now=None) -> dict:
        """Core-owned belief/general-fact/episodic remember pipeline."""
        if self.llm is None:   # #1:fail-fast(prepare 会写 engram,故在写前查;facade 路径的兜底)
            raise LLMNotConfigured(
                "remember requires an llm adapter "
                "(make_stub_llm / make_openai_llm / make_anthropic_llm)")
        created_iso = parse_now(now).astimezone(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        holder_id = holder or self.agent
        return _core.memory_remember_all(
            self.rt.adapter, self.llm,
            self._extraction.belief_prompt,
            self._extraction.episodic_prompt,
            self._extraction.general_fact_prompt,
            tenant_id=self.tenant, holder_id=holder_id,
            interlocutor=interlocutor or "", adapter_name=self.adapter_name,
            source_prefix=self.source_prefix, created_at_iso8601=created_iso,
            payload=text.encode("utf-8"), policy=_build_policy(self._extraction))

    def remember_prepare(self, text: str, *, holder=None, interlocutor=None,
                         now=None) -> dict:
        """三相之一(锁内短):engram 写(belief+gf 共用同一 idempotent engram)。
        #1:不查 self.llm——prepare 不用 llm;llm 缺失的 fail-fast 由调用方在写前做
        (facade monolith remember() 已查;engine.remember 经 _resolve_extraction 查)。
        #6:created_iso 不入 bundle——commit/episodic 从 prepared.created_at_iso8601 读。"""
        created_iso = parse_now(now).astimezone(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        holder_id = holder or self.agent
        prepared = _core.memory_remember_prepare(
            self.rt.adapter, tenant_id=self.tenant, holder_id=holder_id,
            interlocutor=interlocutor or "", adapter_name=self.adapter_name,
            source_prefix=self.source_prefix, created_at_iso8601=created_iso,
            payload=text.encode("utf-8"))
        return {"prepared": prepared, "holder_id": holder_id,
                "interlocutor": interlocutor or "", "text": text}

    def remember_extract(self, bundle: dict, llm=None):
        """三相之二：Core 统一执行三条 LLM 抽取，Python 仅转发配置。"""
        prepared = bundle["prepared"]
        if not prepared.should_extract:
            return None
        extraction_llm = llm or self.llm
        if extraction_llm is None:   # #1:extract 真正用 llm 处兜底(直呼 extract 时)
            raise LLMNotConfigured("remember requires an llm adapter")
        return _core.memory_remember_extract_all(
            self.rt.adapter, extraction_llm,
            self._extraction.belief_prompt,
            self._extraction.episodic_prompt,
            self._extraction.general_fact_prompt,
            holder_id=bundle["holder_id"], payload=bundle["text"].encode("utf-8"),
            policy=_build_policy(self._extraction))

    def remember_commit(self, bundle: dict, extracted, llm=None) -> dict:
        """三相之三：Core 在一个外层事务内提交三条管线。"""
        prepared = bundle["prepared"]
        if not prepared.should_extract:
            return {"engram_ref": prepared.engram_ref, "statement_ids": [],
                    "outcome": prepared.outcome, "extraction_failed": False}
        extraction_llm = llm or self.llm
        holder_id = bundle["holder_id"]
        interlocutor = bundle["interlocutor"]
        return _core.memory_remember_commit_all(
            self.rt.adapter, extraction_llm, tenant_id=self.tenant,
            holder_id=holder_id, interlocutor=interlocutor,
            prepared=prepared, extracted=extracted,
            policy=_build_policy(self._extraction))

    def recall(self, query: str, *, perspective: str = "first_person",
               k: int = 10, mode: str = "semantic", holder: str | None = None,
               strict: bool = False, include_volatile: bool = False) -> list:
        """mode="semantic" — vector cosine via SemanticRetriever.vector_recall;
        mode="completion" — associative spreading via PatternCompletor.

        holder overrides the recall holder dimension (default self.agent). A
        caller that wrote statements under a non-default holder MUST recall with
        the same holder, else the candidate SQL's `s.holder_id = ?` predicate
        excludes them (P3.b2: OpenClaw plugin holder must round-trip).

        strict=True: 语义模式下查询词嵌入失败(SemanticResult.degraded)时抛
        RecallDegradedError,而不是静默返回只剩词法的结果。completion 模式没有
        degraded 信号,strict=True 直接抛 ValueError。

        include_volatile=True:语义召回额外接受 consolidation_state='volatile'
        的语句(只 embed_pending、未经 tick 回放),可能带来近重复。只放宽巩固
        状态,review 守卫与 holder/视角谓词不变;放宽在 C++ 里做。completion
        模式不支持,直接抛 ValueError。"""
        if strict and mode == "completion":
            raise ValueError(
                "strict=True is not supported with mode='completion': "
                "pattern completion has no degraded signal")
        if include_volatile and mode == "completion":
            raise ValueError(
                "include_volatile=True is not supported with mode='completion'")
        holder_id = holder or self.agent
        # holder_perspective is stored lowercase (extractor normalizes the enum);
        # normalize the query perspective so a caller passing "FIRST_PERSON" still
        # matches the stored "first_person" instead of silently returning empty.
        perspective = (perspective or "").lower()
        if mode == "completion":
            res = self.completor.complete(_core.PatternCompletionParams(
                tenant_id=self.tenant, holder_id=holder_id,
                holder_perspective=perspective, cue_text=query, result_k=k))
            return self._with_content_ids(
                [{"row": s.row, "score": s.activation} for s in res.rows])
        res = self.semantic.vector_recall(_core.SemanticRetrieverParams(
            tenant_id=self.tenant, holder_id=holder_id,
            holder_perspective=perspective, query_text=query, k=k,
            include_volatile=include_volatile))
        if strict and res.degraded:
            raise RecallDegradedError("query embedding failed")
        hits = [{"row": s.row, "score": s.score} for s in res.rows]
        return self._with_content_ids(hits)

    def _with_content_ids(self, hits: list) -> list:
        """给每个命中补 `content_id`(行的内容 id,库与库之间可比)。

        一次 `memory_content_ids` 批量查完,不按命中逐条查:k=10 的召回会变成 10 次
        SQL,而内容 id 的规范化不便宜(每行要解析几个 JSON 列)。"""
        if not hits:
            return hits
        mapping = _core.memory_content_ids(
            self.conn, self.tenant, [h["row"].id for h in hits])
        for hit in hits:
            hit["content_id"] = mapping[hit["row"].id]
        return hits

    def digest(self) -> str:
        """本租户 library 的逻辑摘要(64 位小写十六进制)。

        内容相同的两个库给同一个值,内容一变就变:行 id 是 UUID、库与库之间必然
        不同,所以摘要把身份换成内容再哈希。规范化与哈希都在 C++
        `memoryops::digest`,这里只是绑定转发。"""
        return _core.memory_digest(self.conn, self.tenant)

    def backup_to(self, path) -> None:
        """给整个 library 文件留一份自洽的单文件副本(`VACUUM INTO`)。

        目标已存在抛 `FileExistsError`,不碰它;其余失败(上级目录不存在、
        `integrity_check` 不过)抛 `RuntimeError`,都不留半成品。VACUUM、完整性检查与改名都在 C++
        `memoryops::backup_to`,这里只把 `str | os.PathLike` 归一成 `str`。"""
        _core.memory_backup_to(self.rt.adapter, os.fspath(path))

    def content_ids(self, statement_ids: list[str]) -> dict:
        """语句 UUID → 内容 id;本租户没有的 id 映射成 "missing"(与摘要里的约定
        一致)。一次查完,查询在 C++ `memoryops::content_ids`。"""
        return _core.memory_content_ids(self.conn, self.tenant,
                                        [str(i) for i in statement_ids])

    def plan_query(self, text: str = "", *, intent: str = "FACT_LOOKUP",
                   perspective: Optional[str] = None, target: Optional[str] = None,
                   subject: Optional[str] = None, predicate: Optional[str] = None,
                   k: int = 10, now=None):
        """P3.a1 检索规划:一行转发 _core.RetrievalPlanner(7 步管线居 C++,
        语义路径网络期间释放 GIL)。"""
        q = _core.PlannerQuery()
        q.tenant_id = self.tenant
        q.querier = self.agent
        q.perspective = perspective or ""
        q.intent = getattr(_core.QueryIntent, intent)
        q.text = text
        q.subject_id = subject or ""
        q.predicate = predicate or ""
        q.target = target or ""
        q.as_of_iso8601 = parse_now(now).astimezone(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        q.k = k
        q.trace_id = str(uuid.uuid4())
        q.query_id = str(uuid.uuid4())
        # 左脑只对「我自己经历过什么」类 intent 开（形态 S spec §2，照搬父 spec）。
        # 他人信念、元信念、共同基础、承诺、规范是右脑的社会推理，不该被原话直接
        # 顶替。planner 本身不按 intent 门控，所以在这里挡。
        if intent in _SOURCE_LANE_INTENTS:
            q.source_lane = self.source_lane
        planner = _core.RetrievalPlanner(self.rt.adapter, self.semantic)
        return planner.run(q)

    def _converse_turn_args(self, holder, now):
        """converse 三相共享的参数规整(与单体 converse 相同的缺省逻辑)。"""
        if self.llm is None:
            raise LLMNotConfigured(
                "converse requires an extraction llm adapter "
                "(configure a provider + bind the extraction role)")
        holder_id = holder or self.agent
        created_iso = parse_now(now).astimezone(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        return holder_id, created_iso

    def converse_prepare(self, message: str, *, holder=None, interlocutor=None,
                         k: int = 6, now=None):
        """三相之一(锁内):写门 fail-fast + recall + 围栏 + prompt。"""
        holder_id, created_iso = self._converse_turn_args(holder, now)
        return _core.memory_converse_prepare(
            self.rt.adapter, self.semantic,
            tenant_id=self.tenant, holder_id=holder_id,
            interlocutor=interlocutor or "",
            adapter_name=self.adapter_name, source_prefix=self.source_prefix,
            created_at_iso8601=created_iso, message=message, recall_k=k,
            source_lane=self.source_lane)

    def generate_stream(self, chat_llm, prompt: str, on_token=None, *, grounded: bool = False):
        """三相之二(锁外):纯网络,绑定释放 GIL、逐 delta 回调。

        grounded 取 prepare 返回的 ``prepared.grounded``:为 True 时回复开头的
        <evidence> 块不进 on_token(commit 会从最终回复里剥掉)。"""
        return _core.memory_generate_stream(chat_llm, prompt, on_token, grounded)

    def converse_commit(self, message: str, prepared, gen_resp, *, holder=None,
                        interlocutor=None, k: int = 6, now=None) -> dict:
        """三相之三(锁内):失败语义 A 收口 + remember。now 必须与 prepare
        同值(幂等键/召回哈希共用 created_at)——调用方负责传同一时刻。"""
        holder_id, created_iso = self._converse_turn_args(holder, now)
        return _core.memory_converse_commit(
            self.rt.adapter, self.llm, self._extraction.belief_prompt,
            tenant_id=self.tenant, holder_id=holder_id,
            interlocutor=interlocutor or "",
            adapter_name=self.adapter_name, source_prefix=self.source_prefix,
            created_at_iso8601=created_iso, message=message, recall_k=k,
            prepared=prepared, gen_resp=gen_resp,
            policy=_build_policy(self._extraction))

    def converse(self, message: str, *, holder=None, interlocutor=None,
                 k: int = 6, now=None, on_token=None) -> dict:
        """Phase 2c chat-with-memory turn — thin forward to C++
        `memory_converse` (3-phase orchestration居 C++,与 remember/plan_query
        同层;网络段释放 GIL)。chat 适配器缺省回退到抽取适配器(self.llm)。

        返回 {reply, ok, error, context_pack, abstained, statement_ids,
        remember_ok, remember_error}。失败语义 A:generate 失败 → ok=False 无
        回复;remember 失败 → 回复保留、remember_ok=False(可观测)。

        on_token(可空):流式回调 fn(delta:str)。非空时回复在「生成段」逐 token
        增量回传(C++ 在 GIL 释放下运行,binding 每个 delta 重新 acquire GIL 再
        回调);空 = 非流式(等价旧行为)。回调只是把同样的文本先推一遍,不改返回值。"""
        holder_id, created_iso = self._converse_turn_args(holder, now)
        chat = self.chat_llm or self.llm
        return _core.memory_converse(
            self.rt.adapter, chat, self.llm, self.semantic,
            self._extraction.belief_prompt,
            tenant_id=self.tenant, holder_id=holder_id,
            interlocutor=interlocutor or "",
            adapter_name=self.adapter_name, source_prefix=self.source_prefix,
            created_at_iso8601=created_iso, message=message, recall_k=k,
            policy=_build_policy(self._extraction), on_token=on_token,
            source_lane=self.source_lane)

    def tick(self, now: str, *, skip_embed: bool = False) -> dict:
        """Advance background workers: embed pending + fire due commitments +
        flush grounding 滞后事件 (P2.j)。三连组合在 C++ `memoryops::tick_all`
        (嵌入网络期间释放 GIL);这里只剩绑定转发。
        P3.c LW.3: pass live supervisor health so C++ can gate soft stages.
        skip_embed(裁定 R54):只跳过嵌入阶段(调用方判定嵌入端点此刻不能用),其余照常。"""
        return _core.memory_tick_all(
            self.rt.adapter, self.worker, self.policy, now, self.rt.health(),
            skip_embed=skip_embed)

    def embed_pending(self, now: str) -> dict:
        """Run ONLY the embedding stage until the backlog is drained (no
        commitments / replay / projection / outbox, no health gating). Returns
        {"embedded": int, "failed": int}; embedding failures are counted, not
        raised. The loop lives in C++ `memoryops::embed_pending` (GIL released
        during the embedder calls); this is a binding forward."""
        return _core.memory_embed_pending(self.rt.adapter, self.worker, now)

    def build_working_set(self, interlocutor, *, goal=None, token_budget: int = 2000,
                          holder: str | None = None):
        """Assemble the prompt-ready ContextBlock (P2.e): persona /
        common_ground / relevant_memories / pending_commitments / affect under
        an approximate token budget. A `fired` commitment surfaces as a ⚠ DUE
        reminder (B3 closure).

        核心逻辑(五源汇集 + 预算分配 + 截断 + 渲染)在 C++
        `starling::hippocampus::build_working_set`(2026-06-11 边界归位);
        这里只是绑定转发——Python 层不持有 Working Set 语义。
        """
        return _core.build_working_set(
            self.rt.adapter, self.semantic,
            tenant_id=self.tenant, agent_id=holder or self.agent,
            interlocutor=interlocutor, goal=goal or "",
            token_budget=token_budget)

    def latest_event_location(self, theme: str) -> str:
        """sub-project A phase 6:主题 `theme` 最高 seq 的 OCCURRED 事件 location
        (地面真值「当前所在」),无事件返回 ""。薄转发到核心层
        `EpisodicEventStore::latest_event_location`(纯本地 SQLite 读)。"""
        return _core.latest_event_location(self.conn, self.tenant, theme)

    def next_seq(self) -> int:
        """本租户 events 表的下一个序号(空库为 0,含 abandoned 行,只增不复用)。
        查询在 C++ `memoryops::next_seq`,这里只是绑定转发。"""
        return _core.memory_next_seq(self.conn, self.tenant)

    def pending_events(self) -> list[dict]:
        """本租户 state='pending' 的 events 行,按 seq 升序。查询在 C++
        `memoryops::pending_events`,这里原样返回 dict 列表,由门面包成 PendingEvent。"""
        return _core.memory_pending_events(self.conn, self.tenant)

    def remember_event(self, key: str, text: str, *, interlocutor=None,
                       epoch: str = "2026-01-01T00:00:00Z") -> dict:
        """按调用方给的稳定 key 记一次写入,崩溃安全、可重放。三段状态机在 C++
        `memoryops::remember_event`,这里只做 fail-fast 与绑定转发。

        `epoch` 是时钟原点:engram 的 created_at 是 epoch + 事件行 seq 秒。时刻必须
        由 seq 推出(seq 在第 1 段才分配,调用方事先拿不到),所以这里不收 `now`。"""
        if self.llm is None:   # 与 remember 一致:第 1 段会写 pending 行,故在写前查
            raise LLMNotConfigured(
                "remember_event requires an llm adapter "
                "(make_stub_llm / make_openai_llm / make_anthropic_llm)")
        return _core.memory_remember_event(
            self.rt.adapter, self.llm,
            self._extraction.belief_prompt,
            self._extraction.episodic_prompt,
            self._extraction.general_fact_prompt,
            tenant_id=self.tenant, holder_id=self.agent,
            interlocutor=interlocutor or "", adapter_name=self.adapter_name,
            source_prefix=self.source_prefix, key=key, text=text,
            epoch_iso=epoch, policy=_build_policy(self._extraction))

    def abandon_event(self, key: str) -> None:
        """把 pending 事件改成终态 abandoned。状态机在 C++
        `memoryops::abandon_event`(committed 抛 EventConflict,未知 key 抛 KeyError,
        已 abandoned 幂等),这里只是绑定转发。holder 是本门面的 agent。"""
        _core.memory_abandon_event(self.rt.adapter, self.tenant, self.agent, key)

    def forget(self, ids: list[str], *, now=None) -> dict:
        """逻辑删除(→forgotten):核心 `memoryops::forget`,这里只签名归一。
        forgotten 立即移出检索;向量/投影清理由 tick 跟进。"""
        now_iso = parse_now(now).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        n = _core.memory_forget(self.rt.adapter, tenant=self.tenant,
                                ids=list(ids), now_iso=now_iso)
        return {"forgotten": n}

    def clear_tenant(self) -> dict:
        """物理清空本租户全部记忆(保留 kind='self' 认知体):核心
        `memoryops::clear_tenant`,单事务。只有 zvec 后端的向量存在 SQLite 之外,
        才把句柄交给核心在提交后逐条移除;sqlite 后端的向量就是表行,已随事务删除。"""
        index = self.idx if self._vector_backend == "zvec" else None
        return _core.memory_clear_tenant(self.rt.adapter, tenant=self.tenant, index=index)

    # ── 片 6 干预集:触发式安全写(全经核心,这里只薄转发) ─────────────────
    def approve_review(self, stmt_id: str, *, now=None) -> dict:
        """人工审批 review_requested → approved(守卫幂等)。核心 `memoryops::approve_review`。
        reject 不在此 —— reject = forget(→forgotten 终态)。"""
        now_iso = parse_now(now).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        n = _core.memory_approve_review(self.rt.adapter, tenant=self.tenant,
                                        stmt_id=stmt_id, now_iso=now_iso)
        return {"approved": n}

    def run_replay(self, mode: str, *, now=None) -> dict:
        """手动触发 replay。复用 C++ `ReplayScheduler`(sleep 批 200 / idle 批 30)。
        sleep 今天无调用方=真新能力;idle 已被 tick_all 驱动→手动=按需刷新。"""
        now_iso = parse_now(now).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        sched = _core.ReplayScheduler(self.rt.adapter)
        # #38-C P3: a consolidation LLM (if configured) judges each gist (confidence +
        # summary) during the offline pass; None → deterministic gist. The C++ core
        # owns the NORM-gist prompt + orchestration — Python only injects the adapter.
        # #38-C v2 threshold surface: gist_thresholds (dashboard config) overrides
        # K / T / confidence floor / semantic similarity_threshold (0 = semantic off) /
        # entity_gist_enabled (1 = entity-gist on); empty → the C++ canonical defaults.
        gt = self.gist_thresholds or {}
        kw = {k: gt[k] for k in ("min_holders", "min_replay_count", "min_confidence",
                                 "similarity_threshold", "entity_gist_enabled")
              if gt.get(k) is not None}
        rs = (sched.run_sleep(now_iso, self.consolidation_llm, **kw) if mode == "sleep"
              else sched.run_idle(now_iso, self.consolidation_llm, **kw))
        return {"mode": mode, "sampled": rs.sampled, "compressed": rs.compressed,
                "abstracted": rs.abstracted, "reinforced": rs.reinforced,
                "decayed": rs.decayed, "reconciled": rs.reconciled,
                "forced_consolidated": rs.forced_consolidated,
                "ttl_archived": rs.ttl_archived,
                "gist_candidates": rs.gist_candidates, "gist_failed": rs.gist_failed,
                "gist_gated": rs.gist_gated, "replay_batch_id": rs.replay_batch_id}

    def request_reconsolidation(self, stmt_id: str, *, request_id: str, now=None) -> dict:
        """请求再固化:发 reconsolidate.requested 事件,引擎异步开可塑窗。复用现有绑定。
        仅对 consolidated 行有效(enter_reconsolidating 守卫),否则下游静默 no-op。"""
        now_iso = parse_now(now).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        eid = _core.request_reconsolidation(self.rt.adapter, self.tenant, stmt_id,
                                            request_id, now_iso)
        return {"event_id": eid}

    def fulfill_commitment(self, stmt_id: str, *, now=None) -> dict:
        """手动流转:ACTIVE 承诺 → FULFILLED。核心 `CommitmentEngine::fulfill` 原子守卫
        (只动 ACTIVE;已结算/缺失 → no-op,不改状态、不发事件)。acted=False 时路由回 409。"""
        now_iso = parse_now(now).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        acted = self.commitment_engine.fulfill(stmt_id=stmt_id, tenant_id=self.tenant,
                                               now_iso=now_iso)
        return {"acted": bool(acted), "state": "FULFILLED"}

    def withdraw_commitment(self, stmt_id: str, *, now=None) -> dict:
        """手动流转:ACTIVE 承诺 → WITHDRAWN(同 fulfill 的 ACTIVE 守卫)。withdraw 释放承诺
        → 它保护的记忆此后可被衰减(有血缘后果)。acted=False(非 ACTIVE)时路由回 409。"""
        now_iso = parse_now(now).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        acted = self.commitment_engine.withdraw(stmt_id=stmt_id, tenant_id=self.tenant,
                                                now_iso=now_iso)
        return {"acted": bool(acted), "state": "WITHDRAWN"}

    def close(self) -> None:
        # The SqliteAdapter is closed when its runtime/handle is GC'd; nothing
        # to release explicitly in the embedded core.
        self.conn = None
