"""plan 存储(设计书 §5.3):store 命名空间 ``plans``。

每个线程一条 plan 记录(``key = thread_id``),含内容、格式、状态与审批人:
- ``status`` ∈ draft / pending / approved / rejected;
- 评论存 ``("plans", thread_id, "comments")``,``key = comment_id``。
审批动作(approve/reject)只翻状态;**审批后由前端发 run.start 继续,
服务端不自动跑**。
"""

import time
import uuid
from typing import Any

from .runtime_client import runtime_store_client as get_client
from .runtime_client import search_items_list

# store 命名空间:("plans", <thread_id>)
_NAMESPACE = "plans"
# 评论子命名空间:("plans", <thread_id>, "comments")
_COMMENTS_SUFFIX = "comments"
# 允许的 plan 状态
STATUSES = ("draft", "pending", "approved", "rejected")
# 允许的格式
FORMATS = ("html", "markdown")


def _client():
    """运行时 store 客户端。"""
    return get_client()


def _plan_namespace(thread_id: str) -> list[str]:
    """线程的 plan 命名空间。"""
    return [_NAMESPACE, thread_id]


async def get_plan(thread_id: str) -> dict[str, Any] | None:
    """读线程当前 plan;无则 None。"""
    try:
        item = await _client().store.get_item(_plan_namespace(thread_id), "current")
    except Exception:
        return None
    if item is None:
        return None
    value = getattr(item, "value", None) if not isinstance(item, dict) else item.get("value")
    return value if isinstance(value, dict) else None


async def save_plan(
    thread_id: str,
    content: str,
    content_format: str,
    author_login: str,
) -> dict[str, Any]:
    """保存/更新 plan(进入待审批态 pending)。

    参数:
        thread_id: 线程 id。
        content: plan 正文(html 或 markdown)。
        content_format: "html" | "markdown"。
        author_login: 作者 username。
    """
    if content_format not in FORMATS:
        raise ValueError(f"content_format 必须是 {FORMATS} 之一")
    if not content.strip():
        raise ValueError("content 不能为空")
    record: dict[str, Any] = {
        "thread_id": thread_id,
        "content": content,
        "content_format": content_format,
        # 每次保存都回到 pending(重新进入审批)
        "status": "pending",
        "author_login": author_login,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    await _client().store.put_item(_plan_namespace(thread_id), "current", record)
    return record


async def set_plan_status(thread_id: str, status: str) -> dict[str, Any]:
    """翻 plan 状态(approve/reject);无 plan 抛 KeyError。"""
    if status not in ("approved", "rejected"):
        raise ValueError(f"status 必须是 approved/rejected,收到 {status}")
    plan = await get_plan(thread_id)
    if plan is None:
        raise KeyError(f"线程 {thread_id} 无 plan")
    if plan.get("status") in ("approved", "rejected"):
        raise ConflictError(f"plan already {plan.get('status')}")
    plan["status"] = status
    plan["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    await _client().store.put_item(_plan_namespace(thread_id), "current", plan)
    return plan


class ConflictError(Exception):
    """已终态再审批等业务冲突(路由层转 409)。"""


def _comments_namespace(thread_id: str) -> list[str]:
    """评论命名空间。"""
    return [_NAMESPACE, thread_id, _COMMENTS_SUFFIX]


async def list_plan_comments(thread_id: str) -> list[dict[str, Any]]:
    """列 plan 评论(按创建时间)。"""
    try:
        result = await _client().store.search_items(
            namespace=_comments_namespace(thread_id), limit=500
        )
    except Exception:
        return []
    items = search_items_list(result)
    comments = [
        (getattr(i, "value", None) if not isinstance(i, dict) else i.get("value"))
        for i in items or []
    ]
    comments = [c for c in comments if isinstance(c, dict)]
    comments.sort(key=lambda c: str(c.get("created_at") or ""))
    return comments


async def add_plan_comment(
    thread_id: str, author_login: str, content: str
) -> dict[str, Any]:
    """加 plan 评论。"""
    if not content.strip():
        raise ValueError("content 不能为空")
    comment = {
        "comment_id": uuid.uuid4().hex[:12],
        "author_login": author_login,
        "content": content,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    await _client().store.put_item(_comments_namespace(thread_id), comment["comment_id"], comment)
    return comment


async def delete_plan_comment(thread_id: str, comment_id: str, author_login: str) -> None:
    """删 plan 评论(仅作者或 admin;非作者抛 PermissionError)。"""
    comments = await list_plan_comments(thread_id)
    target = next((c for c in comments if c.get("comment_id") == comment_id), None)
    if target is None:
        raise KeyError(f"评论 {comment_id} 不存在")
    if target.get("author_login") != author_login:
        # admin 例外由调用方(路由)判断:这里只放行作者,admin 由路由先判
        raise PermissionError("非作者不能删评论")
    await _client().store.delete_item(_comments_namespace(thread_id), comment_id)
