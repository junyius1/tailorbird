"""per-repo 指令端点(设计书 §5.6)— 存储:store agent_instructions,键 owner/name。

注入系统提示的仓库级指令;full_name 必须是 ``owner/name`` 格式(否则 400)。
"""

import re
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .runtime_client import runtime_store_client as get_client
from .runtime_client import search_items_list

from .auth import require_same_origin_for_mutations, require_session

router = APIRouter(
    prefix="/dashboard/api",
    tags=["agent-instructions"],
    dependencies=[Depends(require_same_origin_for_mutations)],
)

# owner/name 格式
_FULL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
# store 命名空间
_NAMESPACE = "agent_instructions"


class AgentInstructionsCreate(BaseModel):
    """建 per-repo 指令请求体。"""

    # 仓库全名 owner/name
    full_name: str = Field(min_length=3)
    # 指令正文
    content: str = Field(min_length=1)


class AgentInstructionsUpdate(BaseModel):
    """改 per-repo 指令请求体。"""

    # 指令正文
    content: str = Field(min_length=1)


def _serialize(value: dict[str, Any], full_name: str) -> dict[str, Any]:
    """store 值 → 指令对象。"""
    return {
        "full_name": full_name,
        "content": value.get("content"),
        "updated_at": value.get("updated_at"),
    }


@router.get("/agent-instructions")
async def list_agent_instructions(user: dict[str, Any] = Depends(require_session)) -> list[dict[str, Any]]:
    """列 per-repo 指令。"""
    try:
        result = await get_client().store.search_items([_NAMESPACE], limit=500)
    except Exception:  # noqa: BLE001
        return []
    items = search_items_list(result)
    out = []
    for item in items or []:
        key = getattr(item, "key", None) if not isinstance(item, dict) else item.get("key")
        value = getattr(item, "value", None) if not isinstance(item, dict) else item.get("value")
        if isinstance(value, dict) and key:
            out.append(_serialize(value, str(key)))
    return out


@router.post("/agent-instructions", status_code=201)
async def create_agent_instructions(
    body: AgentInstructionsCreate, user: dict[str, Any] = Depends(require_session)
) -> dict[str, Any]:
    """建 per-repo 指令。400 full_name 非 owner/name 格式。"""
    if not _FULL_NAME_RE.match(body.full_name):
        raise HTTPException(status_code=400, detail="full_name: 必须是 owner/name 格式")
    record = {
        "content": body.content,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    await get_client().store.put_item([_NAMESPACE], body.full_name, record)
    return _serialize(record, body.full_name)


@router.get("/agent-instructions/{full_name}")
async def get_agent_instructions(full_name: str, user: dict[str, Any] = Depends(require_session)) -> dict[str, Any]:
    """读单条 per-repo 指令。404 不存在。"""
    try:
        item = await get_client().store.get_item([_NAMESPACE], full_name)
    except Exception:  # noqa: BLE001
        item = None
    if item is None:
        raise HTTPException(status_code=404, detail="not found")
    value = getattr(item, "value", None) if not isinstance(item, dict) else item.get("value")
    return _serialize(value or {}, full_name)


@router.put("/agent-instructions/{full_name}")
async def set_agent_instructions(
    full_name: str,
    body: AgentInstructionsUpdate,
    user: dict[str, Any] = Depends(require_session),
) -> dict[str, Any]:
    """改单条 per-repo 指令。"""
    record = {
        "content": body.content,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    await get_client().store.put_item([_NAMESPACE], full_name, record)
    return _serialize(record, full_name)


@router.delete("/agent-instructions/{full_name}", status_code=204)
async def delete_agent_instructions(full_name: str, user: dict[str, Any] = Depends(require_session)) -> None:
    """删单条 per-repo 指令。404 不存在。"""
    try:
        await get_client().store.delete_item([_NAMESPACE], full_name)
    except Exception as exc:  # noqa: BLE001
        if "404" in str(exc) or "not found" in str(exc).lower():
            raise HTTPException(status_code=404, detail="not found")
        raise
