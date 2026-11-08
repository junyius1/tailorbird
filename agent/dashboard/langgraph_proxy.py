"""LangGraph 协议代理(``/dashboard/api/threads``、``/dashboard/api/runs``)。

open-swe 的 ``@langchain/react`` StreamProvider 以标准 LangGraph 协议
(``POST /threads/{id}/runs/stream`` 等)驱动运行时。浏览器只能带
``tb_session`` cookie,身份与元数据由本代理在服务端补全:

- 鉴权:cookie 会话 → 运行时身份头(形态 A);共享 token 永不进浏览器;
- 归属:run 创建时强制 ``metadata.local_user = sub``、``source``、
  ``trigger_kind``;从首条 human 消息补 ``title`` / ``branch``;
- 隔离:``POST /threads/search`` 注入 ``local_user`` 过滤;
- 透传:其余请求原样转发(含 ``Content-Location`` 响应头);SSE 逐 chunk
  回传,客户端断开必须释放上游订阅。
"""

import asyncio
import json
import re
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

import httpx

from .auth import require_same_origin_for_mutations, require_session
from .runtime_client import raw_httpx_headers, runtime_url

router = APIRouter(
    prefix="/dashboard/api",
    tags=["langgraph-proxy"],
    dependencies=[Depends(require_same_origin_for_mutations)],
)

_MAX_LIFETIME = 14400.0
_TITLE_MAX = 80

# 透传时需丢弃的请求头(重算/危险/由本进程注入)
_STRIP_REQUEST_HEADERS = {
    "host",
    "connection",
    "content-length",
    "authorization",
    "cookie",
    "x-forwarded-for",
    "x-forwarded-host",
    "accept-encoding",
    "sec-fetch-site",
    "sec-fetch-mode",
    "sec-fetch-dest",
    "origin",
    "referer",
    "user-agent",
}

# 透传时需丢弃的响应头(hop-by-hop + 由本进程重算的)
_STRIP_RESPONSE_HEADERS = {
    "connection",
    "content-length",
    "transfer-encoding",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "upgrade",
    "date",
    "x-process-time",
}


def _first_human_text(payload: Any) -> str:
    """run 创建 body → 首条 human 文本(字符串 / 消息列表两种形态)。"""
    if not isinstance(payload, dict):
        return ""
    input_ = payload.get("input")
    if isinstance(input_, str):
        return input_.strip()
    if not isinstance(input_, dict):
        return ""
    messages = input_.get("messages")
    if not isinstance(messages, list):
        return ""
    for message in messages:
        if not isinstance(message, dict) or message.get("type") != "human":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif isinstance(block, str):
                    parts.append(block)
            text = "".join(parts).strip()
            if text:
                return text
    return ""


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:40] or "thread"


def _stamp_run_body(body: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    """run 创建 body 补全:归属 + 来源 + 首条消息派生的 title/branch。"""
    metadata = body.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        body["metadata"] = metadata
    metadata["local_user"] = user["sub"]
    metadata["source"] = "dashboard"
    metadata["thread_category"] = "interactive"
    metadata["trigger_kind"] = "user"
    title = _first_human_text(body)
    if title:
        short = title[:_TITLE_MAX] + ("…" if len(title) > _TITLE_MAX else "")
        if not metadata.get("title"):
            metadata["title"] = short
        if not metadata.get("branch"):
            metadata["branch"] = f"local/{_slugify(short)}"
    config = body.get("config")
    if not isinstance(config, dict):
        config = {}
        body["config"] = config
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        configurable = {}
        config["configurable"] = configurable
    configurable["local_user"] = user["sub"]
    configurable.setdefault("source", "dashboard")
    # 与 thread_api 一致的 run 语义:可恢复事件流 + 中断式并发策略
    body.setdefault("stream_resumable", True)
    body.setdefault("multitask_strategy", "interrupt")
    return body


def _stamp_search_body(body: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    """threads/search:强制只查本身份线程(metadata.local_user)。"""
    filters = body.get("metadata")
    if not isinstance(filters, dict):
        filters = {}
        body["metadata"] = filters
    filters["local_user"] = user["sub"]
    return filters and body


_RUN_THREAD_RE = re.compile(r"^/threads/([^/]+)/runs(/.*)?$")


def _run_thread_id(path: str) -> str | None:
    """run 创建路径 → thread_id(仅对 run 端点有意义)。"""
    match = _RUN_THREAD_RE.match(path)
    return match.group(1) if match else None


async def _ensure_thread(client: httpx.AsyncClient, path: str, body_bytes: bytes, headers: dict[str, str]) -> bool:
    """线程不存在时懒创建(SDK 客户端 mint uuidv7,本地运行时不自动建)。

    仅当上游对 run 创建端点返回 404 时调用;建线程失败 → 返回 False。
    """
    thread_id = _run_thread_id(path)
    if thread_id is None:
        return False
    try:
        payload = json.loads(body_bytes) if body_bytes else {}
    except (ValueError, UnicodeDecodeError):
        payload = {}
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    if not isinstance(metadata, dict):
        metadata = {}
    resp = await client.post(
        runtime_url() + "/threads",
        json={"thread_id": thread_id, "metadata": metadata},
        headers=headers,
    )
    if resp.status_code >= 400:
        return False
    return True

def _forward_headers(request: Request, user: dict[str, Any]) -> dict[str, str]:
    headers = raw_httpx_headers(user["sub"])
    for name, value in request.headers.items():
        if name.lower() in _STRIP_REQUEST_HEADERS:
            continue
        headers[name] = value
    return headers


async def _read_body(request: Request, user: dict[str, Any], path: str) -> bytes:
    body_bytes = await request.body()
    try:
        payload: Any = json.loads(body_bytes) if body_bytes else None
    except (ValueError, UnicodeDecodeError):
        payload = None
    if isinstance(payload, dict):
        if path.endswith("/runs/stream") or path.endswith("/runs"):
            payload = _stamp_run_body(payload, user)
        elif path.endswith("/threads/search"):
            payload = _stamp_search_body(payload, user)
        else:
            return body_bytes
        body_bytes = json.dumps(payload).encode("utf-8")
    return body_bytes


def _response_headers(upstream: httpx.Response) -> dict[str, str]:
    return {
        k: v
        for k, v in upstream.headers.items()
        if k.lower() not in _STRIP_RESPONSE_HEADERS
    }


async def _proxy(request: Request, user: dict[str, Any], path: str):
    method = request.method
    url = runtime_url() + path
    if request.url.query:
        url = f"{url}?{request.url.query}"
    headers = _forward_headers(request, user)
    body_bytes = await _read_body(request, user, path)
    stream = path.endswith("/runs/stream") or path.endswith(
        ("/stream", "/stream/events")
    )

    if not stream:
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                upstream = await client.send(
                    client.build_request(
                        method,
                        url,
                        headers=headers,
                        content=body_bytes if body_bytes else None,
                    ),
                    follow_redirects=False,
                )
        except httpx.HTTPError as exc:
            return JSONResponse(
                status_code=502, content={"detail": f"runtime unreachable: {exc}"}
            )
        out = _response_headers(upstream)
        media = out.pop("content-type", "application/json")
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=out,
            media_type=media,
        )

    # 流式:先开上游并预读首帧,才能把 Content-Location 透传进响应头
    # (SDK 懒创建时靠它取新 run/thread id)。客户端断开在生成器内监控。
    client = httpx.AsyncClient(timeout=None)
    try:
        upstream = await client.send(
            client.build_request(
                method,
                url,
                headers=headers,
                content=body_bytes if body_bytes else None,
            ),
            stream=True,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        await client.aclose()
        return JSONResponse(
            status_code=502, content={"detail": f"runtime unreachable: {exc}"}
        )

    # SDK 客户端 mint 线程 id 后直发 run 端点;本地运行时要求线程先存在 →
    # 404 时懒建线程并重试一次(元数据已在 _read_body 补全)。
    if upstream.status_code == 404 and _run_thread_id(path) is not None:
        try:
            await upstream.aread()
        except Exception:  # noqa: BLE001
            pass
        try:
            await upstream.aclose()
        except Exception:  # noqa: BLE001
            pass
        if not await _ensure_thread(client, path, body_bytes, headers):
            return JSONResponse(
                status_code=404, content={"detail": "thread not found"}
            )
        try:
            upstream = await client.send(
                client.build_request(
                    method,
                    url,
                    headers=headers,
                    content=body_bytes if body_bytes else None,
                ),
                stream=True,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            await client.aclose()
            return JSONResponse(
                status_code=502, content={"detail": f"runtime unreachable: {exc}"}
            )

    headers_out = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    content_location = upstream.headers.get("content-location")
    if content_location:
        headers_out["Content-Location"] = content_location

    async def _sse():
        lifetime = asyncio.get_event_loop().time() + _MAX_LIFETIME
        try:
            if upstream.status_code >= 400:
                detail = (await upstream.aread()).decode("utf-8", "ignore")[:500]
                yield (
                    f"event: error\n"
                    f"data: {json.dumps({'status': upstream.status_code, 'detail': detail})}\n\n"
                ).encode()
                return
            try:
                async for chunk in upstream.aiter_bytes():
                    if not chunk:
                        continue
                    yield chunk
                    if asyncio.get_event_loop().time() > lifetime:
                        break
                    if await request.is_disconnected():
                        break
            except httpx.HTTPError:
                pass
        finally:
            try:
                await upstream.aclose()
            except Exception:  # noqa: BLE001
                pass
            await client.aclose()

    return StreamingResponse(
        _sse(), media_type="text/event-stream", headers=headers_out
    )


def _make_handler(template: str):
    """按路径模板(与路由声明同形)构造上游 path。"""

    async def handler(
        request: Request, user: dict[str, Any] = Depends(require_session),
    ):
        path = template
        for key in ("thread_id", "run_id"):
            value = request.path_params.get(key)
            if value is not None:
                path = path.replace("{" + key + "}", value)
        return await _proxy(request, user, path)

    return handler


# ---------------------------------------------------------------------------
# 路由:标准 LangGraph 协议面(SDK useStream 所需)。
# 每个 (method, path) 单独注册:FastAPI 对多方法路由会拆成多条 route
# 复用同一 operation_id,导致 OpenAPI 重复告警。
# ---------------------------------------------------------------------------
_PROTOCOL_ROUTES: list[tuple[str, str, str]] = [
    ("GET", "/threads", "lg_threads_list"),
    ("POST", "/threads", "lg_threads_create"),
    ("POST", "/threads/search", "lg_threads_search"),
    ("GET", "/threads/{thread_id}", "lg_thread_get"),
    ("PATCH", "/threads/{thread_id}", "lg_thread_patch"),
    ("DELETE", "/threads/{thread_id}", "lg_thread_delete"),
    ("GET", "/threads/{thread_id}/state", "lg_thread_state_get"),
    ("POST", "/threads/{thread_id}/state", "lg_thread_state_update"),
    ("GET", "/threads/{thread_id}/history", "lg_thread_history_get"),
    ("POST", "/threads/{thread_id}/history", "lg_thread_history_post"),
    ("GET", "/threads/{thread_id}/runs", "lg_thread_runs_list"),
    ("POST", "/threads/{thread_id}/runs", "lg_thread_runs_create"),
    ("POST", "/threads/{thread_id}/runs/stream", "lg_thread_runs_stream"),
    ("GET", "/threads/{thread_id}/runs/{run_id}", "lg_run_get"),
    ("POST", "/threads/{thread_id}/runs/{run_id}/cancel", "lg_run_cancel"),
    ("POST", "/threads/{thread_id}/stream/events", "lg_thread_stream_events"),
    ("GET", "/threads/{thread_id}/stream", "lg_thread_stream_join"),
    ("POST", "/runs/stream", "lg_runs_stream"),
    ("POST", "/runs", "lg_runs_create"),
]

for _method, _template, _op_id in _PROTOCOL_ROUTES:
    router.add_api_route(
        _template,
        _make_handler(_template),
        methods=[_method],
        operation_id=_op_id,
    )
