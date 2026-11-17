"""V2 命令端点(浏览器 useStream 的控制面)。

``POST /threads/{id}/commands`` — JSON 请求/响应(非流式)。
命令方法(与 langchain_protocol 对齐):
- ``run.start`` → 建 run(与 V1 创建共享引擎)→ ``{run_id}``;
- ``input.respond`` → 校验 pending 中断 → 以 ``Command(resume=...)`` 续跑;
- ``subscription.subscribe`` / ``subscription.unsubscribe`` → 本实现里
  SSE 连接即订阅(``stream/events``),此处返回 ``not_supported``;
- ``state.get`` / ``state.listCheckpoints`` → 读路径(复用 StateReader);
- 其余 → ``unknown_command``。

响应信封:
  成功 {"type":"success","id":...,"result":...,"meta":{"applied_through_seq":n}}
  失败 {"type":"error","id":...,"error":"<code>","message":"..."}
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request

from ..auth import require_identity
from ..engines import RunBusyError, RunRegistry
from ..threads import ThreadRegistry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["commands"])

# 支持的命令
_SUPPORTED = frozenset(
    {
        "run.start",
        "input.respond",
        "subscription.subscribe",
        "subscription.unsubscribe",
        "state.get",
        "state.listCheckpoints",
    }
)


def _get_runs(request: Request) -> RunRegistry:
    return request.app.state.run_registry


def _get_threads(request: Request) -> ThreadRegistry:
    return request.app.state.threads


def _success(cmd_id: Any, result: Any, seq: int) -> dict[str, Any]:
    return {"type": "success", "id": cmd_id, "result": result, "meta": {"applied_through_seq": seq}}


def _error(cmd_id: Any, code: str, message: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "error", "id": cmd_id, "error": code, "message": message}
    out.update(extra)
    return out


@router.post("/threads/{thread_id}/commands")
async def thread_command(
    thread_id: str,
    request: Request,
    user: str = Depends(require_identity),
    runs: RunRegistry = Depends(_get_runs),
    threads: ThreadRegistry = Depends(_get_threads),
):
    import json

    raw = await request.body()
    try:
        command = json.loads(raw) if raw else None
    except (ValueError, UnicodeDecodeError):
        return _json({"type": "error", "id": None, "error": "invalid_argument", "message": "Invalid JSON body"})
    if not isinstance(command, dict):
        return _json({"type": "error", "id": None, "error": "invalid_argument", "message": "command must be an object"})

    cmd_id = command.get("id")
    method = command.get("method")
    params = command.get("params") if isinstance(command.get("params"), dict) else {}

    if not isinstance(method, str) or method not in _SUPPORTED:
        return _json(_error(cmd_id, "unknown_command", f"unsupported method: {method!r}"))
    if not threads.exists(thread_id):
        return _json(_error(cmd_id, "invalid_argument", f"thread {thread_id} not found"))

    engine = runs.engine(thread_id)

    if method == "run.start":
        return _json(await _handle_run_start(engine, params, user, cmd_id))

    if method == "input.respond":
        return _json(await _handle_input_respond(engine, params, user, cmd_id))

    if method in ("subscription.subscribe", "subscription.unsubscribe"):
        return _json(
            _error(
                cmd_id,
                "not_supported",
                "subscription commands are not supported; use POST /threads/{id}/stream/events (the connection is the subscription)",
            )
        )

    if method == "state.get":
        from ..deps import get_config as _cfg
        from ..state import read_state

        cfg = _cfg(request)
        try:
            state, _ = await read_state(
                thread_id,
                version=None,
                budget=cfg.state_read_budget,
            )
        except KeyError:
            return _json(_error(cmd_id, "invalid_argument", f"thread {thread_id} has no state"))
        values = state.get("values") if isinstance(state, dict) else None
        return _json(_success(cmd_id, {"values": values if isinstance(values, dict) else {}}, engine.seq))

    if method == "state.listCheckpoints":
        from ..deps import get_config as _cfg
        from ..state import read_history

        cfg = _cfg(request)
        try:
            history = await read_history(thread_id, limit=50, before=None, budget=cfg.state_read_budget)
        except KeyError:
            return _json(_error(cmd_id, "invalid_argument", f"thread {thread_id} has no checkpoints"))
        checkpoints = [
            {
                "id": (h.get("checkpoint") or {}).get("checkpoint_id"),
                "parent_id": (h.get("parent_checkpoint") or {}).get("checkpoint_id"),
            }
            for h in history
            if isinstance(h, dict) and isinstance(h.get("checkpoint"), dict)
        ]
        return _json(_success(cmd_id, {"checkpoints": checkpoints}, engine.seq))

    return _json(_error(cmd_id, "unknown_command", f"unsupported method: {method!r}"))


async def _handle_run_start(engine, params: dict[str, Any], user: str, cmd_id: Any) -> dict[str, Any]:
    assistant_id = params.get("assistant_id")
    if not isinstance(assistant_id, str) or not assistant_id:
        return _error(cmd_id, "invalid_argument", "assistant_id is required")
    config = params.get("config") if isinstance(params.get("config"), dict) else {}
    metadata = params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
    strategy = params.get("multitask_strategy") or "interrupt"
    source = metadata.get("source") or "web"
    try:
        record = await engine.start_run(
            assistant_id=assistant_id,
            input=params.get("input"),
            config=config,
            metadata=metadata,
            multitask_strategy=strategy if isinstance(strategy, str) else "interrupt",
            user=user,
            source=source,
        )
    except RunBusyError as exc:
        return _error(cmd_id, "invalid_argument", f"thread already has a run in progress: {exc.active_run_id}")
    return _success(cmd_id, {"run_id": record.run_id}, engine.seq)


async def _handle_input_respond(engine, params: dict[str, Any], user: str, cmd_id: Any) -> dict[str, Any]:
    """单条 {interrupt_id, namespace, response, update?, goto?} 或
    批量 {responses: [...]}。校验 interrupt_id 仍 pending。"""
    entries: list[tuple[str, Any]] = []
    update = params.get("update")
    goto = params.get("goto")
    raw = params.get("responses")
    if isinstance(raw, list) and raw:
        for item in raw:
            if not isinstance(item, dict):
                continue
            iid = item.get("interrupt_id")
            if isinstance(iid, str) and iid:
                entries.append((iid, item.get("response")))
    else:
        iid = params.get("interrupt_id")
        if isinstance(iid, str) and iid:
            entries.append((iid, params.get("response")))

    if not entries:
        return _error(cmd_id, "invalid_argument", "interrupt_id is required")

    # 校验:全部 interrupt_id 必须仍 pending(引擎跟踪 + 状态兜底)
    pending = engine.pending_interrupt_ids()
    for iid, _ in entries:
        if pending and iid not in pending:
            return _error(cmd_id, "no_such_interrupt", f"Unknown or already-consumed interrupt: {iid}")

    resume_map = {iid: resp for iid, resp in entries}
    from langgraph.types import Command

    graph_input: Any
    if update is not None or goto is not None:
        graph_input = Command(resume=resume_map, update=update, goto=goto)
    else:
        graph_input = Command(resume=resume_map)

    config = params.get("config") if isinstance(params.get("config"), dict) else {}
    metadata = params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
    try:
        record = await engine.start_run(
            assistant_id=engine.last_assistant_id or "agent",
            input=graph_input,
            config=config,
            metadata=metadata,
            multitask_strategy="reject",
            user=user,
            source=metadata.get("source") or "web",
        )
    except RunBusyError as exc:
        return _error(cmd_id, "no_such_run", f"thread already has a run in progress: {exc.active_run_id}")
    return _success(cmd_id, {"run_id": record.run_id}, engine.seq)


def _json(payload: dict[str, Any]) -> Any:
    import json

    from fastapi import Response

    return Response(
        content=json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
        media_type="application/json",
    )
