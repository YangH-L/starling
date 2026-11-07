"""检视与命令路由按世界寻址（记忆服务第 1 期 Task 6，spec §13.1）。

每个检视、命令路由挂两次（create_app）：前缀 /api 指向默认世界（旧路径，现有脚本、hook、前端照旧可用），
前缀 /api/w/{world} 指向指定世界。请求落在哪个世界只由 world_name 这一个依赖项决定，租约、只读检视都经它。

世界错误统一由 install_error_handlers 注册的异常处理器转成 {"code", "transient", "detail"}（detail 等于码）（前端按 detail 显示提示）：
引擎池的 WorldSwitchError（取租约时：世界不存在、正在关闭等），以及引擎调用里抛出的 C++ WorldStoreError
（如 endpoint_changed：继承的 provider 地址已改）。码由池或 C++ 给出，这里只查表定 HTTP 状态。

可能建引擎的租约（build=True）一律经 lease_off_loop 在工作线程里取（裁定 R32）：关着的世界第一次打开要取
它的世界锁，而世界锁可能被快照、清理归档占着几秒到几十秒；事件循环上的代码从不等世界锁（见
world_manager 模块文档的锁顺序）。
"""
from __future__ import annotations

import logging
import threading

from anyio import to_thread
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from starling import _core
from starling.dashboard.world_manager import WorldSwitchError

logger = logging.getLogger(__name__)

# 指定世界的前缀；检视与命令路由在 /api 之外再挂到这里。
WORLD_PREFIX = "/api/w/{world}"

# 错误码 → HTTP 状态与瞬态标志，都取自 C++ 错误码目录（spec §13.2）。未登记的码按 500、非瞬态。
_CATALOG = {row["code"]: row for row in _core.error_catalog()}
_STATUS = {code: row["http_status"] for code, row in _CATALOG.items()}


def error_body(code: str) -> dict:
    """统一错误体：detail 仍等于 code（前端 describeError 读 detail），code 与 transient 给程序用。"""
    row = _CATALOG.get(code)
    return {"code": code, "transient": bool(row and row["transient"]), "detail": code}


class WorldHTTPError(HTTPException):
    """带统一错误体的世界错误；由 install_error_handlers 注册的处理器渲染。"""

    def __init__(self, code: str) -> None:
        super().__init__(status_code=_STATUS.get(code, 500), detail=code)
        self.code = code


def world_name(request: Request) -> str:
    """本请求的世界：路径里有 {world} 就用它，否则是默认世界（注册表记的默认世界；没有注册表时
    是 "default"，裁定 R6）。世界存不存在不在这里查，由取租约时的池判（world_not_found）。"""
    world = request.path_params.get("world")
    if world:
        return world
    return request.app.state.pool.default_world()


class _PendingLease:
    """工作线程取到的租约交给等它的协程。协程被取消（原生的 task.cancel 可能在线程还在跑时就打断等待）时
    置 abandoned：线程之后才拿到的租约当场归还，已经交过来的由 abandon 归还。两边在同一把锁下交接，
    所以一份租约要么被协程拿走、要么被归还，不会悬空（悬空的租约会让关闭、恢复这个世界等满超时）。"""

    __slots__ = ("_lock", "_lease", "_abandoned")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lease = None
        self._abandoned = False

    def acquire(self, pool, world: str, build: bool) -> None:
        lease = pool.lease(world, build=build)
        with self._lock:
            if not self._abandoned:
                self._lease = lease
                return
        lease.release()

    def abandon(self) -> None:
        with self._lock:
            self._abandoned = True
            lease, self._lease = self._lease, None
        if lease is not None:
            lease.release()

    def take(self):
        with self._lock:
            lease, self._lease = self._lease, None
        return lease


async def lease_off_loop(pool, world: str, *, build: bool = True):
    """在工作线程里取 world 的一份租约，返回 EngineLease；池的错误（WorldSwitchError）原样抛出。调用方拿到
    之后立刻进 try/finally 归还：从这里返回到调用方的下一行之间没有检查点。"""
    pending = _PendingLease()
    try:
        await to_thread.run_sync(pending.acquire, pool, world, build)
    except BaseException:
        pending.abandon()
        raise
    return pending.take()


def status_for(code: str) -> int:
    return _STATUS.get(code, 500)


def install_error_handlers(app: FastAPI) -> None:
    """把两类世界错误转成 {"detail": 码}。detail 只给码：C++ 的错误文本里有服务器路径。"""

    async def _switch_error(_request: Request, exc: WorldSwitchError) -> JSONResponse:
        status = status_for(exc.code)
        if status >= 500:
            logger.warning("world error %s: %s", exc.code, exc.detail)
        return JSONResponse(error_body(exc.code), status_code=status)

    async def _store_error(_request: Request, exc: Exception) -> JSONResponse:
        code = _core.world_store_error_code(exc)
        status = status_for(code)
        if status >= 500:
            logger.warning("world store error %s: %s", code, exc)
        return JSONResponse(error_body(code), status_code=status)

    async def _world_http_error(_request: Request, exc: WorldHTTPError) -> JSONResponse:
        return JSONResponse(error_body(exc.code), status_code=exc.status_code)

    app.add_exception_handler(WorldHTTPError, _world_http_error)
    app.add_exception_handler(WorldSwitchError, _switch_error)
    app.add_exception_handler(_core.WorldStoreError, _store_error)
