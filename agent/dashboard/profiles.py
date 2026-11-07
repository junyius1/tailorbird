"""用户 profile 端点(设计书 §5.6)— 存储:store profiles,键 local_user。

本地化字段:模型默认、effort、推理流开关(reasoning_stream,§10.6 单一事实源
的 profile 半,优先级高于 env 部署默认)。
"""

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from .runtime_client import runtime_store_client as get_client

from .auth import require_same_origin_for_mutations, require_session

router = APIRouter(
    prefix="/dashboard/api",
    tags=["profile"],
    dependencies=[Depends(require_same_origin_for_mutations)],
)

# store 命名空间:("profiles",)
_NAMESPACE = "profiles"
# 允许的 effort 值
_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")


async def _read_profile(sub: str) -> dict[str, Any]:
    """读 profile(无则空 dict)。"""
    try:
        item = await get_client().store.get_item([_NAMESPACE], sub)
    except Exception:  # noqa: BLE001
        return {}
    value = getattr(item, "value", None) if not isinstance(item, dict) else item.get("value")
    return value if isinstance(value, dict) else {}


@router.get("/profile")
async def get_profile(user: dict[str, Any] = Depends(require_session)) -> dict[str, Any]:
    """读当前用户 profile。"""
    profile = await _read_profile(user["sub"])
    profile = dict(profile)
    profile.setdefault("login", user["sub"])
    return profile


@router.put("/profile")
async def save_profile(
    body: dict[str, Any], user: dict[str, Any] = Depends(require_session)
) -> dict[str, Any]:
    """更新当前用户 profile(白名单字段)。400 字段非法。"""
    profile = dict(await _read_profile(user["sub"]))
    # 白名单字段
    if "default_model" in body:
        if not isinstance(body["default_model"], str):
            raise HTTPException(status_code=400, detail="default_model: 必须是字符串")
        profile["default_model"] = body["default_model"]
    if "reasoning_effort" in body:
        if body["reasoning_effort"] not in _EFFORTS:
            raise HTTPException(status_code=400, detail="reasoning_effort: 非法值")
        profile["reasoning_effort"] = body["reasoning_effort"]
    if "reasoning_stream" in body:
        # 推理流开关(§10.6):True/False 权威;None = Inherit(env 默认)
        rs = body["reasoning_stream"]
        if rs is not None and not isinstance(rs, bool):
            raise HTTPException(status_code=400, detail="reasoning_stream: 必须是 true/false/null")
        profile["reasoning_stream"] = rs
    profile["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    await get_client().store.put_item([_NAMESPACE], user["sub"], profile)
    profile.setdefault("login", user["sub"])
    return profile
