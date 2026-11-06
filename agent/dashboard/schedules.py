"""Schedules / Automations 端点(设计书 §5.5,5 个)。

落地:每个 schedule = 运行时一条 cron(`POST /runs/crons`,
``assistant_id=scheduler``);到点由 scheduler 图扇出
``launch_scheduled_agent_run(schedule_id)``。
schedule 元数据(名字/prompt/模型)存 store ``schedules`` 命名空间,
cron_id 存进元数据供运行时对账。
"""

import logging
import os
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from langgraph_sdk import get_client

from .auth import require_same_origin_for_mutations, require_session

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/dashboard/api",
    tags=["schedules"],
    dependencies=[Depends(require_same_origin_for_mutations)],
)

# store 命名空间:("schedules", <id>)
_NAMESPACE = "schedules"
# scheduler 图 id
_ASSISTANT_ID = "scheduler"


def _parse_cron(expr: str) -> bool:
    """cron 表达式 5 段合法性校验(非法 → False)。"""
    try:
        from croniter import croniter

        return croniter.is_valid(expr)
    except Exception:  # noqa: BLE001
        # croniter 缺失时退回朴素校验(5 段)
        return len(expr.split()) == 5


def _next_fire_iso(expr: str) -> str | None:
    """cron 下一次触发时间(ISO;失败 None)。"""
    try:
        from croniter import croniter
        from datetime import datetime, timezone

        base = datetime.now(timezone.utc)
        return croniter(expr, base).get_next(datetime).isoformat()
    except Exception:  # noqa: BLE001
        return None


async def _get_record(schedule_id: str) -> dict[str, Any] | None:
    """读 schedule 记录。"""
    try:
        item = await get_client().store.get_item([_NAMESPACE], schedule_id)
    except Exception:  # noqa: BLE001
        return None
    value = getattr(item, "value", None) if not isinstance(item, dict) else item.get("value")
    return value if isinstance(value, dict) else None


async def _put_record(record: dict[str, Any]) -> None:
    """写 schedule 记录。"""
    await get_client().store.put_item([_NAMESPACE], str(record["id"]), record)


class ScheduleCreateBody(BaseModel):
    """新建 schedule 请求体。"""

    # 名称
    name: str = Field(min_length=1, max_length=120)
    # cron 表达式(5 段)
    cron: str = Field(min_length=1)
    # 到点执行的 prompt
    prompt: str = Field(min_length=1)
    # 是否启用
    enabled: bool = True
    # 仓库(自由文本 owner/name;可空)
    repo: str | None = None
    # 模型覆盖(可空)
    model_id: str | None = None
    # 推理力度(可空)
    effort: str | None = None


async def _register_cron(record: dict[str, Any]) -> None:
    """到运行时的 cron 面注册(assistant_id=scheduler,input.task 默认扇出)。"""
    client = get_client()
    body = {
        "cron": record["cron"],
        "assistant_id": _ASSISTANT_ID,
        "input": {
            "task": "schedule",
            "schedule_id": record["id"],
            "name": record.get("name"),
            "prompt": record.get("prompt"),
            "repo": record.get("repo"),
            "model_id": record.get("model_id"),
            "effort": record.get("effort"),
            "enabled": record.get("enabled", True),
            "local_user": record.get("owner"),
        },
        "metadata": {"kind": "schedule", "schedule_id": record["id"]},
    }
    # 运行时 cron 创建端点:POST /runs/crons
    import httpx

    from .runtime_client import runtime_url

    url = f"{runtime_url()}/runs/crons"
    headers = {}
    token = (os.environ.get("OPEN_SWE_LOCAL_AUTH_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(timeout=30.0) as http:
        resp = await http.post(url, json=body, headers=headers)
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"cron 注册失败: {resp.text[:300]}",
        )
    record["cron_id"] = (resp.json() or {}).get("cron_id")


async def _unregister_cron(schedule_id: str) -> None:
    """删运行时 cron(best-effort)。"""
    record = await _get_record(schedule_id)
    cron_id = (record or {}).get("cron_id")
    if not cron_id:
        return
    import httpx

    from .runtime_client import runtime_url

    url = f"{runtime_url()}/runs/crons/{cron_id}"
    headers = {}
    token = (os.environ.get("OPEN_SWE_LOCAL_AUTH_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            await http.delete(url, headers=headers)
    except Exception:  # noqa: BLE001
        logger.warning("删运行时 cron 失败(best-effort): %s", cron_id)


@router.get("/schedules")
async def list_schedules(
    limit: int = 50, offset: int = 0, user: dict[str, Any] = Depends(require_session)
) -> list[dict[str, Any]]:
    """列当前用户的定时任务(按 local_user 过滤)。"""
    try:
        result = await get_client().store.search_items(namespace=[_NAMESPACE], limit=500)
    except Exception:  # noqa: BLE001
        return []
    items = getattr(result, "items", result) if not isinstance(result, list) else result
    records: list[dict[str, Any]] = []
    for item in items or []:
        value = getattr(item, "value", None) if not isinstance(item, dict) else item.get("value")
        if not isinstance(value, dict):
            continue
        if value.get("owner") != user["sub"] and not user.get("is_admin"):
            continue
        records.append(value)
    records.sort(key=lambda r: str(r.get("created_at") or ""))
    return records[offset : offset + limit]


@router.post("/schedules", status_code=201)
async def create_schedule(
    body: ScheduleCreateBody, user: dict[str, Any] = Depends(require_session)
) -> dict[str, Any]:
    """新建定时任务(到点由 scheduler 扇出)。400 非法 cron(透传)。"""
    if not _parse_cron(body.cron):
        raise HTTPException(status_code=400, detail="cron: 非法表达式")
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    record: dict[str, Any] = {
        "id": uuid.uuid4().hex[:12],
        "name": body.name,
        "cron": body.cron,
        "prompt": body.prompt,
        "enabled": body.enabled,
        "repo": body.repo,
        "model_id": body.model_id,
        "effort": body.effort,
        "owner": user["sub"],
        "created_at": now_iso,
        "next_fire_at": _next_fire_iso(body.cron),
    }
    await _put_record(record)
    try:
        await _register_cron(record)
    except HTTPException:
        # 注册失败回滚
        await get_client().store.delete_item([_NAMESPACE], record["id"])
        raise
    return record


@router.patch("/schedules/{schedule_id}")
async def update_schedule(
    schedule_id: str,
    payload: dict[str, Any],
    user: dict[str, Any] = Depends(require_session),
) -> dict[str, Any]:
    """改名/改 cron/启停。404 不存在;400 非法 cron。"""
    record = await _get_record(schedule_id)
    if record is None or record.get("owner") != user["sub"] and not user.get("is_admin"):
        raise HTTPException(status_code=404, detail="not found")
    for field in ("name", "cron", "prompt", "enabled", "repo", "model_id", "effort"):
        if field in payload:
            record[field] = payload[field]
    if "cron" in payload and not _parse_cron(str(record["cron"])):
        raise HTTPException(status_code=400, detail="cron: 非法表达式")
    record["next_fire_at"] = _next_fire_iso(str(record["cron"]))
    # 重新注册 cron(先删旧再建)
    await _unregister_cron(schedule_id)
    record.pop("cron_id", None)
    await _put_record(record)
    await _register_cron(record)
    return record


@router.delete("/schedules/{schedule_id}", status_code=204)
async def delete_schedule(
    schedule_id: str, user: dict[str, Any] = Depends(require_session)
) -> None:
    """删定时任务(连带删运行时 cron)。"""
    record = await _get_record(schedule_id)
    if record is None or record.get("owner") != user["sub"] and not user.get("is_admin"):
        raise HTTPException(status_code=404, detail="not found")
    await _unregister_cron(schedule_id)
    await get_client().store.delete_item([_NAMESPACE], schedule_id)


@router.post("/schedules/{schedule_id}/trigger")
async def trigger_schedule(
    schedule_id: str, user: dict[str, Any] = Depends(require_session)
) -> dict[str, Any]:
    """立即触发一次(不等下个 tick;触发即入队,异步执行)。"""
    record = await _get_record(schedule_id)
    if record is None or record.get("owner") != user["sub"] and not user.get("is_admin"):
        raise HTTPException(status_code=404, detail="not found")
    # 直接扇出一次 scheduler run
    import httpx

    from .runtime_client import runtime_url

    url = f"{runtime_url()}/runs"
    headers = {}
    token = (os.environ.get("OPEN_SWE_LOCAL_AUTH_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-OpenSWE-User"] = user["sub"]
    body = {
        "assistant_id": _ASSISTANT_ID,
        "input": {
            "task": "schedule",
            "schedule_id": schedule_id,
            "name": record.get("name"),
            "prompt": record.get("prompt"),
            "repo": record.get("repo"),
            "model_id": record.get("model_id"),
            "effort": record.get("effort"),
            "enabled": True,
            "local_user": record.get("owner"),
        },
        "metadata": {"kind": "schedule", "source": "schedule", "trigger": "manual"},
        "multitask_strategy": "enqueue",
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.post(url, json=body, headers=headers)
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=502, detail="runtime unreachable")
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail="runtime unreachable")
    run = resp.json() or {}
    return {"triggered": True, "run_id": run.get("run_id")}
