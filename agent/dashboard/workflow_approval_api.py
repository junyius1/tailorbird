"""Workflow 审批端点(设计书 §5.4,3 个)— 存储:thread metadata workflow_approvals。

本地分支变体不产生 GitHub workflow push 审批,但该通用机制保留:
若后端注入 push 守卫类工具仍触发,审批条作为通用机制可用。
"""

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from .auth import require_same_origin_for_mutations, require_session
from .runtime_client import langgraph_client

workflow_approval_router = APIRouter(
    prefix="/dashboard/api/workflow-approval",
    tags=["workflow-approval"],
    dependencies=[Depends(require_same_origin_for_mutations)],
)
# metadata 里存审批列表的键
_META_KEY = "workflow_approvals"


async def _read_approvals(thread_id: str, user: dict[str, Any]) -> list[dict[str, Any]]:
    """读线程 metadata 里的审批列表。"""
    client = langgraph_client(user["sub"])
    try:
        thread = await client.threads.get(thread_id)
    except Exception:  # noqa: BLE001
        return []
    metadata = (thread or {}).get("metadata") or {}
    approvals = metadata.get(_META_KEY)
    return approvals if isinstance(approvals, list) else []


async def _write_approvals(
    thread_id: str, user: dict[str, Any], approvals: list[dict[str, Any]]
) -> None:
    """写回审批列表。"""
    client = langgraph_client(user["sub"])
    await client.threads.update(thread_id, metadata={_META_KEY: approvals})


@workflow_approval_router.get("/{thread_id}")
async def list_approvals(
    thread_id: str, user: dict[str, Any] = Depends(require_session)
) -> dict[str, Any]:
    """列该线程待决/历史审批;无 → 200 空数组。"""
    return {"approvals": await _read_approvals(thread_id, user)}


async def _set_decision(
    thread_id: str, fingerprint: str, decision: str, user: dict[str, Any]
) -> dict[str, Any]:
    """置某指纹审批为 decided(approved/rejected);不存在 404,已决 409。"""
    approvals = await _read_approvals(thread_id, user)
    target = next(
        (a for a in approvals if a.get("fingerprint") == fingerprint), None
    )
    if target is None:
        raise HTTPException(status_code=404, detail="not found")
    if target.get("status") in ("approved", "rejected"):
        raise HTTPException(status_code=409, detail="already decided")
    target["status"] = decision
    target["decided_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    target["decided_by"] = user["sub"]
    await _write_approvals(thread_id, user, approvals)
    return target


@workflow_approval_router.post("/{thread_id}/{fingerprint}/approve")
async def approve(
    thread_id: str,
    fingerprint: str,
    user: dict[str, Any] = Depends(require_session),
) -> dict[str, Any]:
    """批准该指纹的推送。"""
    return await _set_decision(thread_id, fingerprint, "approved", user)


@workflow_approval_router.post("/{thread_id}/{fingerprint}/reject")
async def reject(
    thread_id: str,
    fingerprint: str,
    user: dict[str, Any] = Depends(require_session),
) -> dict[str, Any]:
    """拒绝该指纹的推送。"""
    return await _set_decision(thread_id, fingerprint, "rejected", user)
