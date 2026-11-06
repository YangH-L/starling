"""/api/worlds：世界注册表（列表、详情、建、删、设置、关闭）、快照（W1/W2）、回滚与归档（W3）、
自动快照与会话（W4）。记忆服务第 1 期 Task 6 起全部经引擎池（app.state.pool，WorldPool）。

没有「活动世界」：任何世界都可以同时开着，激活路由已删除。列表只读注册表（标签过滤、分页都在 C++
RegistryDb::list），不逐个打开库：开着的世界的计数由池现读（live_stats），关着的取注册表缓存列
（spec §5.5）。库的字节数、engram 数只在详情（GET /api/worlds/{name}）里给。

世界路径只来自服务端注册表：任何路由都不接受客户端传入的文件路径，请求里只有世界名（C++ 按
^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ 校验）和快照 id（只在注册表里查找）。

设置（spec §8）：建世界时 C++ 复制默认世界的当前设置再覆盖 body 里给的（create_world），改设置是部分
合并（update_world_settings）；合并、校验、补默认值全在 C++。返回给客户端的设置不含密钥，每个 endpoint
只多一个 key_set（spec §8.2）。默认世界的 endpoint 等段归 /api/config（裁定 R35）。

错误一律 {"detail": 码}（前端 dashboard/web/src/lib/worlds.ts 按码显示提示）；码由 C++（WorldStoreError）
或引擎池（WorldSwitchError）给出，这里只查表定 HTTP 状态。
设计：docs/superpowers/specs/2026-11-03-memory-service-design.md §5、§7、§8、§13.1
（快照、回滚语义沿用 docs/superpowers/specs/2026-10-25-world-scene-management-design.md §4.3、§5.1、§7）。
"""
from __future__ import annotations

import logging
import os
from functools import partial

from anyio import to_thread
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel, StrictBool, StrictInt

from starling import _core
from starling.dashboard.engine import endpoint_api_key
from starling.dashboard.world_manager import WorldSwitchError

logger = logging.getLogger(__name__)

# 错误码 → HTTP 状态。未登记的码按 500。
_STATUS = {
    "invalid_world_name": 400, "confirm_mismatch": 400, "rollback_mode_unsupported": 400,
    "invalid_auto_snapshot": 400, "invalid_settings": 400, "invalid_labels": 400,
    "invalid_world_query": 400,
    "world_not_found": 404, "snapshot_not_found": 404,
    "world_exists": 409, "world_protected": 409, "labels_immutable": 409,
    "snapshot_unsupported_backend": 409, "invalid_retention": 409, "endpoint_changed": 409,
    "world_tenant_mismatch": 409, "snapshot_corrupt": 409, "world_in_use": 409,
    "insufficient_disk": 507, "world_busy": 503, "worlds_unavailable": 503,
    "world_open_failed": 500, "snapshot_failed": 500, "registry_corrupt": 500,
    "rollback_reopen_failed": 500,
    # 第 3 期（spec §6、§13.2）。
    "world_read_only": 409, "world_schema_behind": 409, "world_not_seed": 409,
    "fork_digest_mismatch": 500, "restore_digest_mismatch": 500,
}

# 世界设置里带 provider 密钥的段（返回时每段加 key_set）。
_ENDPOINT_SECTIONS = ("extraction", "embedding", "chat", "consolidation")
# 客户端设置里不得出现的键：key_set 只在返回里由服务端算出（裁定 R35）。
_SERVER_ONLY_KEYS = frozenset({"key_set"})
# 列表的标签过滤参数前缀：?label.<key>=<value>（可给多个，全部匹配）。
_LABEL_PREFIX = "label."


class AutoSnapshotBody(BaseModel):
    # 只做形状：取值范围（interval_hours 0–8760）由 C++ 校验，越界回 400 invalid_auto_snapshot。
    # 严格类型（整支审查 M-2）：宽松模式会把 "yes" 转成 True、true 转成 1、5.0 转成 5，到不了绑定层
    # 的严格校验；这里直接回 422。
    on_session: StrictBool
    interval_hours: StrictInt


class CreateWorldBody(BaseModel):
    name: str
    label: str = ""
    labels: dict[str, str] = {}
    # 部分设置，覆盖从默认世界复制来的值（C++ 合并、校验）。原样转发：不经模型转换，没给的键不出现。
    settings: dict | None = None
    # 旧字段，映射到 settings.background.auto_snapshot（C++ 合并）。None：新世界默认值。
    auto_snapshot: AutoSnapshotBody | None = None


class SnapshotBody(BaseModel):
    note: str = ""


class ForkBody(BaseModel):
    # spec §6.1。snapshot_id 为空 = 以世界当前状态为来源。settings 原样转发（部分设置，C++ 合并、校验，同
    # CreateWorldBody）。严格布尔：宽松模式会把 "yes" 转成 True，到不了 C++。
    new_name: str
    snapshot_id: str | None = None
    labels: dict[str, str] = {}
    retention_group: str | None = None
    reclaimable: StrictBool = False
    seed: StrictBool = False
    settings: dict | None = None


class UpgradeSeedBody(BaseModel):
    new_name: str


# 回滚语义 A（spec §5.1）。语义 B（"scene"，只回滚该场景）属于 W5，现在一律拒绝。
_ROLLBACK_MODES = ("discard_after",)


class RollbackBody(BaseModel):
    snapshot_id: str
    confirm: str = ""
    mode: str = "discard_after"


def _fail(code: str) -> HTTPException:
    return HTTPException(status_code=_STATUS.get(code, 500), detail=code)


async def _run(fn, *args, **kwargs):
    """在工作线程里跑阻塞的池调用（快照 VACUUM、恢复、关闭等租约、删目录都可能要几秒），把两类领域错误
    映射成 HTTP 错误，其余异常一律兜成 JSON 500 internal_error。事件循环上的代码从不等世界锁（见
    world_manager 模块文档的锁顺序），所以凡可能取世界锁或读库的调用都经这里。"""
    try:
        return await to_thread.run_sync(partial(fn, *args, **kwargs))
    except HTTPException:
        raise                                   # 工作函数里已经决定好的 HTTP 错误
    except WorldSwitchError as exc:
        if _STATUS.get(exc.code, 500) >= 500:
            logger.warning("worlds route: %s: %s", exc.code, exc.detail)
        raise _fail(exc.code) from exc
    except _core.WorldStoreError as exc:
        raise _fail(_core.world_store_error_code(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — 注册表写不进去（磁盘满、目录不可写）等非领域异常
        # 不兜住的话 Starlette 回的是纯文本 500，前端按 JSON detail 解析会炸。异常文本可能
        # 带服务器路径，只进日志，不回给客户端。
        logger.exception("worlds route failed")
        raise HTTPException(status_code=500, detail="internal_error") from exc


def _pool(request: Request):
    return request.app.state.pool


def _require_registry(pool):
    """:memory: 等没有注册表的库（裁定 R6）：世界管理一律 503 worlds_unavailable。"""
    if pool.registry is None:
        raise _fail("worlds_unavailable")
    return pool.registry


# ───────────────────────── 行与设置的形状 ─────────────────────────

def _public_settings(settings: dict, providers: dict) -> dict:
    """实际生效的设置去掉密钥后给客户端（spec §8.2）：设置里本来就没有密钥（只有 provider_name 与
    base_url），每个 endpoint 段加 key_set，表示调用时有没有密钥可用（按 provider_name 取 provider 当前
    的密钥；显式端点取服务进程的 OPENAI_API_KEY，规则见 engine.endpoint_api_key）。"""
    out = dict(settings)
    for section in _ENDPOINT_SECTIONS:
        ep = settings.get(section)
        if ep:
            out[section] = {**ep, "key_set": bool(endpoint_api_key(ep, providers))}
    return out


def _contains_server_only_key(value) -> bool:
    if isinstance(value, dict):
        return any(key in _SERVER_ONLY_KEYS or _contains_server_only_key(item)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_server_only_key(item) for item in value)
    return False


def _require_client_settings(settings) -> None:
    """客户端给的设置：必须是对象，不能带服务端才算的键（key_set）。其余校验都在 C++。"""
    if not isinstance(settings, dict) or _contains_server_only_key(settings):
        raise _fail("invalid_settings")


def _auto_snapshot_of(rec: dict) -> dict:
    # 生效的设置（按看板的打开方式，C++ 判，裁定 R33）：只读打开的世界不拍自动快照，显示为全关。
    auto = _core.world_effective_settings(rec["settings"], record=rec)["background"]["auto_snapshot"]
    return {"on_session": auto["on_session"], "interval_hours": auto["interval_hours"]}


def _snapshot_missing(snap: dict) -> bool:
    # 注册项还在、文件已不在（保留策略清理过的旧文件、手工删除）。
    return not os.path.exists(snap["path"])


def _archive_totals(pool, name: str) -> tuple[int, int]:
    # 回滚归档（<世界 home>/archive/）的个数与字节合计。读不出（archive 目录指到世界目录之外、无权限）时
    # 记 warning、按 0/0 计：不能让一个坏世界拖垮整张列表。
    try:
        listed = pool.archives(name)
    except _core.WorldStoreError as exc:
        logger.warning("world_list_archives failed for %s: %s", name, exc)
        return 0, 0
    return len(listed), sum(item["size_bytes"] for item in listed)


def _text_flag(value: str):
    # 注册表里没有的文本标记（retention_group、fork_of_*）是空串；契约给 null。
    return value or None


def _world_row(pool, rec: dict, open_worlds: set[str]) -> dict:
    """列表的一行。不打开任何库（几百个世界时逐个打开撑不住，spec §5.1）：开着的世界的计数由池现读
    （持一份不建引擎的租约），关着的取注册表缓存列（关闭时与每 30 秒写入）。快照与归档只读注册表与目录。

    第 3 期（spec §6）：seed、read_only、reclaimable、retention_group、fork_of_* 是注册表的原始标记（read_only
    只在只读世界与种子上为真，裁定 R18）；open_mode 是看板实际怎么打开它（C++ world_decide_open_mode：可回收
    世界 read_only 为假、open_mode 却是 "ro"）。schema_behind 只给开着的世界（引擎打开时记下的），关着的为
    null：列表不打开库（裁定 R17），详情另算。"""
    name = rec["name"]
    is_open = name in open_worlds
    counts = {"holders": rec["holders"], "statements": rec["statements"],
              "last_write_at": rec["last_write_at"]}
    if is_open:
        live = pool.live_stats(name)
        if live is not None:
            counts = live
    snaps = pool.snapshots(name)
    archives, archive_bytes = _archive_totals(pool, name)
    return {"name": name, "label": rec["label"], "labels": dict(rec["labels"]),
            "tenant": rec["tenant"], "created_at": rec["created_at"], "open": is_open,
            "holders": counts["holders"], "statements": counts["statements"],
            "last_write_at": counts["last_write_at"],
            "snapshots": len(snaps),
            "snapshot_bytes": sum(s["size_bytes"] for s in snaps if not _snapshot_missing(s)),
            "archives": archives, "archive_bytes": archive_bytes,
            "auto_snapshot": _auto_snapshot_of(rec),
            "seed": rec["seed"], "read_only": rec["read_only"], "reclaimable": rec["reclaimable"],
            "retention_group": _text_flag(rec["retention_group"]),
            "fork_of_world": _text_flag(rec["fork_of_world"]),
            "fork_of_snapshot": _text_flag(rec["fork_of_snapshot"]),
            "open_mode": _core.world_decide_open_mode(rec),
            "schema_behind": pool.schema_behind(name, open_only=True) if is_open else None}


def _snapshot_row(snap: dict) -> dict:
    # 白名单：条目里的 path 是服务器上的绝对路径，不外露。文件缺失的条目照常列出（还能
    # 从界面删掉），size_bytes 记 0，并带 missing: true（只在缺失的行上出现的附加字段）。
    # W4 的 sessions_complete / session_id 在 W4 之前的条目里没有，给 None；content_mark 只给
    # 自动快照的判断用，不在白名单里。digest（裁定 R26）：快照里 library 的摘要，旧条目为 None。
    row = {key: snap[key] for key in ("id", "kind", "reason", "note", "created_at",
                                      "world_time", "size_bytes")}
    row["sessions_complete"] = snap.get("sessions_complete")
    row["session_id"] = snap.get("session_id")
    row["digest"] = snap.get("digest")
    if _snapshot_missing(snap):
        row["size_bytes"] = 0
        row["missing"] = True
    return row


# ───────────────────────── 工作线程里跑的部分 ─────────────────────────

def _label_filter(request: Request) -> dict[str, str]:
    labels = {}
    for key, value in request.query_params.multi_items():
        if key.startswith(_LABEL_PREFIX):
            label = key[len(_LABEL_PREFIX):]
            if not label:
                raise _fail("invalid_labels")
            labels[label] = value
    return labels


def _list_worlds(pool, labels: dict, offset: int, limit: int) -> dict:
    reg = pool.registry
    page = pool.list_worlds(labels, offset, limit)
    open_worlds = set(pool.open_worlds())
    rows = []
    for rec in page["worlds"]:
        try:
            rows.append(_world_row(pool, rec, open_worlds))
        except (WorldSwitchError, _core.WorldStoreError) as exc:
            # 列出之后、建这一行之前被删掉的世界：跳过这一行，不让整张列表 404。别的错照常抛。
            code = exc.code if isinstance(exc, WorldSwitchError) else _core.world_store_error_code(exc)
            if code != "world_not_found":
                raise
    return {"default": pool.default_world(), "total": page["total"],
            "snapshot_retention": reg.snapshot_retention(),
            "auto_snapshot_status": pool.auto_snapshot_status(),
            "worlds": rows}


def _world_detail(pool, providers: dict, name: str) -> dict:
    """详情：列表的一行，加上库的完整统计（只读打开库，持不建引擎的租约）与实际生效的设置（不含密钥；按看板的打开
    方式算，只读打开的世界后台全关，裁定 R33）。
    库存在但读不出时统计置 0 并带 stats_error: true，详情照常给出（坏世界还能从界面删掉）。schema_behind 对
    关着的世界也给：持不建引擎的租约短暂只读打开库读一次（裁定 R17），读不出时 null。"""
    rec = pool.record(name)
    row = _world_row(pool, rec, set(pool.open_worlds()))
    row["schema_behind"] = pool.schema_behind(name)
    try:
        stats = pool.db_stats(name)
    except _core.WorldStoreError as exc:
        logger.warning("world_stats failed for %s: %s", name, exc)
        stats = {"db_bytes": 0, "live_bytes": 0, "engrams": 0}
        row["stats_error"] = True
    row.update(db_bytes=stats["db_bytes"], live_bytes=stats["live_bytes"],
               engrams=stats["engrams"],
               settings=_public_settings(_core.world_effective_settings(rec["settings"], record=rec),
                                         providers))
    return row


def _create_world(pool, body: CreateWorldBody) -> dict:
    auto = body.auto_snapshot.model_dump() if body.auto_snapshot is not None else None
    rec = pool.create_world(body.name, body.label, labels=body.labels, overrides=body.settings,
                            auto_snapshot=auto)
    return _world_row(pool, rec, set(pool.open_worlds()))


def _fork_out(pool, out: dict) -> dict:
    """{"world": 新世界的列表行, "digest"}：记录经列表行的白名单（不外露 db_path、snapshot_dir）。"""
    return {"world": _world_row(pool, out["world"], set(pool.open_worlds())),
            "digest": out["digest"]}


def _fork_world(pool, source: str, body: ForkBody) -> dict:
    out = pool.fork_world(source, body.new_name, snapshot_id=body.snapshot_id, labels=body.labels,
                          retention_group=body.retention_group, reclaimable=body.reclaimable,
                          seed=body.seed, settings=body.settings)
    return _fork_out(pool, out)


def _upgrade_seed(pool, source: str, new_name: str) -> dict:
    return _fork_out(pool, pool.upgrade_seed(source, new_name))


def _update_settings(pool, providers: dict, name: str, overrides: dict) -> dict:
    settings, warnings = pool.update_settings(name, overrides)
    out = _public_settings(settings, providers)
    if warnings:
        out["warnings"] = warnings
    return out


def _reconfirm_endpoints(pool, providers: dict, name: str, config_lock) -> dict:
    """重新确认端点之后回世界详情（设置去掉密钥，与 GET /api/worlds/{name} 同形），重嵌入提示放 warnings。provider 表在
    config_lock 内拷出（与 POST /api/config 互斥，裁定 R59 N-3）。"""
    _settings, warnings = pool.reconfirm_endpoints(name, config_lock=config_lock)
    out = _world_detail(pool, providers, name)
    if warnings:
        out["warnings"] = warnings
    return out


def _set_auto_snapshot(pool, name: str, on_session: bool, interval_hours: int) -> dict:
    return {"world": name, "auto_snapshot": pool.set_auto_snapshot(name, on_session, interval_hours)}


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


def _list_sessions(pool, name: str, limit: int) -> dict:
    # world_list_sessions 只读打开该世界的库：池持一份不建引擎的租约（恢复要等它归还）。
    # source_lane_enabled 取该世界实际生效的设置：它表示「新写入会不会产生会话」。
    listed = pool.list_sessions(name, limit)
    lane = _core.world_effective_settings(pool.record(name)["settings"])["source_lane"]["enabled"]
    return {"world": name, "source_lane_enabled": bool(lane),
            "total": listed["total"], "null_observed": listed["null_observed"],
            "truncated": listed["truncated"],
            "sessions": [_session_row(row) for row in listed["sessions"]]}


def _list_snapshots(pool, name: str) -> dict:
    snaps = pool.snapshots(name)                 # world_not_found
    # 注册表本就是最新在前；显式按 created_at 降序（稳定排序，同秒保持注册表顺序）。
    ordered = sorted(snaps, key=lambda snap: snap["created_at"], reverse=True)
    return {"snapshots": [_snapshot_row(snap) for snap in ordered]}


async def _broadcast(request: Request, kind: str, world: str, payload: dict) -> None:
    ws_manager = getattr(request.app.state, "ws_manager", None)
    if ws_manager is not None:
        await ws_manager.broadcast_world(kind, world, payload)


def build_worlds_router(require_token) -> APIRouter:
    router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])

    @router.get("/worlds")
    async def list_worlds(request: Request, offset: int = 0, limit: int = 50):
        # offset / limit 的范围由 C++ 校验（invalid_world_query → 400）。
        pool = _pool(request)
        _require_registry(pool)
        return await _run(_list_worlds, pool, _label_filter(request), offset, limit)

    @router.post("/worlds")
    async def create_world(request: Request, body: CreateWorldBody):
        pool = _pool(request)
        _require_registry(pool)
        if body.settings is not None:
            _require_client_settings(body.settings)
        return await _run(_create_world, pool, body)

    @router.get("/worlds/{name}")
    async def world_detail(request: Request, name: str):
        pool = _pool(request)
        _require_registry(pool)
        return await _run(_world_detail, pool, request.app.state.config.providers, name)

    @router.delete("/worlds/{name}")
    async def delete_world(request: Request, name: str, confirm: str = ""):
        pool = _pool(request)
        _require_registry(pool)
        if confirm != name:
            raise _fail("confirm_mismatch")      # 不碰池
        await _run(pool.delete_world, name)
        return {"deleted": name}

    @router.post("/worlds/{name}/fork")
    async def fork_world(request: Request, name: str, body: ForkBody):
        # spec §6.1：复制、校验、摘要比较、登记全在 C++；池按来源开没开着选路、持锁（裁定 R5）。新世界不打开。
        pool = _pool(request)
        _require_registry(pool)
        if body.settings is not None:
            _require_client_settings(body.settings)
        return await _run(_fork_world, pool, name, body)

    @router.post("/worlds/{name}/upgrade_seed")
    async def upgrade_seed(request: Request, name: str, body: UpgradeSeedBody):
        # spec §6.2：复制旧种子、在副本上跑迁移、登记成新种子；旧种子原样保留。
        pool = _pool(request)
        _require_registry(pool)
        return await _run(_upgrade_seed, pool, name, body.new_name)

    @router.put("/worlds/{name}/settings")
    async def update_settings(request: Request, name: str, body=Body(...)):
        # body 原样转发（裁定 R35）：部分设置，没给的键不出现，C++ 逐叶合并；不经模型转换，免得把没给的
        # 键补成默认值盖掉已存的设置。
        pool = _pool(request)
        _require_registry(pool)
        _require_client_settings(body)
        return await _run(_update_settings, pool, request.app.state.config.providers, name, body)

    @router.post("/worlds/{name}/settings/reconfirm")
    async def reconfirm_endpoints(request: Request, name: str):
        # 裁定 R53：把各 provider 此刻的端点（默认配置里的地址与种类）复制进这个世界引用它们的角色。默认世界 400
        # invalid_settings（同 PUT 改它的 endpoint）。
        pool = _pool(request)
        _require_registry(pool)
        return await _run(_reconfirm_endpoints, pool, request.app.state.config.providers, name,
                          getattr(request.app.state, "config_lock", None))

    @router.post("/worlds/{name}/close")
    async def close_world(request: Request, name: str):
        # 等该世界的在途请求结束、停线程、丢引擎、写统计缓存（池的 close_world）。没开着时什么都不做。
        pool = _pool(request)
        _require_registry(pool)
        await _run(pool.close_world, name)
        return {"closed": name}

    @router.put("/worlds/{name}/auto_snapshot")
    async def set_auto_snapshot(request: Request, name: str, body: AutoSnapshotBody):
        pool = _pool(request)
        _require_registry(pool)
        return await _run(_set_auto_snapshot, pool, name, body.on_session, body.interval_hours)

    @router.get("/worlds/{name}/sessions")
    async def list_sessions(request: Request, name: str,
                            limit: int = Query(200, ge=1, le=1000)):
        # 越界的 limit 在这里回 422（C++ 的 1–1000 校验留作兜底，那条会回 400
        # invalid_auto_snapshot，前端会显示成「自动快照设置无效」）。
        pool = _pool(request)
        _require_registry(pool)
        return await _run(_list_sessions, pool, name, limit)

    @router.get("/worlds/{name}/snapshots")
    async def list_snapshots(request: Request, name: str):
        pool = _pool(request)
        _require_registry(pool)
        return await _run(_list_snapshots, pool, name)

    @router.post("/worlds/{name}/snapshots")
    async def create_snapshot(request: Request, name: str, body: SnapshotBody = SnapshotBody()):
        pool = _pool(request)
        _require_registry(pool)
        snap = await _run(pool.take_snapshot, name, body.note)
        await _broadcast(request, "snapshot_created", name, {"world": name, "id": snap["id"]})
        return _snapshot_row(snap)

    @router.delete("/worlds/{name}/snapshots/{snapshot_id}")
    async def delete_snapshot(request: Request, name: str, snapshot_id: str):
        pool = _pool(request)
        _require_registry(pool)
        await _run(pool.delete_snapshot, name, snapshot_id)
        return {"deleted": snapshot_id}

    @router.post("/worlds/{name}/rollback")
    async def rollback_world(request: Request, name: str, body: RollbackBody):
        pool = _pool(request)
        _require_registry(pool)
        if body.confirm != name:
            raise _fail("confirm_mismatch")              # 不碰池
        if body.mode not in _ROLLBACK_MODES:
            raise _fail("rollback_mode_unsupported")     # 不碰池
        rolled_back = {"world": name, "snapshot_id": body.snapshot_id}
        try:
            out = await _run(pool.rollback, name, body.snapshot_id)
        except HTTPException as exc:
            # 池报告库已替换（C++ 的 replaced 标记，裁定 R32；池在这种失败上已刷新统计、重开过引擎）：库已经换成快照的
            # 内容，别的标签页同样要知道库变了。判定来自标记，不来自错误码列表。
            if getattr(exc.__cause__, "pool_replaced", False):
                await _broadcast(request, "world_rolled_back", name, rolled_back)
            raise
        await _broadcast(request, "world_rolled_back", name, rolled_back)
        return out

    @router.delete("/worlds/{name}/archives")
    async def purge_archives(request: Request, name: str, confirm: str = ""):
        pool = _pool(request)
        _require_registry(pool)
        if confirm != name:
            raise _fail("confirm_mismatch")              # 不碰池
        return {"deleted": await _run(pool.purge_archives, name)}

    return router
