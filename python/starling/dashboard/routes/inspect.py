"""Read-only inspection routes (SQL-backed).

按世界寻址（见 world_scope）：同一份路由挂在 /api（默认世界）与 /api/w/{world} 下。

- SQL 路由经依赖项 `_world_db` 持该世界的一份不建引擎的租约（lease(world, build=False)），从打开库之前
  到关闭之后（整个请求），按 `pool.context(world)` 给出的库路径与租户只读打开库。检视从不打开世界（裁定
  R21）：关着的世界照样能看，只读它的库文件，不建引擎、不跑迁移。
  恢复（rollback）把同一路径上的 inode 换掉，要等该世界的租约（含这里的只读租约）归零才动文件：不持租约
  的只读连接若在替换之前打开了旧 inode、替换之后才第一次读，会按路径打开新库的 -shm，在同进程里认为自己
  是第一个连接，把引擎的 wal-index 截断重建（见 world_manager 模块文档）。
- 读引擎内存态的两个路由（runtime_health / ingest_status）同样持不建引擎的租约；世界没开着时没有可读的
  内存态，回 503。
- 世界不存在 → 404 world_not_found；正在关闭、恢复、删除 → 503 world_busy（world_scope 的异常处理器）。
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import partial

from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException, Request

from starling.dashboard import queries
from starling.dashboard.routes.world_scope import world_name


async def _world_db(request: Request, world: str = Depends(world_name)):
    """所有 SQL 检视路由的依赖项：持 world 的一份不建引擎的租约覆盖整个请求，产出该世界的 WorldContext
    （路由只用它的 db_path 与 tenant）。上下文在拿到租约之后才读。

    世界的库文件在它第一次被打开（写入、tick 等命令路由）时才建；还没有库文件的世界回 409
    world_not_initialized，而不是让只读打开报 500。检视不替它建引擎（裁定 R21）。

    取租约与进入 try 之间没有 await：任务在那里被取消，租约就永远不归还，此后关闭、恢复这个世界都会等满
    超时回 world_busy。"""
    pool = request.app.state.pool
    lease = pool.lease(world, build=False)
    try:
        ctx = pool.context(world)
        if not os.path.exists(ctx.db_path):
            raise HTTPException(status_code=409, detail="world_not_initialized")
        yield ctx
    finally:
        lease.release()


@contextmanager
def _leased_engine(request: Request):
    """读引擎内存态的路由用：持本请求世界的一份不建引擎的租约读完。世界没开着时 engine 是 None。"""
    pool = request.app.state.pool
    with pool.lease(world_name(request), build=False) as engine:
        yield engine
        engine = None


def _metrics_to_dict(ms) -> dict:
    return {
        "outbox_lag_sequence": ms.outbox_lag_sequence,
        "subscriber_failure_rate": ms.subscriber_failure_rate,
        "extraction_queue_depth": ms.extraction_queue_depth,
        "projection_lag_seconds": ms.projection_lag_seconds,
        "runtime_event_loop_lag_ms": ms.runtime_event_loop_lag_ms,
        "vector_delete_lag": ms.vector_delete_lag,
        "erased_evidence_visible_count": ms.erased_evidence_visible_count,
    }


def _event_to_dict(e) -> dict:
    return {
        "previous_status": e.previous_status.name,
        "current_status": e.current_status.name,
        "trigger": e.trigger,
        "missing_capabilities": list(e.missing_capabilities),
        "metrics_snapshot": _metrics_to_dict(e.metrics_snapshot),
    }


def _default_since() -> str:
    # dogfood 子项 B(Task 2):metrics 路由 since 缺省 = 7 天前(与 bucket 缺省
    # 3600s 一起构成「近一周、按小时」的默认时间序列窗口)。
    return (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_inspect_router(require_token) -> APIRouter:
    """路由不带前缀：create_app 把它挂在 /api 与 /api/w/{world} 两处（见 world_scope）。"""
    router = APIRouter(dependencies=[Depends(require_token)])

    @router.get("/overview")
    async def overview(c=Depends(_world_db)):
        return queries.overview(c.db_path, c.tenant)

    @router.get("/brain_map")
    async def brain_map(c=Depends(_world_db)):   # Phase 3 片 1 — 类脑 IA 落地页 9 脑区计数
        return queries.brain_map(c.db_path, c.tenant)

    @router.get("/statements")
    async def statements(holder: str = "", perspective: str = "",
                         predicate: str = "", review_status: str = "",
                         consolidation_state: str = "", modality: str = "",
                         belief_order: str = "", subject_kind: str = "",
                         limit: int = 100, offset: int = 0, c=Depends(_world_db)):
        return queries.statements(c.db_path, c.tenant, holder=holder,
                                  perspective=perspective, predicate=predicate,
                                  review_status=review_status,
                                  consolidation_state=consolidation_state,
                                  modality=modality,
                                  belief_order=belief_order,
                                  subject_kind=subject_kind,
                                  limit=limit, offset=offset)

    @router.get("/engrams")
    async def engrams(source_kind: str = "", privacy_class: str = "",
                      erased: str = "", q: str = "", limit: int = 100, offset: int = 0,
                      c=Depends(_world_db)):
        # T0a — 原始数据·证据浏览:engram(不可变原文证据)列表(只读派生查询)。
        return queries.engrams_list(c.db_path, c.tenant, source_kind=source_kind,
                                    privacy_class=privacy_class, erased=erased, q=q,
                                    limit=limit, offset=offset)

    @router.get("/engram/{engram_id}")
    async def engram(engram_id: str, c=Depends(_world_db)):
        # T0a — 单条 engram 详情:privacy-gated 预览(复用 provenance 同款抑制规则)+
        # 引用它的 statements 反查。
        r = queries.engram_detail(c.db_path, c.tenant, engram_id)
        if r is None:
            raise HTTPException(status_code=404, detail="engram not found")
        return r

    @router.get("/personae")
    async def personae(c=Depends(_world_db)):
        # T0d-2 — 新皮层·画像(Persona):containers WHERE kind='persona'(只读派生查询)。
        return queries.personae(c.db_path, c.tenant)

    @router.get("/common_ground")
    async def common_ground(c=Depends(_world_db)):
        # T0d-2 — 新皮层·共识(CommonGround):common_ground 五态,LEFT JOIN 带语句文本
        #(只读派生查询)。
        return queries.common_ground(c.db_path, c.tenant)

    @router.get("/cognizers")
    async def cognizers(c=Depends(_world_db)):
        return queries.cognizers(c.db_path, c.tenant)

    @router.get("/commitments")
    async def commitments(c=Depends(_world_db)):
        return queries.commitments(c.db_path, c.tenant)

    @router.get("/replay")
    async def replay(c=Depends(_world_db)):
        return queries.replay(c.db_path, c.tenant)

    @router.get("/lifecycle")
    async def lifecycle(c=Depends(_world_db)):   # Phase 3 片 4 — 生命周期(只读事件派生)
        return queries.lifecycle(c.db_path, c.tenant)

    @router.get("/forecast")
    async def forecast(limit: int = 200, c=Depends(_world_db)):
        # Phase 3 片 5 — 衰减预报:C++ forgetting_curve 只读投影(server now)。
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return queries.forecast(c.db_path, c.tenant, now=now,
                                limit=max(1, min(limit, 1000)))

    @router.get("/conflicts")
    async def conflicts(c=Depends(_world_db)):
        return queries.conflicts(c.db_path, c.tenant)

    @router.get("/gists")
    async def gists(c=Depends(_world_db)):   # #38-C v2 — consolidation NORM gists (read-only)
        return queries.gists(c.db_path, c.tenant)

    @router.get("/gist_members/{gist_id}")
    async def gist_members(gist_id: str, c=Depends(_world_db)):   # #38-C v2 — gist lineage
        return queries.gist_members(c.db_path, c.tenant, gist_id)

    @router.get("/queues")
    async def queues(c=Depends(_world_db)):
        return queries.queues(c.db_path, c.tenant)

    @router.get("/metrics/embed_depth")
    async def metrics_embed_depth(since: str = "", bucket: int = 3600, c=Depends(_world_db)):
        # dogfood 子项 B(Task 2):embed 队列深度时间序列,读 Task 1 采样器写的
        # host metrics.db(与 dashboard.db 分离的文件;缺文件→空 series,不 500)。
        # bucket 钳制到 >=1(同 vitals/forecast/search 等兄弟路由的 limit 钳制一致)——
        # 0 或负值会在 _bucket 里对 bucket_s 取模,除零崩 500。
        return queries.metrics_embed_depth(c.db_path, since or _default_since(), max(1, bucket))

    @router.get("/metrics/latency")
    async def metrics_latency(since: str = "", bucket: int = 3600, c=Depends(_world_db)):
        # dogfood 子项 B(Task 2):抽取时延时间序列(p50/p95),派生 extraction_attempt。
        # bucket 钳制到 >=1,理由同 metrics_embed_depth。
        return queries.metrics_latency(c.db_path, c.tenant, since or _default_since(), max(1, bucket))

    @router.get("/metrics/gist_quality")
    async def metrics_gist_quality(since: str = "", bucket: int = 3600, c=Depends(_world_db)):
        # dogfood 子项 B(Task 3):gist 质量代理——funnel(gist_candidates→abstracted,
        # gated/failed 未持久化不派生)+ confidence/member/summary 分布。bucket 钳制到
        # >=1,理由同 metrics_embed_depth(否则 _bucket 对 bucket_s 取模会除零 500)。
        return queries.metrics_gist_quality(c.db_path, c.tenant, since or _default_since(),
                                            max(1, bucket))

    @router.get("/vitals")
    async def vitals(limit: int = 50, c=Depends(_world_db)):
        # Z-suffixed to match the repo's stored-timestamp convention (engine.py
        # / commands.py _now_iso). The overdue-window filter is a STRING compare
        # against Z-suffixed close_deadline values, so a "+00:00" suffix here
        # would be a latent boundary bug.
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return queries.vitals(c.db_path, c.tenant, now=now,
                              list_limit=max(1, min(limit, 500)))   # clamp(同 forecast/search 一致)

    @router.get("/statement/{statement_id}")
    async def statement(statement_id: str, c=Depends(_world_db)):
        row = queries.statement_by_id(c.db_path, c.tenant, statement_id)
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row

    @router.get("/statement_search")
    async def statement_search(q: str = "", limit: int = 20, c=Depends(_world_db)):
        # Phase 3 片 3 — 透视镜取镜:只读文本查找(副作用自由,不走语义召回)。
        return queries.search_statements(c.db_path, c.tenant, q,
                                         limit=max(1, min(limit, 100)))

    @router.get("/provenance/{statement_id}")
    async def provenance(statement_id: str, max_depth: int = 6, c=Depends(_world_db)):
        # Phase 3 片 3 — 透视镜:来源取证树(只读)。max_depth 钳制防递归滥用。
        tree = queries.provenance(c.db_path, c.tenant, statement_id,
                                  max_depth=max(1, min(max_depth, 12)))
        if tree is None:
            raise HTTPException(status_code=404, detail="not_found")
        return tree

    @router.get("/cascade_preview/{statement_id}")
    async def cascade_preview(statement_id: str, max_depth: int = 6, c=Depends(_world_db)):
        # 片 6 — 级联预览:遗忘前看会波及哪些派生后代(只读 inform-only)。max_depth 钳制。
        # 唯一会全表扫(反向派生边无索引)的检视路由 → 丢线程,避免大租户下阻塞事件循环。
        preview = await to_thread.run_sync(partial(
            queries.cascade_preview, c.db_path, c.tenant, statement_id,
            max_depth=max(1, min(max_depth, 12))))
        if preview is None:
            raise HTTPException(status_code=404, detail="not_found")
        return preview

    @router.get("/runtime_health")
    async def runtime_health(request: Request):
        with _leased_engine(request) as eng:
            if eng is None:
                # health/events are in-memory; with no live engine there is nothing to read.
                raise HTTPException(status_code=503, detail="engine not initialized")
            out = {
                "status": eng.health().name,
                "events": [_event_to_dict(e) for e in eng.events()],
            }
            eng = None
        return out

    @router.get("/ingest_status")
    async def ingest_status(request: Request):
        # dogfood 子项 A(Task 3):spool 队列深度 —— 引擎内存态(remember 累计
        # 耗时计数器 ingest_remember_ms_total),无引擎则无可读(同
        # runtime_health 的能力探测模式)。
        # W4：合并自动快照的失败计数与最近一次错误码（失败照常摄入，只在这里和 GET /api/worlds
        # 外露）。计数是引擎池里所有世界合计；没有注册表（:memory: 等）时没有自动快照，是 0 / None。
        with _leased_engine(request) as eng:
            if eng is None:
                raise HTTPException(status_code=503, detail="engine not initialized")
            status = dict(eng.ingest_status())
            eng = None
        auto = request.app.state.pool.auto_snapshot_status()
        status["auto_snapshot_failures"] = auto["failures"]
        status["auto_snapshot_last_error"] = auto["last_error"]
        return status

    return router
