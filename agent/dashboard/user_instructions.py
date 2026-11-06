"""用户级指令端点(设计书 §5.6)— 存储:store user_instructions,键 local_user。

跨线程共享面:注入所有该用户线程的系统提示。
"""

import time
from typing import Any

from fastapi import APIRouter, Depends

from langgraph_sdk import get_client

from .auth import require_same_origin_for_mutations, require_session

router = APIRouter(
    prefix="/dashboard/api",
    tags=["instructions"],
    dependencies=[Depends(require_same_origin_for_mutations)],
)

# store 命名空间:("user_instructions",)
_NAMESPACE = "user_instructions"


@router.get("/me/instructions")
async def get_my_instructions(user: dict[str, Any] = Depends(require_session)) -> dict[str, Any]:
    """读当前用户指令(无则空串)。"""
    try:
        item = await get_client().store.get_item([_NAMESPACE], user["sub"])
    except Exception:  # noqa: BLE001
        item = None
    value = getattr(item, "value", None) if item is not None else None
    instructions = (value or {}).get("instructions", "") if isinstance(value, dict) else ""
    return {"instructions": instructions or ""}


@router.put("/me/instructions")
async def save_my_instructions(
    body: dict[str, Any], user: dict[str, Any] = Depends(require_session)
) -> dict[str, Any]:
    """保存当前用户指令(任意文本,回显)。"""
    instructions = str(body.get("instructions") or "")
    record = {
        "instructions": instructions,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    await get_client().store.put_item([_NAMESPACE], user["sub"], record)
    return {"instructions": instructions}


@router.delete("/me/instructions", status_code=204)
async def delete_my_instructions(user: dict[str, Any] = Depends(require_session)) -> None:
    """删当前用户指令。"""
    try:
        await get_client().store.delete_item([_NAMESPACE], user["sub"])
    except Exception:  # noqa: BLE001
        pass
