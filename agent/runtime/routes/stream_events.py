"""V2 事件流端点(浏览器 useStream 的观察面)。

``POST /threads/{id}/stream/events`` — SSE。连接即订阅:body 的
channels/namespaces/depth 定义过滤,``since`` 是单调 seq 游标
(重放 ``seq > since`` 的缓冲事件;游标落后于缓冲头 → 先推一个
``resume_gap`` 错误帧再进入实时)。

帧格式(与参考实现一致):
    event:<method>
    data:<信封 JSON>
    id:<seq>

心跳:每 ``RUNTIME_SSE_HEARTBEAT``(缺省 5s)发注释帧 ``: ping``
(防代理掐空闲连接;dashboard httpx 流式 timeout=None 不受影响)。

订阅生命周期:连接建立 → 安装订阅(重放在前,实时在后,无重放/实时
交错)→ 客户端断开 → 卸载。同一事件对所有匹配订阅各投递一次,
客户端按 event_id 去重(参考实现同语义)。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ..auth import require_identity
from ..engines import RunRegistry
from ..threads import ThreadRegistry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["stream-events"])

_VALID_CHANNELS = frozenset(
    {
        "values",
        "updates",
        "messages",
        "tools",
        "custom",
        "lifecycle",
        "input",
        "tasks",
        "checkpoints",
    }
)


def _get_runs(request: Request) -> RunRegistry:
    return request.app.state.run_registry


def _get_threads(request: Request) -> ThreadRegistry:
    return request.app.state.threads


def _channel_valid(name: Any) -> bool:
    if not isinstance(name, str) or not name:
        return False
    if name.startswith("custom:"):
        return bool(name[len("custom:") :])
    return name in _VALID_CHANNELS


@router.post("/threads/{thread_id}/stream/events")
async def thread_stream_events(
    thread_id: str,
    request: Request,
    user: str = Depends(require_identity),
    runs: RunRegistry = Depends(_get_runs),
    threads: ThreadRegistry = Depends(_get_threads),
):
    raw = await request.body()
    try:
        body = json.loads(raw) if raw else {}
    except (ValueError, UnicodeDecodeError):
        body = {}
    if not isinstance(body, dict):
        body = {}

    channels = body.get("channels")
    if not isinstance(channels, list) or not channels or not all(_channel_valid(c) for c in channels):
        return _json_body(400, {"detail": "channels is required and must be a non-empty list of valid channels"})
    channels = {c for c in channels}
    if any(c.startswith("custom:") for c in channels):
        # 具名 custom 通道 → 按 data.name 过滤(由 _Sub 支持)
        pass

    namespaces = body.get("namespaces")
    if not isinstance(namespaces, list) or not namespaces:
        namespaces = [[]]
    namespaces = [ns if isinstance(ns, list) and all(isinstance(s, str) for s in ns) else [] for ns in namespaces]

    depth = body.get("depth")
    depth = max(int(depth), 1) if isinstance(depth, (int, float)) and not isinstance(depth, bool) else 1

    since = body.get("since")
    if since is not None and (isinstance(since, bool) or not isinstance(since, int)):
        return _json_body(400, {"detail": "since must be an integer"})

    if not threads.exists(thread_id):
        return _json_body(404, {"detail": "Not Found"})

    engine = runs.engine(thread_id)
    sid, sub = engine.subscribe(channels, namespaces, depth)
    replay, gap = engine._buffer_since(since)

    heartbeat = float(getattr(request.app.state.runtime_config, "sse_heartbeat_seconds", 5.0) or 5.0)

    async def body_iter():
        try:
            if gap:
                yield _sse_frame(
                    "error",
                    {
                        "type": "error",
                        "id": None,
                        "error": "resume_gap",
                        "message": "since cursor is older than the retained buffer",
                        "since": since,
                        "min_available_seq": replay[0]["seq"] if replay else None,
                    },
                    None,
                )
            for event in replay:
                if await _client_disconnected(request):
                    return
                yield _sse_frame(event.get("method", "event"), event, event.get("seq"))
            while True:
                try:
                    event = await asyncio.wait_for(sub.queue.get(), timeout=heartbeat)
                except asyncio.TimeoutError:
                    if await _client_disconnected(request):
                        return
                    yield b": ping\n\n"
                    continue
                if event is None:  # 哨兵:引擎关闭
                    return
                yield _sse_frame(event.get("method", "event"), event, event.get("seq"))
        finally:
            engine.unsubscribe(sid)

    return StreamingResponse(
        body_iter(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _client_disconnected(request: Request) -> bool:
    try:
        return await request.is_disconnected()
    except Exception:
        return False


def _sse_frame(method: Any, event: dict[str, Any], seq: Any) -> bytes:
    name = method if isinstance(method, str) and method else "event"
    data = json.dumps(event, ensure_ascii=False, default=str).encode("utf-8")
    parts = [f"event:{name}\n".encode(), f"data:{data.decode('utf-8')}\n".encode()]
    if isinstance(seq, int):
        parts.append(f"id:{seq}\n".encode())
    parts.append(b"\n")
    return b"".join(parts)


def _json_body(status_code: int, payload: dict[str, Any]):
    from fastapi import Response

    return Response(
        content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        status_code=status_code,
        media_type="application/json",
    )
