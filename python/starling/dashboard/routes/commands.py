"""Command routes — go through the DashboardEngine (single writer).

Engine calls run real network I/O (LLM extraction / embedding, with C++-side
retry backoff up to tens of seconds), so handlers offload them to a worker
thread via anyio.to_thread — running them inline on the event loop would
freeze every other request and the WS heartbeat for the whole call.

按世界寻址（见 world_scope）：同一份路由挂在 /api（默认世界）与 /api/w/{world} 下，引擎是该世界的
引擎（引擎池按需打开）。广播带世界名（realtime.world_event）。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from functools import partial

from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from starling.dashboard.routes.world_scope import lease_off_loop, world_name


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 'Z' string (tick default)."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class RememberBody(BaseModel):
    text: str
    holder: str | None = None
    interlocutor: str | None = None
    now: str | None = None
    provider: str | None = None  # per-turn extraction-model override (registry name); None = bound role


class RecallBody(BaseModel):
    query: str
    perspective: str = "first_person"
    k: int = 10
    mode: str = "semantic"
    intent: str | None = None    # 非空 → 走 RetrievalPlanner(P3.a1)
    target: str | None = None    # BELIEF_OF_OTHER / COMMON_GROUND 对方
    holder: str | None = None    # override recall holder 维度(默认 dashboard agent)


class ConverseBody(BaseModel):
    message: str
    holder: str | None = None
    interlocutor: str | None = None
    k: int = 6                   # relevant memories to inject
    now: str | None = None
    provider: str | None = None  # per-turn chat-model override (registry name); None = bound role


class TickBody(BaseModel):
    now: str | None = None  # defaults to current UTC time at request handling


class ForgetBody(BaseModel):
    ids: list[str]
    now: str | None = None  # None → MemoryCore resolves to current UTC time


class ClearMemoryBody(BaseModel):
    confirm: str            # 必须等于当前租户名(设置页「危险区」要求手输);不符 → 400


# ── 片 6 干预集 ──────────────────────────────────────────────────────────
class ReviewBody(BaseModel):
    stmt_id: str            # approve(review_requested→approved);reject 走 /api/forget

class ReplayTriggerBody(BaseModel):
    mode: str = "idle"      # "sleep"(批 200,真新)| "idle"(批 30,已被 tick 驱动)

class ReconsolidateBody(BaseModel):
    stmt_id: str            # 仅对 consolidated 行有效(下游守卫)

class CommitmentBody(BaseModel):
    stmt_id: str            # ACTIVE 承诺手动流转(fulfill/withdraw);非 ACTIVE → 409


async def _engine(request: Request, world: str = Depends(world_name)):
    """依赖项：持 world 的一份引擎租约，覆盖整个请求（线程池排队、锁外的 LLM 阶段、最后的提交），
    请求结束（响应发出）后归还。世界没开着时池按需建它的引擎。

    Single-process only: under `uvicorn --workers N` each worker would build
    its own engine writing the same SQLite. Run one worker.

    世界不存在 → 404 world_not_found；正在关闭、恢复、删除 → 503 world_busy（world_scope 的异常处理器）：
    关闭与恢复要等在途租约全部归还才丢引擎，窗口里的新请求不能再拿到旧引擎。

    租约在工作线程里取（裁定 R32，world_scope.lease_off_loop）：世界没开着时要取它的世界锁建引擎，世界锁
    可能被快照、清理归档占着，事件循环不能等它。取到之后立刻进 try（await 返回与 try 之间没有检查点），
    请求被取消也照样归还；取的途中被取消，由 lease_off_loop 归还线程随后拿到的租约。
    """
    lease = await lease_off_loop(request.app.state.pool, world)
    try:
        yield lease.engine
    finally:
        lease.release()


async def _writable_engine(request: Request, world: str = Depends(world_name)):
    """改内容的命令路由的依赖项（裁定 R51、R82）：进 pool.dashboard_write(world)——C++ 的看板写入准入（被客户端占着写入的世界
    409 world_held，可回收世界也是；没人占着的只读打开的世界 409 world_read_only）与在途计数在同一次持池锁内完成——再与 _engine
    一样持一份引擎租约，覆盖整个请求。计数在请求结束（含异常、取消）时减回去；计数大于 0 时客户端的可写 admit 回 world_busy。
    被拒时不计数、不取租约、不建引擎。召回与 working_set 不经这里。"""
    pool = request.app.state.pool
    with pool.dashboard_write(world):
        lease = await lease_off_loop(pool, world)
        try:
            yield lease.engine
        finally:
            lease.release()


def build_commands_router(require_token) -> APIRouter:
    """路由不带前缀：create_app 把它挂在 /api 与 /api/w/{world} 两处（见 world_scope）。"""
    router = APIRouter(dependencies=[Depends(require_token)])

    @router.post("/remember")
    async def remember(body: RememberBody, request: Request, eng=Depends(_writable_engine)):
        from starling.dashboard.engine import _LLMNotConfigured
        from starling import _core
        try:
            r = await to_thread.run_sync(partial(
                eng.remember, body.text, holder=body.holder,
                interlocutor=body.interlocutor, now=body.now, provider=body.provider))
        except _LLMNotConfigured:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail="llm_not_configured")
        except _core.WriteGateRejected:
            # #2:三相锁外 extract 期间健康转 DRAINING → commit 抛;回 503 而非 500。
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                                detail="draining")
        await _broadcast(request, "statement_added", {"statement_ids": r["statement_ids"]})
        return r

    @router.post("/recall")
    async def recall(body: RecallBody, request: Request, eng=Depends(_engine)):
        if body.intent:
            # intent 以 C++ QueryIntent 枚举为单一源;非法值在这里挡成 422,
            # 否则 plan_query 的 getattr 会 AttributeError → 500。
            from starling import _core
            if not hasattr(_core.QueryIntent, body.intent):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="invalid_intent")
            payload = await to_thread.run_sync(partial(
                eng.plan_query, body.query, intent=body.intent,
                target=body.target, k=body.k))
            await _broadcast(request, "recall", {"n": len(payload["results"])})
            return payload
        hits = await to_thread.run_sync(partial(
            eng.recall, body.query, perspective=body.perspective, k=body.k,
            mode=body.mode, holder=body.holder))
        out = [{"id": h["row"].id, "subject": h["row"].subject_id,
                "predicate": h["row"].predicate, "object": h["row"].object_value,
                "score": h["score"]} for h in hits]
        await _broadcast(request, "recall", {"n": len(out)})
        return {"results": out}

    @router.post("/converse")
    async def converse(body: ConverseBody, request: Request, eng=Depends(_writable_engine)):
        from starling.dashboard.engine import _LLMNotConfigured
        try:
            r = await to_thread.run_sync(partial(
                eng.converse, body.message, holder=body.holder,
                interlocutor=body.interlocutor, k=max(1, min(body.k, 50)), now=body.now,
                provider=body.provider))
        except _LLMNotConfigured:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail="llm_not_configured")
        # The turn consolidated the exchange → tell other views to refresh.
        if r.get("statement_ids"):
            await _broadcast(request, "statement_added",
                             {"statement_ids": r["statement_ids"]})
        return r

    @router.post("/tick")
    async def tick(body: TickBody, request: Request, eng=Depends(_writable_engine)):
        payload = await to_thread.run_sync(eng.tick, body.now or _now_iso())
        await _broadcast(request, "tick", payload)
        if payload["fired"]:
            await _broadcast(request, "commitment_fired", {"fired": payload["fired"]})
        return payload

    @router.get("/working_set")
    async def working_set(request: Request, interlocutor: str, goal: str | None = None,
                          token_budget: int = 2000, holder: str | None = None,
                          eng=Depends(_engine)):
        return await to_thread.run_sync(partial(
            eng.working_set, interlocutor, goal=goal, token_budget=token_budget,
            holder=holder))

    @router.post("/forget")
    async def forget(body: ForgetBody, request: Request, eng=Depends(_writable_engine)):
        r = await to_thread.run_sync(partial(eng.forget, body.ids, now=body.now))
        await _broadcast(request, "statement_forgotten", r)
        return r

    @router.post("/clear_memory")
    async def clear_memory(body: ClearMemoryBody, request: Request, eng=Depends(_writable_engine)):
        # 设置页「危险区」:物理清空当前租户的全部记忆(保留 self 认知体),并把该租户
        # spool 里尚未 claim 的会话 job 移到 discarded/。confirm 必须等于当前租户名。
        from starling import _core
        from starling.dashboard.engine import ClearConfirmMismatch
        try:
            r = await to_thread.run_sync(partial(eng.clear_memory, confirm=body.confirm))
        except ClearConfirmMismatch:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="confirm_mismatch")
        except _core.WriteGateRejected:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                                detail="draining")
        await _broadcast(request, "memory_cleared",
                         {"tenant": r["tenant"], "total": r["total"],
                          "spool_discarded": r["spool_discarded"]})
        return r

    # ── 片 6 干预集:触发式安全写(全经 engine self._lock 串行) ─────────────
    @router.post("/review")
    async def review(body: ReviewBody, request: Request, eng=Depends(_writable_engine)):
        # approve:review_requested→approved(守卫幂等;非该态返回 approved=0)。
        r = await to_thread.run_sync(partial(eng.approve_review, body.stmt_id))
        await _broadcast(request, "statement_added", {"statement_ids": [body.stmt_id]})
        return r

    @router.post("/replay_trigger")
    async def replay_trigger(body: ReplayTriggerBody, request: Request, eng=Depends(_writable_engine)):
        # 手动触发 replay;持写锁整段(操作者发起)。新批次 → 刷新梦境/生命周期。
        mode = body.mode if body.mode in ("sleep", "idle") else "idle"
        r = await to_thread.run_sync(partial(eng.run_replay, mode))
        await _broadcast(request, "tick", r)
        return r

    @router.post("/reconsolidate")
    async def reconsolidate(body: ReconsolidateBody, request: Request, eng=Depends(_writable_engine)):
        # 请求再固化(发 reconsolidate.requested 事件,引擎异步开可塑窗)。每次发新 uuid
        # request_id;真正的重复去重在引擎侧按 stmt_id+tenant 归并到同一窗口(open_or_append),
        # 故连点不会建重复窗口(非「当日去重」——那个键因 uuid 唯一从不命中)。
        r = await to_thread.run_sync(partial(
            eng.request_reconsolidation, body.stmt_id, request_id=uuid.uuid4().hex))
        await _broadcast(request, "tick", {"reconsolidate_requested": body.stmt_id})
        return r

    @router.post("/commitment/fulfill")
    async def commitment_fulfill(body: CommitmentBody, request: Request,
                                 eng=Depends(_writable_engine)):
        # 手动 fulfill:ACTIVE→FULFILLED(核心 CommitmentEngine 原子守卫)。非 ACTIVE 不
        # 改状态 → 409(不广播);成功广播 commitment_transition 供面板即时刷新。
        r = await to_thread.run_sync(partial(eng.fulfill_commitment, body.stmt_id))
        if not r["acted"]:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="not_active")
        await _broadcast(request, "commitment_transition",
                         {"stmt_id": body.stmt_id, "state": r["state"]})
        return r

    @router.post("/commitment/withdraw")
    async def commitment_withdraw(body: CommitmentBody, request: Request,
                                  eng=Depends(_writable_engine)):
        # 手动 withdraw:ACTIVE→WITHDRAWN(释放承诺 → 它保护的记忆此后可衰减)。同样
        # ACTIVE-only:非 ACTIVE → 409,不广播。
        r = await to_thread.run_sync(partial(eng.withdraw_commitment, body.stmt_id))
        if not r["acted"]:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="not_active")
        await _broadcast(request, "commitment_transition",
                         {"stmt_id": body.stmt_id, "state": r["state"]})
        return r

    return router


async def _broadcast(request: Request, kind: str, payload: dict) -> None:
    """本请求世界上的事件广播（带世界名）。"""
    mgr = getattr(request.app.state, "ws_manager", None)
    if mgr is not None:
        await mgr.broadcast_world(kind, world_name(request), payload)
