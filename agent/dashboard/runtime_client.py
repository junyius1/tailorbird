"""dashboard 进程 → 2024 运行时的客户端封装(设计书 §5.1/§18)。

两种部署拓扑共用同一套代理代码:
- 同进程:dashboard 与运行时都在 :2024,这里经 HTTP loopback 调自己;
- 双进程:``LANGGRAPH_URL`` 指向 :2024 运行时,浏览器只连 :8001 dashboard。

**身份注入**(§3.1.2 形态 A):所有对运行时的调用统一加
``Authorization: Bearer <OPEN_SWE_LOCAL_AUTH_TOKEN>`` +
``X-OpenSWE-User: <当前会话 sub>``,使运行时侧以**该用户身份**执行
(thread 归属/权限与当前用户一致)。
"""

import os
from typing import Any

from langgraph_sdk import get_client
from langgraph_sdk.client import LangGraphClient

# 运行时地址(缺省 :2024)
_DEFAULT_URL = "http://localhost:2024"


def runtime_url() -> str:
    """运行时地址,优先级:LANGGRAPH_URL > LANGGRAPH_PORT > 缺省 :2024。

    同进程挂载时 dashboard 与运行时同端口;显式设 LANGGRAPH_URL 可指向
    独立运行时进程(双进程拓扑)。
    """
    explicit = os.environ.get("LANGGRAPH_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    port = os.environ.get("LANGGRAPH_PORT", "").strip()
    if port:
        return f"http://localhost:{port}"
    return _DEFAULT_URL


def _auth_headers(user: str | None = None) -> dict[str, str]:
    """进程间 bearer 头(§3.1.2 形态 A)。"""
    headers: dict[str, str] = {}
    token = (os.environ.get("OPEN_SWE_LOCAL_AUTH_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # 当前会话用户 → 运行时侧身份(无则机器身份 local-service)
    if user:
        headers["X-OpenSWE-User"] = user
    return headers


def langgraph_client(user: str | None = None) -> LangGraphClient:
    """构造指向运行时的 SDK 客户端(带身份头)。

    参数:
        user: 当前会话 username(注入 X-OpenSWE-User);None = 机器身份。
    """
    return get_client(url=runtime_url(), headers=_auth_headers(user))


def raw_httpx_headers(user: str | None = None) -> dict[str, str]:
    """httpx 代理(SSE/原始 body)用的同一套身份头。"""
    return _auth_headers(user)
