"""线程 CRUD + state/history 路由(替代 langgraph_api 的 ``/threads*`` 面)。

记录源:``ThreadRegistry``(内存;启动时从 checkpoints 重建)。
wire 形状与 SDK 客户端(langgraph_sdk 的 threads / 浏览器 JS client)对齐:

- ``POST /threads``           body ``{thread_id?, metadata?, if_exists?}`` → Thread / 409
- ``GET /threads/{id}``       → Thread / 404
- ``PATCH /threads/{id}``     body ``{metadata?}`` → Thread(metadata 合并)
- ``DELETE /threads/{id}``    → 204 / 404
- ``POST /threads/search``    body ``{metadata?, status?, ids?, limit, offset,
                               sort_by?, sort_order?}`` → [Thread]
- ``POST /threads/count``     body 同上(无分页) → ``{"count": n}``
- ``GET /threads/{id}/state`` → ThreadState / 404 / 503
- ``POST /threads/{id}/state/checkpoint`` body ``{checkpoint?}`` → ThreadState
- ``GET /threads/{id}/state/{checkpoint_id}`` → ThreadState
- ``POST /threads/{id}/history`` body ``{limit?, before?}`` → [ThreadState]

过滤语义与 inmem 一致:metadata 用 jsonb ``@>`` 包含匹配,status 精确匹配;
默认按 ``updated_at`` 降序。
"""

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from ..auth import require_identity
from ..deps import get_checkpointer, get_threads
from ..state import (
    latest_checkpoint_id,
    read_history,
    read_state,
    reader_state_at_checkpoint,
    state_cache,
)
from ..threads import ThreadRecord

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/threads", tags=["threads"])


def _record_to_thread(rec: ThreadRecord) -> dict[str, Any]:
    return {
        "thread_id": rec.thread_id,
        "created_at": rec.created_at,
        "updated_at": rec.updated_at,
        "state_updated_at": rec.state_updated_at,
        "metadata": rec.metadata,
        "status": rec.status,
        "config": rec.config,
    }


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be an object")
    return body


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.post("")
async def create_thread(request: Request, user: str = Depends(require_identity)):
    body = await _json_body(request)
    threads = get_threads(request)
    thread_id = body.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        import uuid

        thread_id = str(uuid.uuid4())
    if threads.exists(thread_id):
        if body.get("if_exists") == "do_nothing":
            return JSONResponse(content=_record_to_thread(threads.get(thread_id)))
        raise HTTPException(
            status_code=409, detail=f"Thread with ID {thread_id} already exists"
        )
    metadata = body.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise HTTPException(status_code=422, detail="metadata must be an object")
    record = threads.create(thread_id, metadata=metadata)
    return JSONResponse(content=_record_to_thread(record))


@router.patch("/{thread_id}")
async def patch_thread(thread_id: str, request: Request, user: str = Depends(require_identity)):
    body = await _json_body(request)
    threads = get_threads(request)
    record = threads.get(thread_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found")
    metadata = body.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, dict):
            raise HTTPException(status_code=422, detail="metadata must be an object")
        threads.patch_metadata(thread_id, metadata)
    record = threads.get(thread_id)
    return JSONResponse(content=_record_to_thread(record))


@router.delete("/{thread_id}")
async def delete_thread(thread_id: str, request: Request, user: str = Depends(require_identity)):
    threads = get_threads(request)
    if not threads.exists(thread_id):
        raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found")
    threads.delete(thread_id)
    state_cache().invalidate(thread_id)
    checkpointer = get_checkpointer(request)
    try:
        await checkpointer.adelete_thread(thread_id)
    except Exception:  # noqa: BLE001
        logger.warning("adelete_thread(%s) 失败", thread_id, exc_info=True)
    return Response(status_code=204)


@router.get("/{thread_id}")
async def get_thread(thread_id: str, request: Request, user: str = Depends(require_identity)):
    threads = get_threads(request)
    record = threads.get(thread_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found")
    return JSONResponse(content=_record_to_thread(record))


# ---------------------------------------------------------------------------
# 搜索 / 计数
# ---------------------------------------------------------------------------


def _jsonb_contains(superset: Any, subset: Any) -> bool:
    """jsonb ``@>`` 语义(与 inmem 的 is_jsonb_contained 一致)。"""
    if isinstance(subset, dict):
        if not isinstance(superset, dict):
            return False
        for key, value in subset.items():
            if key not in superset:
                return False
            if not _jsonb_contains(superset[key], value):
                return False
        return True
    return superset == subset


def _apply_filters(
    records: list[ThreadRecord],
    metadata: dict[str, Any] | None,
    status: str | None,
    ids: list[str] | None,
) -> list[ThreadRecord]:
    id_set = set(ids) if ids else None
    out = []
    for rec in records:
        if id_set is not None and rec.thread_id not in id_set:
            continue
        if metadata and not _jsonb_contains(rec.metadata, metadata):
            continue
        if status and rec.status != status:
            continue
        out.append(rec)
    return out


def _sort_and_page(
    records: list[ThreadRecord],
    sort_by: str | None,
    sort_order: str | None,
    limit: int,
    offset: int,
) -> list[ThreadRecord]:
    if sort_by in ("thread_id", "created_at", "updated_at", "state_updated_at", "status"):
        reverse = not (sort_order and sort_order.upper() == "ASC")
        key = lambda r: getattr(r, sort_by) or ""  # noqa: E731
    else:
        key = lambda r: r.updated_at or ""  # noqa: E731
        reverse = True
    ordered = sorted(records, key=key, reverse=reverse)
    return ordered[offset : offset + limit]


@router.post("/search")
async def search_threads(request: Request, user: str = Depends(require_identity)):
    body = await _json_body(request)
    threads = get_threads(request)
    limit = int(body.get("limit") or 10)
    offset = int(body.get("offset") or 0)
    limit = max(1, min(limit, 1000))
    records = threads.list_all()
    filtered = _apply_filters(
        records,
        metadata=body.get("metadata"),
        status=body.get("status"),
        ids=body.get("ids"),
    )
    paged = _sort_and_page(
        filtered,
        sort_by=body.get("sort_by"),
        sort_order=body.get("sort_order"),
        limit=limit,
        offset=offset,
    )
    return JSONResponse(content=[_record_to_thread(r) for r in paged])


@router.post("/count")
async def count_threads(request: Request, user: str = Depends(require_identity)):
    body = await _json_body(request)
    threads = get_threads(request)
    records = threads.list_all()
    filtered = _apply_filters(
        records,
        metadata=body.get("metadata"),
        status=body.get("status"),
        ids=body.get("ids"),
    )
    return JSONResponse(content={"count": len(filtered)})


# ---------------------------------------------------------------------------
# state / history
# ---------------------------------------------------------------------------


def _budget(request: Request) -> float:
    return float(request.app.state.runtime_config.state_read_budget)


async def _version_for(request: Request, thread_id: str) -> str:
    """state 缓存 version:state_updated_at 缺失时回退最新 checkpoint id。"""
    record = get_threads(request).get(thread_id)
    if record is not None:
        version = str(record.state_updated_at or "")
        if version:
            return version
    return await latest_checkpoint_id(get_checkpointer(request), thread_id) or "unknown"


@router.get("/{thread_id}/state")
async def get_thread_state(thread_id: str, request: Request, user: str = Depends(require_identity)):
    threads = get_threads(request)
    if not threads.exists(thread_id):
        raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found")
    version = await _version_for(request, thread_id)
    try:
        state, _cached = await read_state(
            thread_id, version=version, budget=_budget(request)
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="thread has no state")
    except asyncio.TimeoutError:
        # 预算内读不完:503 而非降级空值(降级空值 = UI 空白线程的根源)
        raise HTTPException(
            status_code=503,
            detail="state read timed out; thread too large or runtime busy",
        )
    except Exception:
        logger.exception("读 state 失败 thread=%s", thread_id)
        raise HTTPException(status_code=502, detail="state read failed")
    return JSONResponse(content=state)


@router.post("/{thread_id}/state/checkpoint")
async def get_thread_state_checkpoint(
    thread_id: str, request: Request, user: str = Depends(require_identity)
):
    """SDK ``getState``(无 checkpoint 参数)与 V2 ``state.get`` 的实际端点。

    body ``{checkpoint?: {checkpoint_id?, checkpoint_ns?}, subgraphs?: bool}``。
    无 checkpoint → 最新 state;有 → 指定 checkpoint。
    """
    threads = get_threads(request)
    if not threads.exists(thread_id):
        raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found")
    body = await _json_body(request)
    checkpoint = body.get("checkpoint") or {}
    checkpoint_id = checkpoint.get("checkpoint_id")
    if not checkpoint_id:
        return await get_thread_state(thread_id, request, user)
    return await _state_at_checkpoint(thread_id, request, checkpoint_id)


@router.get("/{thread_id}/state/{checkpoint_id}")
async def get_thread_state_at_checkpoint(
    thread_id: str,
    checkpoint_id: str,
    request: Request,
    user: str = Depends(require_identity),
):
    threads = get_threads(request)
    if not threads.exists(thread_id):
        raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found")
    return await _state_at_checkpoint(thread_id, request, checkpoint_id)


async def _state_at_checkpoint(
    thread_id: str, request: Request, checkpoint_id: str
) -> JSONResponse:
    try:
        state = await reader_state_at_checkpoint(
            thread_id,
            checkpoint_id,
            budget=_budget(request),
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="checkpoint not found")
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=503,
            detail="state read timed out; thread too large or runtime busy",
        )
    except Exception:
        logger.exception(
            "读 checkpoint state 失败 thread=%s ckpt=%s", thread_id, checkpoint_id
        )
        raise HTTPException(status_code=502, detail="state read failed")
    return JSONResponse(content=state)


@router.post("/{thread_id}/history")
async def get_thread_history(
    thread_id: str, request: Request, user: str = Depends(require_identity)
):
    """SDK ``getHistory`` 的端点。body ``{limit?, before?}``(新→旧)。"""
    threads = get_threads(request)
    if not threads.exists(thread_id):
        raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found")
    body = await _json_body(request)
    limit = int(body.get("limit") or 10)
    limit = max(1, min(limit, 1000))
    before = body.get("before") or None
    try:
        items = await read_history(
            thread_id, limit, before=before, budget=_budget(request)
        )
    except Exception:
        logger.exception("读 history 失败 thread=%s", thread_id)
        raise HTTPException(status_code=502, detail="history read failed")
    return JSONResponse(content=items)
