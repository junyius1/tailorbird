"""V1 runs API(dashboard Python SDK 驱动路径)。

与 langgraph_api 的 wire 语义对齐:
- ``POST /threads/{id}/runs`` / ``POST /runs`` → 200 + Run JSON
  + ``Content-Location: /threads/{id}/runs/{run_id}``;
- ``GET /threads/{id}/runs`` → 200 + Run[](limit/offset/status);
- ``GET /threads/{id}/runs/{run_id}`` → 200 + Run JSON / 404;
- ``POST /threads/{id}/runs/{run_id}/cancel?wait=&action=`` → 202
  (wait=true 走 join 流,本地无消费者,按 202 立即返回);
- run 创建体支持 input / command(resume) / checkpoint_id(fork) /
  multitask_strategy / metadata / config;webhook 字段接收但由运行时
  自己的 loopback 替代(见 engines._dispatch_webhook)。

fire-and-forget:创建即后台驱动,完成经 webhook 回调 dashboard,
dashboard 轮询 ``runs.list`` / 线程 state 刷新展示态。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response

from ..auth import require_identity
from ..engines import RunBusyError, RunRegistry
from ..threads import ThreadRegistry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["runs"])


def _get_runs(request: Request) -> RunRegistry:
    return request.app.state.run_registry


def _get_threads(request: Request) -> ThreadRegistry:
    return request.app.state.threads


def _command_input(command: Any) -> Any:
    """SDK ``command`` 字段 → langgraph Command(resume/update/goto)。"""
    from langgraph.types import Command

    if not isinstance(command, dict):
        return command
    filtered = {k: v for k, v in command.items() if v is not None}
    if not filtered:
        return None
    return Command(**filtered)


def _thread_exists(threads: ThreadRegistry, thread_id: str) -> bool:
    return threads.exists(thread_id)


@router.post("/threads/{thread_id}/runs")
async def create_thread_run(
    thread_id: str,
    request: Request,
    user: str = Depends(require_identity),
    runs: RunRegistry = Depends(_get_runs),
    threads: ThreadRegistry = Depends(_get_threads),
):
    body = await _json_body(request)
    if not _thread_exists(threads, thread_id):
        return Response(status_code=404, content=b'{"detail":"Not Found"}', media_type="application/json")
    return await _create_run(body, thread_id, user, runs, threads)


@router.post("/runs")
async def create_stateless_run(
    request: Request,
    user: str = Depends(require_identity),
    runs: RunRegistry = Depends(_get_runs),
    threads: ThreadRegistry = Depends(_get_threads),
):
    body = await _json_body(request)
    thread_id = body.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        thread_id = str(uuid.uuid4())
        body["thread_id"] = thread_id
    if not _thread_exists(threads, thread_id):
        threads.create(thread_id, metadata=body.get("metadata") if isinstance(body.get("metadata"), dict) else None)
    return await _create_run(body, thread_id, user, runs, threads)


async def _create_run(
    body: dict[str, Any],
    thread_id: str,
    user: str,
    runs: RunRegistry,
    threads: ThreadRegistry,
):
    assistant_id = body.get("assistant_id")
    if not isinstance(assistant_id, str) or not assistant_id:
        return Response(
            status_code=422,
            content=b'{"detail":[{"loc":["body","assistant_id"],"msg":"field required","type":"value_error.missing"}]}',
            media_type="application/json",
        )

    input_ = body.get("input")
    command = body.get("command")
    graph_input: Any
    if command is not None:
        graph_input = _command_input(command)
    elif input_ is not None:
        graph_input = input_
    else:
        graph_input = None

    config = body.get("config") if isinstance(body.get("config"), dict) else {}
    configurable = dict(config.get("configurable") or {})
    checkpoint_id = body.get("checkpoint_id") or configurable.get("checkpoint_id")
    if isinstance(checkpoint_id, str) and checkpoint_id:
        configurable["checkpoint_id"] = checkpoint_id
    run_config = {"configurable": configurable}
    if isinstance(config.get("recursion_limit"), int):
        run_config["recursion_limit"] = config["recursion_limit"]

    metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    if body.get("interrupt_before") is not None:
        configurable["interrupt_before"] = body["interrupt_before"]
    if body.get("interrupt_after") is not None:
        configurable["interrupt_after"] = body["interrupt_after"]

    strategy = body.get("multitask_strategy") or "interrupt"
    source = metadata.get("source") or "dashboard"
    durability = body.get("durability")
    if not isinstance(durability, str) or not durability:
        durability = None

    graph_id = assistant_id if assistant_id in ("agent", "scheduler") else "agent"
    engine = runs.engine(thread_id, graph_id=graph_id)
    try:
        record = await engine.start_run(
            assistant_id=assistant_id,
            input=graph_input,
            config=run_config,
            metadata=metadata,
            multitask_strategy=strategy if isinstance(strategy, str) else "interrupt",
            user=user,
            source=source,
            durability=durability,
        )
    except RunBusyError as exc:
        return Response(
            status_code=409,
            content=f'{{"detail":{{"error":"busy_thread","message":"thread already has a run in progress","running_run_id":"{exc.active_run_id}"}}}}'.encode(),
            media_type="application/json",
        )

    # 线程 metadata 同步(与 dashboard _stamp_run_body 的 title/branch 语义一致:
    # 那边已写入 metadata,这里不重复派生)
    threads.patch_metadata(thread_id, {"latest_run_id": record.run_id})
    return Response(
        content=_run_json(record.to_dict()),
        status_code=200,
        media_type="application/json",
        headers={"Content-Location": f"/threads/{thread_id}/runs/{record.run_id}"},
    )


@router.get("/threads/{thread_id}/runs")
async def list_thread_runs(
    thread_id: str,
    limit: int = Query(10, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    status: str | None = Query(None),
    user: str = Depends(require_identity),
    runs: RunRegistry = Depends(_get_runs),
    threads: ThreadRegistry = Depends(_get_threads),
):
    if not _thread_exists(threads, thread_id):
        return Response(status_code=404, content=b"[]", media_type="application/json")
    engine = runs.get_engine(thread_id)
    records = engine.list_runs(limit=limit * 4) if engine else []
    if status:
        records = [r for r in records if r.status == status]
    records = records[offset : offset + limit]
    body = [_run_json(r.to_dict()) for r in records]
    return Response(
        content=b"[" + b",".join(body) + b"]",
        media_type="application/json",
    )


@router.get("/threads/{thread_id}/runs/{run_id}")
async def get_thread_run(
    thread_id: str,
    run_id: str,
    user: str = Depends(require_identity),
    runs: RunRegistry = Depends(_get_runs),
    threads: ThreadRegistry = Depends(_get_threads),
):
    engine = runs.get_engine(thread_id)
    record = engine.get_run(run_id) if engine else None
    if record is None:
        return Response(status_code=404, content=b'{"detail":"Not Found"}', media_type="application/json")
    return Response(content=_run_json(record.to_dict()), media_type="application/json")


@router.post("/threads/{thread_id}/runs/{run_id}/cancel")
async def cancel_thread_run(
    thread_id: str,
    run_id: str,
    wait: int = Query(0),
    action: str = Query("interrupt"),
    user: str = Depends(require_identity),
    runs: RunRegistry = Depends(_get_runs),
    threads: ThreadRegistry = Depends(_get_threads),
):
    if not _thread_exists(threads, thread_id):
        return Response(status_code=404, content=b'{"detail":"Not Found"}', media_type="application/json")
    engine = runs.get_engine(thread_id)
    record = engine.get_run(run_id) if engine else None
    if record is None:
        return Response(status_code=404, content=b'{"detail":"Not Found"}', media_type="application/json")
    if engine is None:
        return Response(status_code=404, content=b'{"detail":"Not Found"}', media_type="application/json")
    engine.request_cancel(run_id, action=action if action in ("interrupt", "rollback") else "interrupt")
    if not wait:
        return Response(status_code=202)
    # wait=true:join 流——重放缓冲尾 + 实时,直到该 run 终帧(或 120s 上限)
    return await _join_run_stream(engine, run_id)


async def _join_run_stream(engine: Any, run_id: str) -> Any:
    """join 流:该 run 的剩余事件(SSE 帧),直到 terminal 或超时。"""
    import asyncio

    from fastapi.responses import StreamingResponse

    from ..engines import _TERMINAL
    from .stream_events import _sse_frame

    heartbeat = 5.0

    async def body_iter():
        import time as _t

        deadline = _t.monotonic() + 120
        # 已终态:只重放缓冲里该 run 之后的事件(简化:全缓冲尾 200 条)
        tail = list(engine._buffer)[-200:]
        for event in tail:
            yield _sse_frame(event.get("method", "event"), event, event.get("seq"))
        record = engine.get_run(run_id)
        if record is None or record.status in _TERMINAL:
            return
        sid, sub = engine.subscribe(
            {"values", "messages", "lifecycle", "input", "updates", "checkpoints", "tools", "tasks", "custom"},
            [[]],
            8,
        )
        try:
            while _t.monotonic() < deadline:
                rec = engine.get_run(run_id)
                if rec is not None and rec.status in _TERMINAL:
                    break
                try:
                    event = await asyncio.wait_for(sub.queue.get(), timeout=heartbeat)
                except asyncio.TimeoutError:
                    yield b": ping\n\n"
                    continue
                if event is None:
                    break
                yield _sse_frame(event.get("method", "event"), event, event.get("seq"))
        finally:
            engine.unsubscribe(sid)

    return StreamingResponse(
        body_iter(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


async def _json_body(request: Request) -> dict[str, Any]:
    import json

    raw = await request.body()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _run_json(record: dict[str, Any]) -> bytes:
    import json

    return json.dumps(record, ensure_ascii=False, default=str).encode("utf-8")
