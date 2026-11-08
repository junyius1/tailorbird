"""dashboard 进程 → 运行时(:2026)的客户端封装(设计书 §5.1/§18)。

两种部署拓扑共用同一套代理代码:
- 同进程:dashboard 与运行时同端口,这里经 HTTP loopback 调自己;
- 双进程:``LANGGRAPH_URL`` 指向运行时(缺省 :2026),浏览器只连 :8002 dashboard。

**身份注入**(§3.1.2 形态 A):所有对运行时的调用统一加
``Authorization: Bearer <OPEN_SWE_LOCAL_AUTH_TOKEN>`` +
``X-OpenSWE-User: <当前会话 sub>``,使运行时侧以**该用户身份**执行
(thread 归属/权限与当前用户一致)。
"""

import logging
import os
from typing import Any

from langgraph_sdk import get_client
from langgraph_sdk.client import LangGraphClient

# 运行时地址(缺省 :2026)
_DEFAULT_URL = "http://localhost:2026"


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


def search_items_list(result: Any) -> list[Any]:
    """store ``search_items`` 结果 → items 列表(防 ``dict.items`` 方法误取)。

    SDK 返回 ``SearchItemsResponse``(**dict**,形如 ``{"items": [...]}``);
    对 dict 用 ``getattr(result, "items", ...)`` 会拿到内建 ``dict.items``
    方法本身(iterable 判定即炸 TypeError)。统一走本函数。
    """
    if isinstance(result, dict):
        items = result.get("items")
        return items if isinstance(items, list) else []
    if isinstance(result, list):
        return result
    items = getattr(result, "items", None)
    return items if isinstance(items, list) else []


def runtime_store_client(user: str | None = None) -> LangGraphClient:
    """store 访问客户端:统一经 HTTP 调运行时(bearer + 身份头)。

    与 ``thread_api`` 的 ``langgraph_client()`` 同模式:
    - 同进程(:2026)→ HTTP loopback 调自己;
    - 双进程(:8002)→ HTTP 调 :2026。
    不用 in-process ``get_client()`` 绑定:standalone dashboard 进程无运行时,
    其 in-process 绑定在**请求期**才抛错(构造期探测不到),会让
    user_registry / plans / profiles 等 store 调用静默失败("unknown user")。
    两种拓扑共享运行时背后的同一 Postgres store,HTTP 访问天然一致。
    """
    return langgraph_client(user)
