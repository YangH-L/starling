"""FastAPI app factory for the Starling dashboard engine-API."""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import os
import threading
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

from anyio import to_thread
from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from starling import _core
from starling.dashboard.auth import make_require_token
from starling.dashboard.config import DashboardConfig
from starling.dashboard.routes.world_scope import lease_off_loop
from starling.dashboard.world_manager import EngineFactory, WorldPool, WorldSwitchError

logger = logging.getLogger(__name__)


def _ws_origin_allowed(origin: str, cors_origins: list[str]) -> bool:
    """CSWSH guard. Browsers always send Origin on WS; non-browser clients omit it.

    - no Origin  -> allow (CLI / server-side client, not a CSWSH vector)
    - allowlist set -> Origin must be in it
    - no allowlist (loopback dev) -> allow only loopback-origin browsers
    """
    if not origin:
        return True
    if cors_origins:
        return origin in cors_origins
    host = urllib.parse.urlparse(origin).hostname
    return host in ("127.0.0.1", "localhost", "::1")


def _require_engine_matches(engine: object, ctx) -> None:
    """注入的引擎必须打开的就是默认世界的库、用的就是默认世界的租户（ctx = pool.context(默认世界)）。
    默认世界的库路径与租户以注册表为准：注册表早已建好时，它记的默认世界可能与 config 里的不同（见
    create_app 的 warning）。按 config 建好的引擎若照收，检视路由（读注册表里的库）和命令路由（写这个
    引擎）从第一个请求起就指着两个库。

    比引擎实际打开的库（_db_path）和实际使用的租户（_core.tenant），不只比 _cfg。测试替身可能只实现
    部分接口，缺的字段不比。"""
    core = getattr(engine, "_core", None)
    engine_cfg = getattr(engine, "_cfg", None)
    opened = [getattr(engine, "_db_path", None), getattr(engine_cfg, "db_path", None)]
    tenants = [getattr(core, "tenant", None), getattr(engine_cfg, "tenant", None)]
    for path in filter(None, opened):
        if os.path.realpath(path) != os.path.realpath(ctx.db_path):
            raise ValueError(f"injected engine opened {path!r}, but the default world's "
                             f"database is {ctx.db_path!r}")
    for tenant in filter(None, tenants):
        if tenant != ctx.tenant:
            raise ValueError(f"injected engine uses tenant {tenant!r}, but the default "
                             f"world's tenant is {ctx.tenant!r}")


def _open_pool(config: DashboardConfig, engine_factory) -> WorldPool:
    """打开注册表（首次启动时由 C++ 导入旧 worlds.json，裁定 R14）并建引擎池，把 config 同步成默认世界
    的设置（裁定 R27）。库路径没有父目录（:memory: 等）时 data_dir 为空：没有注册表，池只服务默认世界
    （裁定 R6）。"""
    registry = None
    if config.data_dir:
        registry = _core.WorldRegistry.open(config.data_dir, config.db_path, config.tenant)
    pool = WorldPool(registry, config, engine_factory=engine_factory)
    if registry is not None:
        pool.sync_default_settings()
        default = pool.context(pool.default_world())
        if os.path.realpath(default.db_path) != os.path.realpath(config.db_path) \
                or default.tenant != config.tenant:
            # 注册表是默认世界库路径与租户的权威来源（例如 STARLING_DASH_DB 指到了别处）。
            logger.warning("worlds registry in %s overrides config: default world uses %s "
                           "(tenant %s), config says %s (tenant %s)", config.data_dir,
                           default.db_path, default.tenant, config.db_path, config.tenant)
    return pool


class _NotReadyGate:
    """就绪之前（spec §10.1：注册表打开、默认世界引擎建好之前）所有 HTTP 请求回 503 worlds_unavailable（瞬态，码表里
    已有），WebSocket 握手直接拒绝。就绪之后原样放行。纯 ASGI 中间件：lifespan 消息不经这里拦。"""

    def __init__(self, app, *, state) -> None:
        self.app = app
        self._state = state

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket") or self._state.ready:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1013})
            return
        from starling.dashboard.routes.world_scope import error_body, status_for
        await JSONResponse(error_body("worlds_unavailable"),
                           status_code=status_for("worlds_unavailable"))(scope, receive, send)


def _start_pool(pool: WorldPool, mgr, loop) -> None:
    """池起来之后、服务请求之前的那一段（工作线程里调用）。池的线程（后台 tick、统计去抖计时器）→ 事件循环的桥：tick 的
    消息形状与手动 /api/tick 一致，前端无需区分自动/手动；world_changed 每个世界每秒至多一条（池里去抖）。

    默认世界的引擎在此即建，不论 tick_interval_s（裁定 R33，与改之前的生产启动一致）：库与 schema 从启动起就在（检视路由
    只读打开库），摄入从启动起就在收本机 hook 投递的会话。只取一下租约，不留引擎的引用。然后每个开着的世界按自己的
    settings.background 起线程（tick 只在 tick_interval_s > 0 时起，摄入按 background.ingest），此后新打开的世界也起；
    再起按时间的自动快照调度与 30 秒统计刷新（有注册表时）。"""
    pool.on_tick = mgr.bridge(loop, "tick")
    pool.on_world_changed = mgr.bridge(loop, "world_changed")
    pool.lease(pool.default_world()).release()
    pool.start_background()


def _stop_pool(pool: WorldPool) -> None:
    """D-P2-6 关停顺序：先让每个开着的世界进 DRAINING（写门转为拒绝；在 lifespan 的 yield 之后做，不用信号处理器，后者与
    uvicorn 自己的关停竞争），再停摄入（在途的 remember 不再被取走），再停 tick（stop_background 对每个世界 quiesce：先摄入、
    后 tick），最后关掉全部世界、释放 C++ 写连接。事件循环随 lifespan 结束，池的线程不能再往它投广播。"""
    pool.begin_drain()
    pool.stop_background()
    pool.close_all()
    pool.on_tick = None
    pool.on_world_changed = None


def create_app(config: DashboardConfig, *, engine: object | None = None,
               engine_factory: EngineFactory | None = None, deferred_open: bool = False,
               on_ready: Callable[[], None] | None = None,
               on_open_failed: Callable[[BaseException], None] | None = None) -> FastAPI:
    """Build the dashboard app.

    世界由引擎池（app.state.pool，WorldPool）按需打开，每个世界一个引擎；没有「活动世界」。检视与命令
    路由同时挂在 /api（默认世界）与 /api/w/{world}（指定世界）下。

    `engine`（DashboardEngine）：作为默认世界的引擎预先放进池（测试与嵌入式调用方用），必须打开的是默认
    世界的库与租户，否则 ValueError。为 None 时池按需建（lifespan 在 tick_interval_s > 0 时即建）。
    `engine_factory(ctx, providers)`：池建引擎用的工厂，缺省 DashboardEngine(ctx, providers)。

    `deferred_open`（starling-serve，spec §10.1）：注册表与默认世界引擎不在这里开，而在 lifespan 启动后的一个工作线程里开，
    lifespan 本身立刻让出，服务器随即开始接受连接；开好之前 app.state.ready 为 False、app.state.pool 为 None，所有 API（含
    /health）回 503 worlds_unavailable（_NotReadyGate）。开好之后调 on_ready()；开的途中出错调 on_open_failed(exc)（之后照旧
    一律 503）。不能与 engine 同用。缺省 False：与之前一样在这里开池，app.state.ready 从一开始就是 True。
    """
    config.validate_bind()
    # 左脑 lane 的开关一致性在启动时就拒绝，而不是等到第一次摄入才发现。
    config.validate_source_lane()
    if deferred_open and engine is not None:
        raise ValueError("deferred_open cannot take an injected engine")
    pool = None
    if not deferred_open:
        pool = _open_pool(config, engine_factory)
        if engine is not None:
            default = pool.default_world()
            _require_engine_matches(engine, pool.context(default))
            pool.seed_engine(default, engine)

    def _open_deferred(app: FastAPI, loop) -> None:
        """工作线程里：开注册表与池、建默认世界引擎、起后台线程，然后才发布池、置 ready。"""
        opened = _open_pool(config, engine_factory)
        try:
            _start_pool(opened, app.state.ws_manager, loop)
        except BaseException:
            _stop_pool(opened)
            raise
        app.state.pool = opened
        app.state.ready = True

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # lifespan 只在 ASGI 服务器 / `with TestClient` 下运行；直接构造 TestClient(app) 的测试不建引擎、不起线程。
        # 建引擎（首次打开取世界锁）在工作线程里做，同 lease_off_loop。
        loop = asyncio.get_running_loop()
        if not deferred_open:
            await to_thread.run_sync(_start_pool, pool, app.state.ws_manager, loop)
            yield
            _stop_pool(pool)
            return
        opening = asyncio.ensure_future(to_thread.run_sync(_open_deferred, app, loop))

        def _opened(task: asyncio.Future) -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                logger.error("starling service failed to open its worlds: %s: %s",
                             type(exc).__name__, exc)
                if on_open_failed is not None:
                    on_open_failed(exc)
            elif on_ready is not None:
                on_ready()
        opening.add_done_callback(_opened)
        yield
        # 关停：开池的线程还在跑就等它跑完（工作线程取消不了），再按同样的顺序关。
        with contextlib.suppress(BaseException):
            await asyncio.shield(opening)
        opened = app.state.pool
        if opened is not None:
            _stop_pool(opened)

    app = FastAPI(title="Starling Dashboard", version="0.1.0", lifespan=lifespan)
    app.state.config = config
    app.state.pool = pool
    app.state.ready = not deferred_open
    # 写配置的路由（POST /api/config、DELETE /api/config/provider）彼此串行（见 routes/config.py）。线程锁，
    # 只在工作线程里取：事件循环上的锁（asyncio / anyio）不能跨事件循环唤醒等待者，而不进 lifespan 的
    # TestClient 每个请求各起一个事件循环。
    app.state.config_lock = threading.Lock()

    # 先加就绪闸门、再加 CORS：后加的在外层，503 也带上 CORS 头。
    app.add_middleware(_NotReadyGate, state=app.state)
    if config.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    require_token = make_require_token(config.token)

    from starling.dashboard.realtime import ConnectionManager

    app.state.ws_manager = ConnectionManager()

    # 下面的路由函数一律从连接上取 app.state（ws.app / request.app），不闭包捕获 app、config、pool
    # （裁定 R29）：FastAPI 在模块级 lru_cache 里留着每个路由函数，捕获了它们，丢掉的 app 就连同引擎池、
    # 已开的 C++ 写连接一直活着（测试里每个 create_app 都留下一份，gc 越来越慢）。

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        state = ws.app.state
        cfg = state.config
        origin = ws.headers.get("origin", "")
        if not _ws_origin_allowed(origin, cfg.cors_origins):
            await ws.close(code=1008)
            return
        # auth handshake: first text frame must be the token (when configured).
        # Sending the token in-band (not via URL query) avoids leaking it in logs.
        if cfg.token:
            await ws.accept()
            try:
                first = await ws.receive_text()
            except WebSocketDisconnect:
                return
            if not hmac.compare_digest(first, cfg.token):
                await ws.close(code=1008)
                return
            state.ws_manager.register(ws)
        else:
            await state.ws_manager.connect(ws)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            state.ws_manager.disconnect(ws)

    @app.websocket("/ws/converse")
    async def ws_converse(ws: WebSocket) -> None:
        # Streaming converse (#37): a per-turn, per-client token stream. A NEW
        # endpoint, not the broadcast /ws — tokens are request-scoped and must
        # never fan out to every dashboard. Auth mirrors /ws (in-band token as
        # the first text frame). Protocol: client sends one JSON request
        # {message, world?, provider?, holder?, interlocutor?, k?}（world 缺省 = 默认世界）;
        # server replies
        # optionally {"type":"status","status":"grounding"} (once, before any
        # token: the model is writing a hidden evidence block first), then
        # {"type":"token","delta":...} ×N then {"type":"done", ...outcome}, or
        # {"type":"error","error":...}. The reply still persists (remember runs
        # after the stream) — disconnect mid-stream does not abort the turn.
        from starling.dashboard.engine import _LLMNotConfigured

        state = ws.app.state
        cfg, pool = state.config, state.pool
        origin = ws.headers.get("origin", "")
        if not _ws_origin_allowed(origin, cfg.cors_origins):
            await ws.close(code=1008)
            return
        await ws.accept()
        if cfg.token:
            try:
                first = await ws.receive_text()
            except WebSocketDisconnect:
                return
            if not hmac.compare_digest(first, cfg.token):
                await ws.close(code=1008)
                return
        try:
            req = await ws.receive_json()
        except WebSocketDisconnect:
            return
        if not isinstance(req, dict) or not isinstance(req.get("message"), str) \
                or not req["message"]:
            await ws.send_json({"type": "error", "error": "empty_message"})
            await ws.close()
            return

        message = req["message"]
        world = req.get("world")
        if world is not None and not isinstance(world, str):
            await ws.send_json({"type": "error", "error": "invalid_world_name"})
            await ws.close()
            return
        holder = req.get("holder")
        interlocutor = req.get("interlocutor")
        provider = req.get("provider")
        try:
            k = max(1, min(int(req.get("k", 6) or 6), 50))
        except (TypeError, ValueError):
            k = 6

        # 本轮持该世界的一份引擎租约（按轮，不按连接：长连接在两轮之间不挡关闭与恢复）。世界不存在、
        # 正在关闭或恢复时拒绝，给前端一个可识别的错误帧（码同 HTTP 的 detail）。租约在工作线程里取
        # （裁定 R32：首次打开要等世界锁，事件循环不等），在工作线程里、引擎用完的那一刻归还（见
        # run_blocking），协程被取消也不会提前放走仍在跑的这一轮。
        world = world or pool.default_world()          # 缺省或空串：默认世界
        try:
            lease = await lease_off_loop(pool, world)
        except WorldSwitchError as exc:
            await ws.send_json({"type": "error", "error": exc.code})
            await ws.close()
            return
        try:
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue = asyncio.Queue()
            done = object()
            box: dict = {}
        except BaseException:
            lease.release()
            raise

        def on_token(delta: str) -> None:
            # Runs on the C++ worker thread (binding re-acquires the GIL per
            # delta). Only hand the delta to the loop — never touch the socket
            # or DB from here.
            loop.call_soon_threadsafe(queue.put_nowait, {"type": "token", "delta": delta})

        def on_status(status: str) -> None:
            # Same queue as tokens, so call_soon_threadsafe ordering guarantees the
            # status frame reaches the client before the first token of the reply.
            loop.call_soon_threadsafe(queue.put_nowait, {"type": "status", "status": status})

        def run_blocking() -> None:
            engine = lease.engine
            try:
                box["result"] = engine.converse_stream(
                    message, on_token=on_token, on_status=on_status, holder=holder,
                    interlocutor=interlocutor, k=k, provider=provider)
            except Exception as exc:  # surfaced to the client as an error frame
                # 只留错误码：异常的回溯里有引擎的栈帧，留着它就钉住了旧 Runtime。
                if isinstance(exc, _LLMNotConfigured):
                    box["error"] = "llm_not_configured"
                elif isinstance(exc, _core.WorldStoreError):
                    box["error"] = _core.world_store_error_code(exc)   # 如 endpoint_changed
                else:
                    box["error"] = "converse_failed"
            finally:
                del engine
                lease.release()
                loop.call_soon_threadsafe(queue.put_nowait, done)

        try:
            loop.run_in_executor(None, run_blocking)
        except BaseException:
            lease.release()
            raise

        # Drain deltas until the worker signals done. call_soon_threadsafe
        # preserves order, so every delta is dequeued before `done`. If the
        # client vanishes we keep draining (cheap) so converse runs to completion
        # and the turn still persists — we just stop sending.
        disconnected = False
        while True:
            item = await queue.get()
            if item is done:
                break
            if disconnected:
                continue
            try:
                await ws.send_json(item)
            except Exception:  # noqa: BLE001 — client closed; finish server-side quietly
                disconnected = True
        if disconnected:
            return

        if "error" in box:
            await ws.send_json({"type": "error", "error": box["error"]})
            await ws.close()
            return
        result = box.get("result") or {}
        await ws.send_json({"type": "done", **result})
        # Mirror POST /converse: a consolidated turn tells other views to refresh.
        if result.get("statement_ids"):
            await state.ws_manager.broadcast_world(
                "statement_added", world, {"statement_ids": result["statement_ids"]})
        await ws.close()

    @app.get("/health")
    async def health(request: Request) -> dict:
        return {"status": "ok", "version": request.app.version}

    @app.get("/api/ping", dependencies=[Depends(require_token)])
    async def ping() -> dict:
        return {"pong": True}

    from starling.dashboard.routes.inspect import build_inspect_router
    from starling.dashboard.routes.commands import build_commands_router
    from starling.dashboard.routes.evalreport import build_eval_router
    from starling.dashboard.routes.config import build_config_router
    from starling.dashboard.routes.world_scope import WORLD_PREFIX, install_error_handlers
    from starling.dashboard.routes.worlds import build_worlds_router

    install_error_handlers(app)
    # 检视与命令路由各挂两次：/api（旧路径，默认世界）与 /api/w/{world}（spec §13.1）。路由函数经
    # world_scope.world_name 取世界，两处挂载共用同一份处理函数。
    inspect_router = build_inspect_router(require_token)
    commands_router = build_commands_router(require_token)
    for prefix in ("/api", WORLD_PREFIX):
        app.include_router(inspect_router, prefix=prefix)
        app.include_router(commands_router, prefix=prefix)
    app.include_router(build_eval_router(require_token))
    app.include_router(build_config_router(require_token))
    # /api/worlds（世界管理：列表、详情、建删、设置、关闭、快照、恢复），经引擎池；只挂在 /api 下。
    app.include_router(build_worlds_router(require_token))

    _build = Path(__file__).resolve().parents[3] / "dashboard" / "web" / "build"
    if _build.is_dir():
        _build_resolved = _build.resolve()

        @app.get("/{full_path:path}")
        async def spa(full_path: str):
            # SPA fallback: serve a real static file if present, else index.html.
            candidate = (_build / full_path).resolve()
            if full_path and candidate.is_file() and candidate.is_relative_to(_build_resolved):
                return FileResponse(str(candidate))
            return FileResponse(str(_build_resolved / "index.html"))
    return app
