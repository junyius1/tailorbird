"""用户 profile 端点(设计书 §5.6)— 存储:store profiles,键 local_user。

本地化字段:模型默认、effort、推理流开关(reasoning_stream,§10.6 单一事实源
的 profile 半,优先级高于 env 部署默认)。
"""

import os
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from .runtime_client import runtime_store_client as get_client

from ..utils.repo import normalize_repo_meta, resolve_local_repo_path
from .auth import require_same_origin_for_mutations, require_session

def _repo_full_name() -> str:
    """当前仓库 full_name(本地变体单仓,env 可覆盖)。"""
    return os.environ.get("TAILORBIRD_REPO_FULL_NAME", "junyius1/tailorbird")


router = APIRouter(
    prefix="/dashboard/api",
    tags=["profile"],
    dependencies=[Depends(require_same_origin_for_mutations)],
)

# 路径添加仓库的 store 命名空间:(local_repos, <username>)
_LOCAL_REPOS_NAMESPACE = "local_repos"

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


async def _read_local_repos(sub: str) -> list[dict[str, Any]]:
    """读用户"按路径添加"的仓库列表(无则空)。"""
    try:
        item = await get_client().store.get_item([_LOCAL_REPOS_NAMESPACE], sub)
    except Exception:  # noqa: BLE001
        return []
    value = getattr(item, "value", None) if not isinstance(item, dict) else item.get("value")
    if not isinstance(value, dict):
        return []
    repos = value.get("repos")
    if not isinstance(repos, list):
        return []
    return [entry for entry in repos if isinstance(entry, dict) and str(entry.get("path") or "").strip()]


def _normalize_local_path(raw: Any) -> str:
    """路径入参 → 规范化绝对路径;非法(非绝对)抛 400。"""
    path = str(raw or "").strip().rstrip("/")
    if not path:
        raise HTTPException(status_code=400, detail="path: 必填")
    if not path.startswith("/"):
        raise HTTPException(status_code=400, detail="path: 必须是绝对路径")
    return str(Path(path).expanduser().resolve())


@router.get("/repositories")
async def list_repositories(user: dict[str, Any] = Depends(require_session)):
    """本地变体:无 GitHub 集成,返回固定当前仓库 + 用户按路径添加的仓库。

    路径仓库的 ``full_name`` 即宿主绝对路径本身(沙箱挂载直达该路径,
    见 utils/repo.py 的 normalize_repo_meta)。
    """
    # 每项带 local_path = 沙箱实际挂载/操作的宿主目录(tooltip 展示用)
    repositories = [
        {
            "full_name": _repo_full_name(),
            "private": True,
            "local_path": resolve_local_repo_path(
                normalize_repo_meta(_repo_full_name(), create=False)
            ),
        }
    ]
    seen = {repositories[0]["full_name"]}
    for entry in await _read_local_repos(user["sub"]):
        full_name = str(entry.get("path") or "").strip().rstrip("/")
        if full_name and full_name not in seen:
            seen.add(full_name)
            repositories.append(
                {
                    "full_name": full_name,
                    "private": True,
                    "local_path": resolve_local_repo_path(
                        normalize_repo_meta(full_name, create=False)
                    ),
                }
            )
    return {"installations": [], "repositories": repositories}


@router.post("/repositories")
async def add_local_repository(
    body: dict[str, Any], user: dict[str, Any] = Depends(require_session)
) -> dict[str, Any]:
    """按宿主路径添加仓库(下拉框"添加路径")。

    目录必须已存在;重复添加幂等(不重复入库)。
    """
    raw = body.get("path") if isinstance(body, dict) else None
    full_name = _normalize_local_path(raw)
    host = Path(full_name)
    if not host.is_dir():
        raise HTTPException(status_code=400, detail=f"路径不存在或不是目录: {full_name}")
    entries = await _read_local_repos(user["sub"])
    if not any(str(entry.get("path")) == full_name for entry in entries):
        entries.append(
            {
                "path": full_name,
                "name": host.name or "workspace",
                "added_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )
        await get_client().store.put_item(
            [_LOCAL_REPOS_NAMESPACE], user["sub"], {"repos": entries}
        )
    return {"full_name": full_name, "private": True}


@router.delete("/repositories")
async def remove_local_repository(
    body: dict[str, Any], user: dict[str, Any] = Depends(require_session)
) -> dict[str, bool]:
    """删除按路径添加的仓库(下拉框列表项的垃圾桶)。

    只接受路径仓库(``full_name`` 以 ``/`` 开头);固定默认仓库不可删。
    """
    raw = body.get("full_name") if isinstance(body, dict) else None
    full_name = str(raw or "").strip().rstrip("/")
    if not full_name.startswith("/"):
        raise HTTPException(status_code=400, detail="仅路径添加的仓库可以删除")
    entries = await _read_local_repos(user["sub"])
    if not any(str(entry.get("path")) == full_name for entry in entries):
        raise HTTPException(status_code=404, detail="该仓库不在已添加列表里")
    entries = [entry for entry in entries if str(entry.get("path")) != full_name]
    await get_client().store.put_item(
        [_LOCAL_REPOS_NAMESPACE], user["sub"], {"repos": entries}
    )
    return {"ok": True}
