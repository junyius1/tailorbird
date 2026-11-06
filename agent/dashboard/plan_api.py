"""Plan 端点(设计书 §5.3,7 个)— 存储:store 命名空间 plans。"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import plan_store
from .auth import require_admin, require_same_origin_for_mutations, require_session

plan_router = APIRouter(
    prefix="/dashboard/api/plan",
    tags=["plan"],
    dependencies=[Depends(require_same_origin_for_mutations)],
)


class PlanSaveBody(BaseModel):
    """保存 plan 请求体。"""

    # plan 正文
    content: str = Field(min_length=1)
    # 格式
    content_format: str = Field(pattern="^(html|markdown)$")


class CommentBody(BaseModel):
    """加评论请求体。"""

    # 评论内容
    content: str = Field(min_length=1)


@plan_router.get("/{thread_id}")
async def get_plan(thread_id: str, user: dict[str, Any] = Depends(require_session)) -> dict[str, Any]:
    """读当前 plan;无 → 404(前端据此不显示 plan 面板)。"""
    plan = await plan_store.get_plan(thread_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="not found")
    comments = await plan_store.list_plan_comments(thread_id)
    plan = dict(plan)
    plan["comments"] = comments
    return plan


@plan_router.put("/{thread_id}")
async def save_plan(
    thread_id: str, body: PlanSaveBody, user: dict[str, Any] = Depends(require_session)
) -> dict[str, Any]:
    """保存/更新 plan(进入待审批)。400 content 空/格式非法。"""
    try:
        record = await plan_store.save_plan(
            thread_id, body.content, body.content_format, user["sub"]
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return record


@plan_router.post("/{thread_id}/approve")
async def approve_plan(
    thread_id: str, user: dict[str, Any] = Depends(require_session)
) -> dict[str, Any]:
    """审批通过(状态翻转);审批后由前端发 run.start 继续,服务端不自动跑。

    已终态再审批 → 409。
    """
    try:
        return await plan_store.set_plan_status(thread_id, "approved")
    except KeyError:
        raise HTTPException(status_code=404, detail="not found")
    except plan_store.ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@plan_router.post("/{thread_id}/reject")
async def reject_plan(
    thread_id: str, user: dict[str, Any] = Depends(require_session)
) -> dict[str, Any]:
    """审批拒绝。已终态再审批 → 409。"""
    try:
        return await plan_store.set_plan_status(thread_id, "rejected")
    except KeyError:
        raise HTTPException(status_code=404, detail="not found")
    except plan_store.ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@plan_router.get("/{thread_id}/comments")
async def list_comments(
    thread_id: str, user: dict[str, Any] = Depends(require_session)
) -> list[dict[str, Any]]:
    """列 plan 评论。无 plan → 404。"""
    if await plan_store.get_plan(thread_id) is None:
        raise HTTPException(status_code=404, detail="not found")
    return await plan_store.list_plan_comments(thread_id)


@plan_router.post("/{thread_id}/comments")
async def add_comment(
    thread_id: str, body: CommentBody, user: dict[str, Any] = Depends(require_session)
) -> dict[str, Any]:
    """加评论。404 无 plan;400 空内容。"""
    if await plan_store.get_plan(thread_id) is None:
        raise HTTPException(status_code=404, detail="not found")
    try:
        return await plan_store.add_plan_comment(thread_id, user["sub"], body.content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@plan_router.delete("/{thread_id}/comments/{comment_id}", status_code=204)
async def delete_comment(
    thread_id: str,
    comment_id: str,
    user: dict[str, Any] = Depends(require_session),
) -> None:
    """删评论(仅作者或 admin;非作者 → 403)。"""
    try:
        await plan_store.delete_plan_comment(
            thread_id, comment_id,
            user["sub"] if user.get("is_admin") else user["sub"],
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="not found")
    except PermissionError:
        # 非作者:admin 才放行
        if not user.get("is_admin"):
            raise HTTPException(status_code=403, detail="forbidden")
        # admin 强删:直接删(绕过作者检查)
        from .plan_store import _comments_namespace

        from langgraph_sdk import get_client

        await get_client().store.delete_item(
            _comments_namespace(thread_id), comment_id
        )
