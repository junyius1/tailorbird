"""运行时认证(FastAPI dependency,替代 langgraph_sdk.Auth)。

与 dashboard → runtime 的既有 bearer 契约完全一致(复用
``agent.local_auth.extract_identity``):

- ``Authorization: Bearer <OPEN_SWE_LOCAL_AUTH_TOKEN>:<username>``(复合),或
- 纯 ``Bearer <token>`` + ``X-OpenSWE-User: <username>``。

成功 → 依赖返回 username 字符串;失败 → 401 ``{"detail": "Invalid bearer token"}``。
豁免前缀:``/noauth``、``/ui``。
"""

import logging

from fastapi import Request
from fastapi.exceptions import HTTPException

from ..local_auth import extract_identity

logger = logging.getLogger(__name__)

# 不鉴权的路径前缀(与 langgraph_api 的豁免习惯一致)
EXEMPT_PREFIXES = ("/noauth", "/ui")

# 进程级开关:lifespan 启动后为 True
_runtime_ready: bool = False


def mark_ready() -> None:
    global _runtime_ready
    _runtime_ready = True


def is_ready() -> bool:
    return _runtime_ready


async def require_identity(request: Request) -> str:
    """返回 username;未鉴权抛 401。

    路由以 ``user: str = require_identity`` 注入(FastAPI 直接 await 协程依赖)。
    """
    if request.url.path.startswith(EXEMPT_PREFIXES):
        return "local-user"
    authorization: str | None = request.headers.get("authorization")
    user_header: str | None = request.headers.get("x-openswe-user")
    username = extract_identity(authorization, user_header)
    if username is None:
        logger.debug("401: path=%s auth=%r", request.url.path, authorization)
        raise HTTPException(status_code=401, detail="Invalid bearer token")
    return username


__all__ = [
    "EXEMPT_PREFIXES",
    "require_identity",
    "mark_ready",
    "is_ready",
]
