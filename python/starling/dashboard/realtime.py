"""WebSocket connection manager — broadcasts engine events to dashboards.

世界相关的消息一律带世界名（记忆服务第 1 期 Task 6）：{"type": ..., "world": <世界名>, "payload": ...}。
看板按 world 字段只处理当前页面选中的世界。消息形状只在 world_event 这一处定义。
"""
from __future__ import annotations

import asyncio
from typing import Callable

from fastapi import WebSocket

# 单个 socket 一次推送的上限。命令路由在引擎租约内广播：一个不再读取的标签页若能让
# send_json 无限挂起，就会拉长每一份租约，关闭或恢复这个世界随之等满上限回 world_busy。
_SEND_TIMEOUT_S = 2.0


def world_event(kind: str, world: str, payload: dict) -> dict:
    """一个世界上发生的事件的广播消息。"""
    return {"type": kind, "world": world, "payload": payload}


class ConnectionManager:
    def __init__(self) -> None:
        self._active: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._active.append(ws)

    def register(self, ws: WebSocket) -> None:
        """Append an already-accepted socket (post-auth handshake path)."""
        self._active.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self._active:
            self._active.remove(ws)

    async def broadcast(self, message: dict) -> None:
        dead = []
        for ws in list(self._active):
            try:
                await asyncio.wait_for(ws.send_json(message), timeout=_SEND_TIMEOUT_S)
            except Exception:   # 含 TimeoutError：推不出去的 socket 一律当死连接摘掉
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def broadcast_world(self, kind: str, world: str, payload: dict) -> None:
        await self.broadcast(world_event(kind, world, payload))

    def bridge(self, loop: asyncio.AbstractEventLoop,
               kind: str) -> Callable[[str, dict], None]:
        """引擎池线程（后台 tick、统计去抖计时器、摄入线程）→ 事件循环的桥：返回 callback(world, payload)，
        把一条 kind 消息交给 loop 去广播，不等它发完（池的线程不能被慢 socket 拖住）。"""
        def callback(world: str, payload: dict) -> None:
            asyncio.run_coroutine_threadsafe(self.broadcast_world(kind, world, payload), loop)
        return callback
