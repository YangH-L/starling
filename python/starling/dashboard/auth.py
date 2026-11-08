"""Bearer-token auth — constant-time compare; token from config (env-only)."""
from __future__ import annotations

import hmac

from fastapi import Header

from starling.dashboard.routes.world_scope import WorldHTTPError

# 401 的 detail 沿用原来的文字：前端按状态码判 401（ApiError.unauthorized），toast 显示的是 detail（最后修复波 A5）。
_UNAUTHORIZED_DETAIL = "invalid or missing bearer token"


def make_require_token(token: str):
    """Build a FastAPI dependency that enforces `Authorization: Bearer <token>`.

    When `token` is empty the gate is open (loopback dev). Comparison is
    constant-time; the token is never echoed in errors or logs. A failure is
    401 with the catalog body {"code": "unauthorized", "transient": false,
    "detail": ...} (spec §13.1; 最后修复波 A5，裁定 R91(5)).
    """
    async def require_token(authorization: str = Header(default="")) -> None:
        if not token:
            return
        prefix = "Bearer "
        supplied = authorization[len(prefix):] if authorization.startswith(prefix) else ""
        if not supplied or not hmac.compare_digest(supplied, token):
            raise WorldHTTPError("unauthorized", _UNAUTHORIZED_DETAIL)

    return require_token
