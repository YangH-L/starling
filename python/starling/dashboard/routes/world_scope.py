"""检视与命令路由按世界寻址（记忆服务第 1 期 Task 6，spec §13.1）。

每个检视、命令路由挂两次（create_app）：前缀 /api 指向默认世界（旧路径，现有脚本、hook、前端照旧可用），
前缀 /api/w/{world} 指向指定世界。请求落在哪个世界只由 world_name 这一个依赖项决定，租约、只读检视都经它。

世界错误统一由 install_error_handlers 注册的异常处理器转成 {"detail": "<码>"}（前端按 detail 显示提示）：
引擎池的 WorldSwitchError（取租约时：世界不存在、正在关闭等），以及引擎调用里抛出的 C++ WorldStoreError
（如 endpoint_changed：继承的 provider 地址已改）。码由池或 C++ 给出，这里只查表定 HTTP 状态。
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from starling import _core
from starling.dashboard.world_manager import WorldSwitchError

logger = logging.getLogger(__name__)

# 指定世界的前缀；检视与命令路由在 /api 之外再挂到这里。
WORLD_PREFIX = "/api/w/{world}"

# 错误码 → HTTP 状态。未登记的码按 500。
_STATUS = {
    "world_not_found": 404, "invalid_world_name": 400,
    "world_busy": 503, "worlds_unavailable": 503,
    "endpoint_changed": 409, "world_protected": 409, "world_tenant_mismatch": 409,
    "world_open_failed": 500, "rollback_reopen_failed": 500,
}


def world_name(request: Request) -> str:
    """本请求的世界：路径里有 {world} 就用它，否则是默认世界（注册表记的默认世界；没有注册表时
    是 "default"，裁定 R6）。世界存不存在不在这里查，由取租约时的池判（world_not_found）。"""
    world = request.path_params.get("world")
    if world:
        return world
    return request.app.state.pool.default_world()


def status_for(code: str) -> int:
    return _STATUS.get(code, 500)


def install_error_handlers(app: FastAPI) -> None:
    """把两类世界错误转成 {"detail": 码}。detail 只给码：C++ 的错误文本里有服务器路径。"""

    async def _switch_error(_request: Request, exc: WorldSwitchError) -> JSONResponse:
        status = status_for(exc.code)
        if status >= 500:
            logger.warning("world error %s: %s", exc.code, exc.detail)
        return JSONResponse({"detail": exc.code}, status_code=status)

    async def _store_error(_request: Request, exc: Exception) -> JSONResponse:
        code = _core.world_store_error_code(exc)
        status = status_for(code)
        if status >= 500:
            logger.warning("world store error %s: %s", code, exc)
        return JSONResponse({"detail": code}, status_code=status)

    app.add_exception_handler(WorldSwitchError, _switch_error)
    app.add_exception_handler(_core.WorldStoreError, _store_error)
