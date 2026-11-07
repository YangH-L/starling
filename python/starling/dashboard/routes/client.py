"""/api/v1/client：客户端会话、句柄、门面方法与管理操作（记忆服务第 4 期 Task 4.5，spec §11、§13.1–§13.3）。

全部 POST，body 是 JSON，鉴权沿用看板的 Bearer token。会话、句柄、写占用、在途调用的状态机在 C++（_core.SessionTable，
app.state.sessions）；门面方法经该世界的 DashboardEngine（MemoryCore）按句柄的 holder 调（裁定 R76）；管理操作直接调池。
这里只做形状：校验 body、把调用放进工作线程、把结果转成 JSON、把领域异常转成错误码。

门面调用的形状（裁定 R62、R63）：resolve → 取租约 → call_enter → 调引擎 → call_exit（finally）→ 还租约（finally）。整段在同一个
工作线程函数里跑：请求被取消时线程照样跑完、在自己的 finally 里退出调用、还租约，所以租约覆盖整个调用（恢复、关闭排干租约时
也就等到了在途调用），不会出现「协程已经还了租约、线程还在写库」。开句柄（admit_client_handle）在工作线程里调，不持租约。

错误体一律 {"code", "transient", "detail"}（world_scope.error_body，码与状态取自 C++ 错误码目录）。detail 只给码，不带
端点地址与密钥（spec §13.1）。body 形状不对、未知方法、未知管理操作回 400 invalid_request。
"""
from __future__ import annotations

import dataclasses
import logging
from functools import partial
from typing import Literal

from anyio import to_thread
from fastapi import APIRouter, Body, Depends, FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from starling import _core
from starling._memory_core import LLMNotConfigured, RecallDegradedError
from starling.dashboard.routes.world_scope import WorldHTTPError, error_body, status_for
from starling.dashboard.world_manager import WorldSwitchError
from starling.extractor.config import ExtractionConfig

logger = logging.getLogger(__name__)

PREFIX = "/api/v1/client"
# 客户端协议版本（spec §13.4）：与 starling_client._transport.PROTOCOL 不同就在 connect 时回 protocol_mismatch。
PROTOCOL = 1
# 句柄 close 不给 timeout_s 时等在途调用的上限（秒）。
_CLOSE_TIMEOUT_S = 30.0
# 世界设置里的 endpoint 段：info.default_settings 去掉地址（base_url）。
_ENDPOINT_SECTIONS = ("extraction", "embedding", "chat", "consolidation")
# StatementRow 的全部字段（召回的 row 整行序列化，裁定 R77），取自绑定本身，字段增减不用改这里。
_ROW_FIELDS = tuple(name for name, attr in vars(_core.StatementRow).items()
                    if isinstance(attr, property))


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _fail(code: str) -> WorldHTTPError:
    return WorldHTTPError(code)


# ───────────────────────── 请求体 ─────────────────────────

class ConnectBody(_Body):
    protocol: int


class SessionBody(_Body):
    session_id: str


class OpenBody(_Body):
    session_id: str
    world: str
    holder: str = Field(min_length=1)          # 修复轮 1 F3：空 holder 是 0039 无主旧行的标记，不能当 holder 用
    read_only: bool = False
    settings: dict | None = None


class CloseBody(_Body):
    handle_id: str
    timeout_s: float | None = None


class CallBody(_Body):
    handle_id: str
    method: str
    args: dict = {}


# 门面方法的参数（缺省同 spec §13.3 的客户端签名）。
class _RecallArgs(_Body):
    query: str
    k: int = 10
    mode: Literal["semantic", "completion"] = "semantic"
    strict: bool = True
    include_volatile: bool = True

    @model_validator(mode="after")
    def _completion_has_no_degraded_signal(self):
        # MemoryCore.recall 对这两个组合抛 ValueError：在调引擎之前拦成 invalid_request（F2：引擎里的 ValueError 是 500）。
        if self.mode == "completion" and (self.strict or self.include_volatile):
            raise ValueError("strict / include_volatile need mode='semantic'")
        return self


class _RememberEventArgs(_Body):
    key: str
    text: str
    interlocutor: str
    epoch: str = "2026-01-01T00:00:00Z"


class _KeyArgs(_Body):
    key: str


class _ContentIdsArgs(_Body):
    ids: list[str]


class _NoArgs(_Body):
    pass


class _EmbedPendingArgs(_Body):
    now: str


# 管理操作的参数（spec §13.3）。
class _CreateWorldArgs(_Body):
    name: str
    labels: dict[str, str] | None = None
    settings: dict | None = None
    seed: bool = False


class _ForkArgs(_Body):
    source: str
    name: str
    snapshot_id: str | None = None
    labels: dict[str, str] | None = None
    retention_group: str | None = None
    reclaimable: bool = False
    seed: bool = False
    settings: dict | None = None


class _UpgradeSeedArgs(_Body):
    source: str
    name: str


class _WorldArgs(_Body):
    world: str


class _SnapshotArgs(_Body):
    world: str
    note: str = ""


class _SnapshotIdArgs(_Body):
    world: str
    snapshot_id: str


class _ListWorldsArgs(_Body):
    labels: dict[str, str] | None = None
    page: int = 1
    page_size: int = 50
    with_digest: bool = False


def _parse(model: type[_Body], data) -> _Body:
    try:
        return model.model_validate(data if data is not None else {})
    except ValidationError:
        raise _fail("invalid_request") from None


# ───────────────────────── 门面方法（工作线程里跑） ─────────────────────────

def _row_dict(row) -> dict:
    return {name: getattr(row, name) for name in _ROW_FIELDS}


def _recall(engine, holder: str, args: _RecallArgs):
    hits = engine.recall(args.query, k=args.k, mode=args.mode, holder=holder, strict=args.strict,
                         include_volatile=args.include_volatile)
    return [{"content_id": hit["content_id"], "score": hit["score"], "row": _row_dict(hit["row"])}
            for hit in hits]


def _remember_event(engine, holder: str, args: _RememberEventArgs):
    return engine.remember_event(args.key, args.text, holder=holder, interlocutor=args.interlocutor,
                                 epoch=args.epoch)


def _abandon_event(engine, holder: str, args: _KeyArgs):
    engine.abandon_event(args.key, holder=holder)


# 方法名 → (参数模型, 写不写, 实现)。写入类方法要求句柄可写（resolve 的 read_only 为 True 时 world_read_only）。
_METHODS = {
    "recall": (_RecallArgs, False, _recall),
    "remember_event": (_RememberEventArgs, True, _remember_event),
    "abandon_event": (_KeyArgs, True, _abandon_event),
    "content_ids": (_ContentIdsArgs, False, lambda engine, _holder, args: engine.content_ids(args.ids)),
    "digest": (_NoArgs, False, lambda engine, _holder, _args: engine.digest()),
    "pending_events": (_NoArgs, False, lambda engine, _holder, _args: engine.pending_events()),
    "next_seq": (_NoArgs, False, lambda engine, _holder, _args: engine.next_seq()),
    "embed_pending": (_EmbedPendingArgs, True, lambda engine, _holder, args: engine.embed_pending(args.now)),
}


def _engine_error_code(method: str, exc: Exception) -> str | None:
    """引擎调用里抛出的非池、非 C++ 世界错误 → 码；None 表示不认识（按 internal_error 处理）。

    KeyError 只在 abandon_event 上是 event_not_found（C++ 的「这个 holder 没有这个 key」，绑定译成 KeyError）；别的方法里
    的 KeyError、ValueError 是服务端的 bug，回 internal_error（修复轮 1 F2）。参数在调引擎之前已经过模型校验，所以这里不会把
    请求错误误判成 500；唯一的例外是 recall 的 strict=True 配 mode="completion"（C++ 之外、引擎之前的组合校验）。"""
    if isinstance(exc, RecallDegradedError):
        return "recall_degraded"
    if isinstance(exc, _core.EventConflict):
        return "event_conflict"
    if isinstance(exc, KeyError):
        return "event_not_found" if method == "abandon_event" else None
    if isinstance(exc, LLMNotConfigured):
        return "llm_not_configured"
    if isinstance(exc, _core.WriteGateRejected):
        return "world_busy"                     # 关停排水：写门已关，瞬态
    return None


def _facade_call(pool, sessions, handle_id: str, method: str, raw_args) -> object:
    spec = _METHODS.get(method)
    if spec is None:
        raise _fail("invalid_request")
    model, writes, run = spec
    args = _parse(model, raw_args)
    info = sessions.resolve(handle_id, pool.now_ms())            # handle_closed / session_expired
    if writes and info.read_only:
        raise _fail("world_read_only")
    lease = pool.lease(info.world)                               # 整个调用期间都持着租约（R63）
    try:
        sessions.call_enter(handle_id, pool.now_ms())
        try:
            engine = lease.engine
            try:
                return run(engine, info.holder, args)
            except (WorldSwitchError, _core.WorldStoreError, WorldHTTPError):
                raise
            except Exception as exc:  # noqa: BLE001 — 领域异常转码，其余记日志后回 internal_error
                code = _engine_error_code(method, exc)
                if code is None:
                    logger.exception("client call %s failed", method)
                    code = "internal_error"
                raise _fail(code) from None
            finally:
                engine = None
        finally:
            sessions.call_exit(handle_id, pool.now_ms())         # 裁定 R62：一定退出
    finally:
        lease.release()


# ───────────────────────── 会话与句柄（工作线程里跑） ─────────────────────────

def _public_default_settings(pool) -> dict:
    """默认世界存着的设置（新世界从它复制，C++ 补齐默认值），去掉地址：每个 endpoint 段删掉 base_url。设置里本来就没有密钥
    （只有 provider_name，spec §8.2）。"""
    settings = _core.world_effective_settings(pool.record(pool.default_world())["settings"])
    for section in _ENDPOINT_SECTIONS:
        endpoint = settings.get(section)
        if isinstance(endpoint, dict):
            endpoint.pop("base_url", None)
    return settings


def _info(pool) -> dict:
    """{version, commit, protocol, extraction_config, default_settings}（spec §13.4）。version 与 commit 编在 C++ 里（裁定 R88）。
    extraction_config 是引擎生效的 ExtractionConfig：池建的每个引擎都用缺省构造（DashboardEngine 不传 extraction）。"""
    extraction = dataclasses.asdict(ExtractionConfig())
    extraction["extra_core_predicates"] = list(extraction["extra_core_predicates"])
    return {"version": _core.__version__, "commit": _core.build_commit(), "protocol": PROTOCOL,
            "extraction_config": extraction, "default_settings": _public_default_settings(pool)}


def _open(pool, body: OpenBody) -> dict:
    handle = pool.admit_client_handle(body.world, body.session_id, body.holder, body.read_only,
                                      pool.now_ms(), settings=body.settings)
    return {"handle_id": handle}


# ───────────────────────── 管理操作（工作线程里跑） ─────────────────────────

def _flag(value: str):
    return value or None


def _status(name: str, open_worlds: set[str], closing: set[str]) -> str:
    if name in closing:
        return "closing"
    return "open" if name in open_worlds else "closed"


def _world_item(pool, rec: dict, open_worlds: set[str], closing: set[str]) -> dict:
    """list_worlds 的一项（spec §13.3）：名字、标签、seed、read_only、来源、holder 数、语句数、最后写入时间、状态。开着的世界的
    计数由池现读（不算活动），关着的取注册表缓存列。不含路径。"""
    name = rec["name"]
    counts = {"holders": rec["holders"], "statements": rec["statements"],
              "last_write_at": rec["last_write_at"]}
    if name in open_worlds:
        live = pool.live_stats(name)
        if live is not None:
            counts = live
    return {"name": name, "label": rec["label"], "labels": dict(rec["labels"]),
            "created_at": rec["created_at"], "seed": rec["seed"], "read_only": rec["read_only"],
            "reclaimable": rec["reclaimable"], "retention_group": _flag(rec["retention_group"]),
            "fork_of_world": _flag(rec["fork_of_world"]),
            "fork_of_snapshot": _flag(rec["fork_of_snapshot"]),
            "holders": counts["holders"], "statements": counts["statements"],
            "last_write_at": counts["last_write_at"],
            "status": _status(name, open_worlds, closing)}


def _item_now(pool, rec: dict) -> dict:
    return _world_item(pool, rec, set(pool.open_worlds()), set(pool.closing_worlds()))


def _fork_out(pool, out: dict) -> dict:
    return {"world": _item_now(pool, out["world"]), "digest": out["digest"]}


def _admin_create_world(pool, args: _CreateWorldArgs) -> dict:
    return _item_now(pool, pool.create_world(args.name, "", labels=args.labels,
                                             overrides=args.settings, seed=args.seed))


def _admin_fork(pool, args: _ForkArgs) -> dict:
    return _fork_out(pool, pool.fork_world(
        args.source, args.name, snapshot_id=args.snapshot_id, labels=args.labels,
        retention_group=args.retention_group, reclaimable=args.reclaimable, seed=args.seed,
        settings=args.settings))


def _admin_upgrade_seed(pool, args: _UpgradeSeedArgs) -> dict:
    return _fork_out(pool, pool.upgrade_seed(args.source, args.name))


def _admin_snapshot(pool, args: _SnapshotArgs) -> dict:
    snap = pool.take_snapshot(args.world, args.note, kind="held")      # spec §7.1：客户端拍的快照由它持有
    return {"snapshot_id": snap["id"], "digest": snap["digest"]}


def _admin_restore(pool, args: _SnapshotIdArgs) -> dict:
    return {"digest": pool.rollback(args.world, args.snapshot_id)["digest"]}


def _admin_delete_snapshot(pool, args: _SnapshotIdArgs) -> dict:
    pool.delete_snapshot(args.world, args.snapshot_id)
    return {}


def _with_engine(pool, world: str, fn):
    """不开句柄的世界级读（digest、pending_events）：持一份 build=True 的租约（世界没开着就打开它）调引擎。"""
    with pool.lease(world) as engine:
        try:
            return fn(engine)
        finally:
            engine = None


def _admin_digest(pool, args: _WorldArgs) -> dict:
    return {"digest": _with_engine(pool, args.world, lambda engine: engine.digest())}


def _admin_pending_events(pool, args: _WorldArgs) -> dict:
    return {"events": _with_engine(pool, args.world, lambda engine: engine.pending_events())}


def _admin_close_world(pool, args: _WorldArgs) -> dict:
    pool.close_world(args.world)
    return {}


def _admin_list_worlds(pool, args: _ListWorldsArgs) -> dict:
    """一页世界（注册表 RegistryDb::list：标签全部匹配、排序与分页范围由 C++ 定，越界 invalid_world_query）。page 从 1 起。
    with_digest=True 时逐个加 digest（pool.world_digest：开着的走引擎，关着的只读打开，不改文件）。列出之后被删掉的世界跳过。"""
    page = pool.list_worlds(args.labels, (args.page - 1) * args.page_size, args.page_size)
    open_worlds, closing = set(pool.open_worlds()), set(pool.closing_worlds())
    rows = []
    for rec in page["worlds"]:
        try:
            item = _world_item(pool, rec, open_worlds, closing)
            if args.with_digest:
                item["digest"] = pool.world_digest(rec["name"])
        except (WorldSwitchError, _core.WorldStoreError) as exc:
            code = exc.code if isinstance(exc, WorldSwitchError) else _core.world_store_error_code(exc)
            if code != "world_not_found":
                raise
            continue
        rows.append(item)
    return {"worlds": rows, "total": page["total"], "page": args.page, "page_size": args.page_size}


_ADMIN = {
    "create_world": (_CreateWorldArgs, _admin_create_world),
    "fork": (_ForkArgs, _admin_fork),
    "upgrade_seed": (_UpgradeSeedArgs, _admin_upgrade_seed),
    "snapshot": (_SnapshotArgs, _admin_snapshot),
    "restore": (_SnapshotIdArgs, _admin_restore),
    "delete_snapshot": (_SnapshotIdArgs, _admin_delete_snapshot),
    "digest": (_WorldArgs, _admin_digest),
    "pending_events": (_WorldArgs, _admin_pending_events),
    "close_world": (_WorldArgs, _admin_close_world),
    "list_worlds": (_ListWorldsArgs, _admin_list_worlds),
}


# ───────────────────────── 路由 ─────────────────────────

async def _off_loop(fn, *args):
    """在工作线程里跑阻塞的部分（取世界锁、建引擎、读库、等在途调用）。池与 C++ 的世界错误原样抛出，由 world_scope 注册的
    处理器转成错误体；其余异常记日志、回 500 internal_error（异常文本可能带服务器路径，不回给客户端）。"""
    try:
        return await to_thread.run_sync(partial(fn, *args))
    except (WorldHTTPError, WorldSwitchError, _core.WorldStoreError):
        raise
    except Exception:  # noqa: BLE001
        logger.exception("client route failed")
        raise _fail("internal_error") from None


def _pool(request: Request):
    return request.app.state.pool


def _sessions(request: Request):
    sessions = request.app.state.sessions
    if sessions is None:
        raise _fail("worlds_unavailable")
    return sessions


async def _broadcast(request: Request, kind: str, world: str, payload: dict) -> None:
    ws_manager = getattr(request.app.state, "ws_manager", None)
    if ws_manager is not None:
        await ws_manager.broadcast_world(kind, world, payload)


def build_client_router(require_token) -> APIRouter:
    router = APIRouter(prefix=PREFIX, dependencies=[Depends(require_token)])

    @router.post("/connect")
    async def connect(request: Request, body: ConnectBody):
        pool, sessions = _pool(request), _sessions(request)
        if body.protocol != PROTOCOL:
            raise _fail("protocol_mismatch")
        info = await _off_loop(_info, pool)
        session_id = sessions.connect(pool.now_ms())
        return {"session_id": session_id, "lease_ms": sessions.lease_ms,
                "heartbeat_ms": sessions.heartbeat_ms, "info": info}

    @router.post("/heartbeat")
    async def heartbeat(request: Request, body: SessionBody):
        pool, sessions = _pool(request), _sessions(request)
        sessions.heartbeat(body.session_id, pool.now_ms())
        return {}

    @router.post("/disconnect")
    async def disconnect(request: Request, body: SessionBody):
        _sessions(request).disconnect(body.session_id)
        return {}

    @router.post("/info")
    async def info(request: Request):
        return await _off_loop(_info, _pool(request))

    @router.post("/open")
    async def open_handle(request: Request, body: OpenBody):
        _sessions(request)
        return await _off_loop(_open, _pool(request), body)

    @router.post("/close")
    async def close_handle(request: Request, body: CloseBody):
        _sessions(request)
        timeout = _CLOSE_TIMEOUT_S if body.timeout_s is None else body.timeout_s
        await _off_loop(_pool(request).close_client_handle, body.handle_id, timeout)
        return {}

    @router.post("/call")
    async def call(request: Request, body: CallBody):
        pool, sessions = _pool(request), _sessions(request)
        result = await _off_loop(_facade_call, pool, sessions, body.handle_id, body.method,
                                 body.args)
        return {"result": result}

    @router.post("/admin/{op}")
    async def admin(request: Request, op: str, body=Body(default=None)):
        spec = _ADMIN.get(op)
        if spec is None:
            raise _fail("invalid_request")
        model, run = spec
        args = _parse(model, body)
        pool = _pool(request)
        if pool.registry is None:
            raise _fail("worlds_unavailable")
        try:
            out = await _off_loop(run, pool, args)
        except Exception as exc:
            # 恢复：库已经换成快照内容（池在异常上挂了 pool_replaced，裁定 R32）时照样广播。
            if op == "restore" and getattr(exc, "pool_replaced", False):
                await _broadcast(request, "world_rolled_back", args.world,
                                 {"world": args.world, "snapshot_id": args.snapshot_id})
            raise
        if op == "snapshot":
            await _broadcast(request, "snapshot_created", args.world,
                             {"world": args.world, "id": out["snapshot_id"]})
        elif op == "restore":
            await _broadcast(request, "world_rolled_back", args.world,
                             {"world": args.world, "snapshot_id": args.snapshot_id})
        return out

    return router


def install_client_error_handlers(app: FastAPI) -> None:
    """/api/v1/client 下 body 形状不对回 400 invalid_request（统一错误体）；别的路径照旧是 FastAPI 的 422。"""

    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        if request.url.path.startswith(PREFIX + "/"):
            return JSONResponse(error_body("invalid_request"), status_code=status_for("invalid_request"))
        return await request_validation_exception_handler(request, exc)

    app.add_exception_handler(RequestValidationError, _validation)
