"""本地用户注册表(设计书 §3.1.1):store 命名空间 ``local_users``。

多进程部署下两个服务器共享同一 store(同一 Postgres)→ 用户表天然跨进程一致。
``OPEN_SWE_USER_REGISTRY=1``(默认开):登录要求 username 已存在,
``POST /auth/register`` 由 admin 或首个用户创建账号;
``OPEN_SWE_USER_REGISTRY=0``(裸开发):任意 username 首登自动建条目。
注册表里可存每用户 git 身份(``git_name``/``git_email``),
用于沙箱 commit 作者(§3.5);未存则回退宿主默认。
"""

import os
from typing import Any

from .runtime_client import runtime_store_client as get_client
from .runtime_client import search_items_list

# store 命名空间:("local_users", <username>)
_NAMESPACE = "local_users"


def registry_enabled() -> bool:
    """是否启用注册表(缺省开)。"""
    return (os.environ.get("OPEN_SWE_USER_REGISTRY", "1") or "1").strip() != "0"


async def read_user_record(username: str) -> dict[str, Any] | None:
    """读用户记录;不存在返回 None。

    返回:
        ``{"username","created_at","admin":bool,"git_name"?,"git_email"?}``。
    """
    try:
        client = get_client()
        item = await client.store.get_item([_NAMESPACE], username)
    except Exception:
        return None
    if item is None:
        return None
    value = getattr(item, "value", None) if not isinstance(item, dict) else item.get("value")
    if isinstance(value, dict):
        return value
    return None


async def upsert_user_record(record: dict[str, Any]) -> None:
    """写/更新用户记录。"""
    client = get_client()
    username = str(record.get("username") or "")
    await client.store.put_item([_NAMESPACE], username, dict(record))


async def list_user_records() -> list[dict[str, Any]]:
    """列全部用户(admin 页用)。"""
    client = get_client()
    result = await client.store.search_items([_NAMESPACE], limit=500)
    items = search_items_list(result)
    records: list[dict[str, Any]] = []
    for item in items or []:
        value = getattr(item, "value", None) if not isinstance(item, dict) else item
        if isinstance(value, dict):
            records.append(value)
    return records


async def user_exists(username: str) -> bool:
    """用户是否已存在(注册表判断)。"""
    return await read_user_record(username) is not None


async def count_users() -> int:
    """用户数(首用户判断用)。"""
    return len(await list_user_records())
