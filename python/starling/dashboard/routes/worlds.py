"""/api/worlds：世界注册表、快照（W1/W2）与回滚、归档（W3）。

写操作经 WorldManager（世界管理锁内），统计只读查各世界库、不开引擎。列表持一份引擎租约
（见 _list_worlds），切换或回滚活动世界的窗口内回 503 world_switching。
世界路径只来自服务端注册表：任何路由都不接受客户端传入的文件路径，请求里只有
世界名（C++ 侧按 ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ 校验）和快照 id（只在注册表里查找）。
回滚同样只收快照 id，库、快照与归档的路径都从注册表推出。
W4：每个世界的自动快照配置（PUT …/auto_snapshot）、会话清单（GET …/sessions）、快照行的
sessions_complete / session_id；内容标记 content_mark 只在注册表与绑定内部，不外露。
设计：docs/superpowers/specs/2026-10-25-world-scene-management-design.md §4.3、§5.1、§7、§9
"""
from __future__ import annotations

import logging
import os
from functools import partial

from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, StrictBool, StrictInt

from starling import _core
from starling.dashboard.world_manager import WorldSwitchError

logger = logging.getLogger(__name__)

# 错误码 → HTTP 状态。码本身由 C++（WorldStoreError）或 WorldManager（WorldSwitchError）
# 给出，前端据 detail 字符串显示提示（dashboard/web/src/lib/worlds.ts），所以 detail
# 一律原样回传错误码。未登记的码按 500 处理。
_STATUS = {
    "invalid_world_name": 400, "confirm_mismatch": 400, "rollback_mode_unsupported": 400,
    "invalid_auto_snapshot": 400,
    "world_not_found": 404, "snapshot_not_found": 404,
    "world_exists": 409, "world_protected": 409, "ingest_busy": 409,
    "snapshot_unsupported_backend": 409, "invalid_retention": 409,
    "world_tenant_mismatch": 409, "snapshot_corrupt": 409, "world_in_use": 409,
    "insufficient_disk": 507, "world_switching": 503,
    "world_open_failed": 500, "snapshot_failed": 500, "registry_corrupt": 500,
    "rollback_reopen_failed": 500,
}


class AutoSnapshotBody(BaseModel):
    # 只做形状：取值范围（interval_hours 0–8760）由 C++ 校验，越界回 400 invalid_auto_snapshot。
    # 严格类型（整支审查 M-2）：宽松模式会把 "yes" 转成 True、true 转成 1、5.0 转成 5，到不了绑定层
    # 的严格校验；这里直接回 422。
    on_session: StrictBool
    interval_hours: StrictInt


class CreateWorldBody(BaseModel):
    name: str
    label: str = ""
    auto_snapshot: AutoSnapshotBody | None = None     # None：新世界默认值（按会话开、按时间关）


class SnapshotBody(BaseModel):
    note: str = ""


# 回滚语义 A（spec §5.1）。语义 B（"scene"，只回滚该场景）属于 W5，现在一律拒绝。
_ROLLBACK_MODES = ("discard_after",)


class RollbackBody(BaseModel):
    snapshot_id: str
    confirm: str = ""
    mode: str = "discard_after"


def _fail(code: str) -> HTTPException:
    return HTTPException(status_code=_STATUS.get(code, 500), detail=code)


def _manager(request: Request):
    """取 WorldManager。库路径没有父目录（`:memory:` 等）时 create_app 不建注册表，
    app.state.worlds 为 None（或测试替身里根本没有这个属性）→ 503。"""
    mgr = getattr(request.app.state, "worlds", None)
    if mgr is None:
        raise HTTPException(status_code=503, detail="worlds_unavailable")
    return mgr


async def _run(fn, *args):
    """在工作线程里跑阻塞的 manager / 只读统计调用，把两类领域错误映射成 HTTP 错误，
    其余异常一律兜成 JSON 500 internal_error。

    切换（等线程停稳 + 重开库）和快照（VACUUM 整库）都是阻塞操作，不能占着事件循环。
    每个请求只发起一次 _run，所以同一请求不会并发调用 manager；跨请求的互斥由
    WorldManager 自己的锁负责，这里不再加锁。"""
    try:
        return await to_thread.run_sync(partial(fn, *args))
    except HTTPException:
        raise                                   # 工作函数里已经决定好的 HTTP 错误（如 404）
    except WorldSwitchError as exc:
        raise _fail(exc.code) from exc
    except _core.WorldStoreError as exc:
        raise _fail(_core.world_store_error_code(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — 注册表写不进去（磁盘满、目录不可写）等非领域异常
        # 不兜住的话 Starlette 回的是纯文本 500，前端按 JSON detail 解析会炸。异常文本可能
        # 带服务器路径，只进日志，不回给客户端。
        logger.exception("worlds route failed")
        raise HTTPException(status_code=500, detail="internal_error") from exc


def _archive_totals(mgr, name: str, reg: dict | None = None) -> tuple[int, int]:
    # 回滚归档（<世界 home>/archive/）的个数与字节合计。读不出（archive 目录指到世界目录
    # 之外、无权限）时记 warning、按 0/0 计，不置 stats_error：那个标志的意思是库本身读不出。
    try:
        listed = mgr.archives(name, reg)
    except _core.WorldStoreError as exc:
        logger.warning("world_list_archives failed for %s: %s", name, exc)
        return 0, 0
    return len(listed), sum(item["size_bytes"] for item in listed)


def _world_row(mgr, name: str, entry: dict, snaps: list[dict], active: bool,
               reg: dict | None = None) -> dict:
    # 只读查库、不开引擎：列 N 个世界不用建 N 个引擎。库文件不存在（新建未激活）时全 0。
    # 库文件存在但读不出（损坏、被占）时 world_stats 抛 snapshot_failed：不能让一个坏世界
    # 拖垮整张列表（/worlds 页打不开，坏世界也就没法从界面删掉），该行统计置 0，
    # 并带 stats_error: true（只在出错的行上出现的附加字段）。
    stats_error = False
    try:
        stats = _core.world_stats(entry["db_path"], entry["tenant"])
    except _core.WorldStoreError as exc:
        logger.warning("world_stats failed for %s: %s", name, exc)
        stats = {"db_bytes": 0, "live_bytes": 0, "statements": 0, "engrams": 0}
        stats_error = True
    archives, archive_bytes = _archive_totals(mgr, name, reg)
    row = {"name": name, "label": entry.get("label", ""), "tenant": entry["tenant"],
           "created_at": entry.get("created_at", ""), "active": active,
           "db_bytes": stats["db_bytes"], "live_bytes": stats["live_bytes"],
           "statements": stats["statements"], "engrams": stats["engrams"],
           "snapshots": len(snaps),
           "snapshot_bytes": sum(s["size_bytes"] for s in snaps if not _snapshot_missing(s)),
           "archives": archives, "archive_bytes": archive_bytes,
           "auto_snapshot": {"on_session": entry["auto_snapshot"]["on_session"],
                             "interval_hours": entry["auto_snapshot"]["interval_hours"]}}
    if stats_error:
        row["stats_error"] = True
    return row


def _snapshot_missing(snap: dict) -> bool:
    # 注册项还在、文件已不在（保存失败的补偿删掉了被保留策略清理过的旧文件、手工删除）。
    return not os.path.exists(snap["path"])


def _snapshot_row(snap: dict) -> dict:
    # 白名单：条目里的 path 是服务器上的绝对路径，不外露。文件缺失的条目照常列出（还能
    # 从界面删掉），size_bytes 记 0，并带 missing: true（只在缺失的行上出现的附加字段）。
    # W4 的 sessions_complete / session_id 在 W4 之前的条目里没有，给 None；content_mark 只给
    # 自动快照的判断用，不在白名单里。
    row = {key: snap[key] for key in ("id", "kind", "reason", "note", "created_at",
                                      "world_time", "size_bytes")}
    row["sessions_complete"] = snap.get("sessions_complete")
    row["session_id"] = snap.get("session_id")
    if _snapshot_missing(snap):
        row["size_bytes"] = 0
        row["missing"] = True
    return row


def _list_worlds(mgr) -> dict:
    # world_stats 只读打开每个世界的库，其中包括活动世界的库。全程持一份租约（不惰性建引擎）：
    # 回滚活动世界要等它归还才替换库文件，否则一条在替换之前打开、替换之后才读的只读连接会
    # 重置新库的 -shm（见 world_manager 模块文档）。切换窗口内 lease 抛 world_switching → 503，
    # 前端照旧显示上一次的列表。租约只碰 _gate；这里不拿 manager.lock（mgr.archives 不拿锁）。
    # 回滚非活动世界不等租约，与这里的 world_stats 可能交错：那个库上没有写连接，新库的 -shm
    # 被重置也无害（已接受，见 spec §8.1）。
    with mgr.lease():
        reg = mgr.load()
        snapshots = reg.get("snapshots", {})
        rows = [_world_row(mgr, name, reg["worlds"][name], snapshots.get(name, []),
                           name == reg["active"], reg)
                for name in sorted(reg["worlds"])]
    return {"active": reg["active"], "default": reg["default"],
            "snapshot_retention": reg["snapshot_retention"],
            "auto_snapshot_status": mgr.auto_snapshot_status(), "worlds": rows}


def _create_world(mgr, name: str, label: str, auto_snapshot: dict | None) -> dict:
    entry = mgr.create(name, label, auto_snapshot)
    # 新世界不会是活动世界，也还没有快照；用 create 返回的条目而不是重读注册表，
    # 避免与并发的删除/切换错位。它的库文件还不存在，world_stats 不打开任何库，不需要租约。
    return _world_row(mgr, name, entry, [], False)


def _set_auto_snapshot(mgr, name: str, on_session: bool, interval_hours: int) -> dict:
    return {"world": name, "auto_snapshot": mgr.set_auto_snapshot(name, on_session, interval_hours)}


def _text_or_none(value):
    # C++ 的 SessionRow 没有时间、没有锚点时是空串；契约给 null（前端类型是 string | null）。
    return value or None


def _session_row(row: dict) -> dict:
    anchor = row.get("anchor") or {}
    return {"session_id": row["session_id"], "units": row["units"], "speakers": row["speakers"],
            "first_at": _text_or_none(row.get("first_at")),
            "last_at": _text_or_none(row.get("last_at")),
            "anchor": ({"snapshot_id": anchor["snapshot_id"], "created_at": anchor["created_at"]}
                       if anchor.get("snapshot_id") else None)}


def _list_sessions(mgr, state, name: str, limit: int) -> dict:
    # world_list_sessions 只读打开该世界的库（可能就是活动世界的库）：与 _list_worlds 一样全程持
    # 一份租约（不惰性建引擎），切换窗口内 lease 抛 world_switching → 503。不拿 manager.lock。
    # source_lane_enabled 取当前 config（拿到租约之后才读，同 inspect 的 _cfg），对非活动世界也是：
    # 它表示「新写入会不会产生会话」。
    with mgr.lease():
        lane_enabled = bool(state.config.source_lane_enabled)
        reg = mgr.load()
        listed = _core.world_list_sessions(reg, name, limit)
    return {"world": name, "source_lane_enabled": lane_enabled,
            "total": listed["total"], "null_observed": listed["null_observed"],
            "truncated": listed["truncated"],
            "sessions": [_session_row(row) for row in listed["sessions"]]}


def _list_snapshots(mgr, name: str) -> dict:
    reg = mgr.load()
    if name not in reg["worlds"]:
        raise _fail("world_not_found")
    snaps = reg.get("snapshots", {}).get(name, [])
    # 注册表本就是最新在前；显式按 created_at 降序（稳定排序，同秒保持注册表顺序）。
    ordered = sorted(snaps, key=lambda snap: snap["created_at"], reverse=True)
    return {"snapshots": [_snapshot_row(snap) for snap in ordered]}


async def _broadcast(request: Request, kind: str, payload: dict) -> None:
    ws_manager = getattr(request.app.state, "ws_manager", None)
    if ws_manager is not None:
        await ws_manager.broadcast({"type": kind, "payload": payload})


def build_worlds_router(require_token) -> APIRouter:
    router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])

    @router.get("/worlds")
    async def list_worlds(request: Request):
        mgr = _manager(request)
        return await _run(_list_worlds, mgr)

    @router.post("/worlds")
    async def create_world(request: Request, body: CreateWorldBody):
        mgr = _manager(request)
        auto = body.auto_snapshot.model_dump() if body.auto_snapshot is not None else None
        return await _run(_create_world, mgr, body.name, body.label, auto)

    @router.post("/worlds/{name}/activate")
    async def activate_world(request: Request, name: str):
        mgr = _manager(request)
        await _run(mgr.activate, name)
        await _broadcast(request, "world_switched", {"active": name})
        return {"active": name}

    @router.delete("/worlds/{name}")
    async def delete_world(request: Request, name: str, confirm: str = ""):
        mgr = _manager(request)
        if confirm != name:
            raise _fail("confirm_mismatch")      # 不碰 manager
        await _run(mgr.delete, name)
        return {"deleted": name}

    @router.put("/worlds/{name}/auto_snapshot")
    async def set_auto_snapshot(request: Request, name: str, body: AutoSnapshotBody):
        mgr = _manager(request)
        return await _run(_set_auto_snapshot, mgr, name, body.on_session, body.interval_hours)

    @router.get("/worlds/{name}/sessions")
    async def list_sessions(request: Request, name: str,
                            limit: int = Query(200, ge=1, le=1000)):
        # 越界的 limit 在这里回 422（C++ 的 1–1000 校验留作兜底，那条会回 400
        # invalid_auto_snapshot，前端会显示成「自动快照设置无效」）。
        mgr = _manager(request)
        return await _run(_list_sessions, mgr, request.app.state, name, limit)

    @router.get("/worlds/{name}/snapshots")
    async def list_snapshots(request: Request, name: str):
        mgr = _manager(request)
        return await _run(_list_snapshots, mgr, name)

    @router.post("/worlds/{name}/snapshots")
    async def create_snapshot(request: Request, name: str, body: SnapshotBody = SnapshotBody()):
        mgr = _manager(request)
        snap = await _run(mgr.take_snapshot, name, body.note)
        await _broadcast(request, "snapshot_created", {"world": name, "id": snap["id"]})
        return _snapshot_row(snap)

    @router.delete("/worlds/{name}/snapshots/{snapshot_id}")
    async def delete_snapshot(request: Request, name: str, snapshot_id: str):
        mgr = _manager(request)
        await _run(mgr.delete_snapshot, name, snapshot_id)
        return {"deleted": snapshot_id}

    @router.post("/worlds/{name}/rollback")
    async def rollback_world(request: Request, name: str, body: RollbackBody):
        mgr = _manager(request)
        if body.confirm != name:
            raise _fail("confirm_mismatch")              # 不碰 manager
        if body.mode not in _ROLLBACK_MODES:
            raise _fail("rollback_mode_unsupported")     # 不碰 manager
        rolled_back = {"world": name, "snapshot_id": body.snapshot_id}
        try:
            out = await _run(mgr.rollback, name, body.snapshot_id)
        except HTTPException as exc:
            if exc.detail == "rollback_reopen_failed":
                # 库已经换成了快照的内容，只是引擎没能重开：别的标签页同样要知道库变了。
                await _broadcast(request, "world_rolled_back", rolled_back)
            raise
        await _broadcast(request, "world_rolled_back", rolled_back)
        return out

    @router.delete("/worlds/{name}/archives")
    async def purge_archives(request: Request, name: str, confirm: str = ""):
        mgr = _manager(request)
        if confirm != name:
            raise _fail("confirm_mismatch")              # 不碰 manager
        return {"deleted": await _run(mgr.purge_archives, name)}

    return router
