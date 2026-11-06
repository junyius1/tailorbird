"""Skills / 组织技能端点(设计书 §5.6)— 存储:store user_skills / organization_skills。

用户级 ``/skills`` 按 sub 隔离;组织级 ``/organization-skills`` 所有登录用户
可见、仅 admin 可写。name 小写字母/数字/单连字符(≤64)。
"""

import re
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from langgraph_sdk import get_client

from .auth import require_admin, require_same_origin_for_mutations, require_session

router = APIRouter(
    prefix="/dashboard/api",
    tags=["skills"],
    dependencies=[Depends(require_same_origin_for_mutations)],
)

# name 规则:小写字母/数字/单连字符
_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def _validate_name(name: str) -> None:
    """校验技能名(非法 → 400)。"""
    if not _NAME_RE.match(name) or len(name) > 64:
        raise HTTPException(status_code=400, detail="name: 必须是小写字母/数字/单连字符")


def _user_ns(sub: str) -> list[str]:
    """用户技能命名空间:("user_skills", <sub>)。"""
    return ["user_skills", sub]


def _org_ns() -> list[str]:
    """组织技能命名空间:("organization_skills",)。"""
    return ["organization_skills"]


class SkillCreate(BaseModel):
    """建技能请求体。"""

    # 技能名
    name: str = Field(min_length=1, max_length=64)
    # 描述
    description: str = Field(min_length=1, max_length=1024)
    # 指令(可空)
    instructions: str | None = Field(default=None, max_length=20000)


class SkillUpdate(BaseModel):
    """改技能请求体。"""

    # 描述
    description: str | None = Field(default=None, max_length=1024)
    # 指令
    instructions: str | None = Field(default=None, max_length=20000)


def _serialize(value: dict[str, Any], name: str) -> dict[str, Any]:
    """store 值 → 技能对象。"""
    return {
        "name": name,
        "description": value.get("description"),
        "instructions": value.get("instructions"),
        "updated_at": value.get("updated_at"),
    }


# ---- 用户级 ----


@router.get("/skills")
async def list_skills(user: dict[str, Any] = Depends(require_session)) -> list[dict[str, Any]]:
    """列当前用户技能(按 sub 隔离)。"""
    try:
        result = await get_client().store.search_items(namespace=_user_ns(user["sub"]), limit=500)
    except Exception:  # noqa: BLE001
        return []
    items = getattr(result, "items", result) if not isinstance(result, list) else result
    out = []
    for item in items or []:
        name = getattr(item, "key", None) if not isinstance(item, dict) else item.get("key")
        value = getattr(item, "value", None) if not isinstance(item, dict) else item.get("value")
        if isinstance(value, dict) and name:
            out.append(_serialize(value, str(name)))
    return out


@router.post("/skills", status_code=201)
async def create_skill(body: SkillCreate, user: dict[str, Any] = Depends(require_session)) -> dict[str, Any]:
    """建技能。400 name 非法;409 重名。"""
    _validate_name(body.name)
    ns = _user_ns(user["sub"])
    try:
        existing = await get_client().store.get_item(ns, body.name)
    except Exception:  # noqa: BLE001
        existing = None
    if existing is not None:
        raise HTTPException(status_code=409, detail="skill already exists")
    record = {
        "description": body.description,
        "instructions": body.instructions,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    await get_client().store.put_item(ns, body.name, record)
    return _serialize(record, body.name)


@router.put("/skills/{name}")
async def update_skill(name: str, body: SkillUpdate, user: dict[str, Any] = Depends(require_session)) -> dict[str, Any]:
    """改技能。404 不存在。"""
    ns = _user_ns(user["sub"])
    try:
        item = await get_client().store.get_item(ns, name)
    except Exception:  # noqa: BLE001
        item = None
    if item is None:
        raise HTTPException(status_code=404, detail="not found")
    value = dict(getattr(item, "value", {}) if not isinstance(item, dict) else item.get("value") or {})
    if body.description is not None:
        value["description"] = body.description
    if body.instructions is not None:
        value["instructions"] = body.instructions
    value["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    await get_client().store.put_item(ns, name, value)
    return _serialize(value, name)


@router.delete("/skills/{name}", status_code=204)
async def delete_skill(name: str, user: dict[str, Any] = Depends(require_session)) -> None:
    """删技能。404 不存在。"""
    try:
        await get_client().store.delete_item(_user_ns(user["sub"]), name)
    except Exception as exc:  # noqa: BLE001
        if "404" in str(exc) or "not found" in str(exc).lower():
            raise HTTPException(status_code=404, detail="not found")
        raise


# ---- 组织级(所有用户可读,仅 admin 可写)----


@router.get("/organization-skills")
async def list_org_skills(user: dict[str, Any] = Depends(require_session)) -> list[dict[str, Any]]:
    """列组织技能(所有登录用户可见)。"""
    try:
        result = await get_client().store.search_items(namespace=_org_ns(), limit=500)
    except Exception:  # noqa: BLE001
        return []
    items = getattr(result, "items", result) if not isinstance(result, list) else result
    out = []
    for item in items or []:
        name = getattr(item, "key", None) if not isinstance(item, dict) else item.get("key")
        value = getattr(item, "value", None) if not isinstance(item, dict) else item.get("value")
        if isinstance(value, dict) and name:
            out.append(_serialize(value, str(name)))
    return out


@router.post("/organization-skills", status_code=201)
async def create_org_skill(body: SkillCreate, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    """建组织技能(仅 admin)。"""
    _validate_name(body.name)
    try:
        existing = await get_client().store.get_item(_org_ns(), body.name)
    except Exception:  # noqa: BLE001
        existing = None
    if existing is not None:
        raise HTTPException(status_code=409, detail="skill already exists")
    record = {
        "description": body.description,
        "instructions": body.instructions,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    await get_client().store.put_item(_org_ns(), body.name, record)
    return _serialize(record, body.name)


@router.put("/organization-skills/{name}")
async def update_org_skill(name: str, body: SkillUpdate, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    """改组织技能(仅 admin)。"""
    try:
        item = await get_client().store.get_item(_org_ns(), name)
    except Exception:  # noqa: BLE001
        item = None
    if item is None:
        raise HTTPException(status_code=404, detail="not found")
    value = dict(getattr(item, "value", {}) if not isinstance(item, dict) else item.get("value") or {})
    if body.description is not None:
        value["description"] = body.description
    if body.instructions is not None:
        value["instructions"] = body.instructions
    value["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    await get_client().store.put_item(_org_ns(), name, value)
    return _serialize(value, name)


@router.delete("/organization-skills/{name}", status_code=204)
async def delete_org_skill(name: str, user: dict[str, Any] = Depends(require_admin)) -> None:
    """删组织技能(仅 admin)。"""
    try:
        await get_client().store.delete_item(_org_ns(), name)
    except Exception as exc:  # noqa: BLE001
        if "404" in str(exc) or "not found" in str(exc).lower():
            raise HTTPException(status_code=404, detail="not found")
        raise
