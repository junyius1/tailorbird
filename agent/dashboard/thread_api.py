"""线程核心端点(设计书 §5.2,本变体主战场)。

- 列表/详情/删除/状态/resolve/cancel(按 ``metadata.local_user == sub`` 过滤,
  404 归一化:不存在或非本身份 → 一律 404);
- 发消息 = 触发 run(`POST /messages` busy 入队 / idle 409 引导 run.start);
- SDK command 透传 + 富化(`POST /commands` 前端 StreamProvider 主口);
- **有界 state/history 代理**(防超大线程冻结单循环:单快照 15s / history
  ≤5 + 90s + 合并 + 退避 + 降级);
- SSE 透传(v2 事件流 + join 流,客户端断开必须释放上游订阅);
- 推理懒加载(`GET /reasoning`、`GET /reasoning/{message_id}`,只读直查
  Postgres 专表,不进单循环);
- 本地 git diff(`working-tree-diff` / `branch-diff`,纯本地,不依赖 GitHub)。
"""

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..utils.repo import normalize_repo_meta, repo_meta_to_full_name
from ..utils.timing import server_timing_header
from .auth import require_session, require_same_origin_for_mutations
from .runtime_client import langgraph_client, raw_httpx_headers, runtime_url
from .state_cache import state_cache

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/dashboard/api",
    tags=["threads"],
    dependencies=[Depends(require_same_origin_for_mutations)],
)

# ---------------------------------------------------------------------------
# 有界代理的常量(设计书 §5.2,单循环性能事故的防线,数值逐字保留)
# ---------------------------------------------------------------------------
# history 无过滤请求的 limit 强制上限
_DISCOVERY_HISTORY_LIMIT = 5
# history 遍历预算(秒)
_HISTORY_PROXY_TIMEOUT = 90.0
# history 合并阈值:相同 payload 的并发请求合并为一次上游调用
_HISTORY_COALESCING_PRUNE_AT = 32
# history 退避:重试次数 / 基础退避秒数(指数)
_FALLBACK_ATTEMPTS = 3
_FALLBACK_RETRY_BASE_DELAY = 1.5
# 降级快照缓存 TTL(秒)
_FALLBACK_SNAPSHOT_CACHE_TTL_SECONDS = 30.0
# state 单快照读预算(秒)
_STATE_PROXY_TIMEOUT = 15.0
# SSE 空闲超时 / 最大存活(秒,env 可覆盖)
_STREAM_IDLE_TIMEOUT = float(os.environ.get("OPEN_SWE_STREAM_IDLE_TIMEOUT_SECONDS", "60"))
_STREAM_MAX_LIFETIME = float(os.environ.get("OPEN_SWE_STREAM_MAX_LIFETIME_SECONDS", "14400"))
# 消息正文上限
_MAX_CONTENT_LEN = 20000
# 线程 busy 判定的 live run 状态
_LIVE_RUN_STATUSES = ("pending", "running")

# history 合并表:key = "thread_id:json(payload)" → (lock, 请求计数)
_history_inflight: dict[str, asyncio.Event] = {}
# 降级快照缓存:thread_id → (时间, 快照)
_fallback_snapshot_cache: dict[str, tuple[float, dict[str, Any]]] = {}


# ---------------------------------------------------------------------------
# 基础:客户端 / 归属判断 / 404 归一化 / 列表项
# ---------------------------------------------------------------------------


def _client_for(user: dict[str, Any]):
    """带当前用户身份的运行时客户端。"""
    return langgraph_client(user["sub"])


def _is_admin(user: dict[str, Any]) -> bool:
    """是否 admin。"""
    return bool(user.get("is_admin"))


async def _get_thread_or_404(
    thread_id: str, user: dict[str, Any], *, postable: bool = False
) -> dict[str, Any]:
    """取线程并做归属判断;不存在或非本身份 → 404 归一化(不泄漏存在性)。

    参数:
        thread_id: 线程 id。
        user: 当前会话。
        postable: 是否按"可发 run"判定(同 owner 判据,语义占位)。
    """
    client = _client_for(user)
    try:
        thread = await client.threads.get(thread_id)
    except Exception as exc:  # noqa: BLE001
        # SDK 的 404(线程不存在)与其它错误统一归 404
        if "404" in str(exc) or "not found" in str(exc).lower():
            raise HTTPException(status_code=404, detail="not found")
        raise HTTPException(status_code=502, detail="runtime unreachable")
    if thread is None:
        raise HTTPException(status_code=404, detail="not found")
    metadata = thread.get("metadata") or {}
    owner = metadata.get("local_user")
    # admin 可读任意线程;普通用户仅本人
    if owner and not _is_admin(user) and owner != user["sub"]:
        raise HTTPException(status_code=404, detail="not found")
    return thread


def _thread_summary(thread: dict[str, Any], user: dict[str, Any] | None = None) -> dict[str, Any]:
    """Thread 对象 → ThreadListItem(列表/详情元素结构)。

    字段:``id, title, repo, repoFullName, branch, model, effort, planMode,
    planStatus, source, threadCategory, triggerKind, status, viewed,
    viewedAt, createdAt, updatedAt, lastUserMessage?``。
    """
    metadata = thread.get("metadata") or {}
    # repo 展示:name = 末段(路径仓库 = 目录名);full_name = owner/name 或路径
    repo_meta = metadata.get("repo")
    repo_full = repo_meta_to_full_name(repo_meta)
    repo_name = repo_full.rsplit("/", 1)[-1] if repo_full else ""
    # 展示态:由 latest_run_status 推导
    latest = metadata.get("latest_run_status")
    if latest in ("pending", "running"):
        status = "running"
    elif latest == "error" or latest == "timeout":
        status = "error"
    elif latest == "interrupted":
        status = "interrupted"
    else:
        status = "idle"
    return {
        "id": thread.get("thread_id"),
        "title": metadata.get("title") or "(未命名)",
        "repo": repo_name,
        "repoFullName": repo_full,
        "branch": metadata.get("branch"),
        "model": metadata.get("model"),
        "effort": metadata.get("effort"),
        "planMode": bool(metadata.get("plan_mode")),
        "planStatus": metadata.get("plan_status"),
        "source": metadata.get("source") or "web",
        "threadCategory": metadata.get("thread_category") or "interactive",
        "triggerKind": metadata.get("trigger_kind") or "user",
        "automationId": metadata.get("automation_id"),
        "automationName": metadata.get("automation_name"),
        "status": status,
        "viewed": bool(metadata.get("viewed")),
        "viewedAt": metadata.get("viewed_at_ms"),
        "createdAt": thread.get("created_at"),
        "updatedAt": thread.get("updated_at"),
        "lastUserMessage": metadata.get("last_user_message"),
        # transcript 由客户端从 SDK 水合(GET …/state → stream.messages);
        # summary 只带元数据,但前端 AgentThread 类型要求 messages 存在(上游同此)。
        "messages": [],
        "isOwner": bool(user and (metadata.get("local_user") in (None, user["sub"]) or _is_admin(user))),
    }


async def _search_threads(
    user: dict[str, Any], *, limit: int, offset: int, all_users: bool, filters: dict[str, Any]
) -> list[dict[str, Any]]:
    """封装运行时 ``POST /threads/search``(加 local_user 过滤,单用户下隔离)。"""
    client = _client_for(user)
    metadata: dict[str, Any] = dict(filters.get("metadata") or {})
    # 非 admin 的 all=true 无效;普通用户恒按本人过滤
    if not (all_users and _is_admin(user)):
        metadata["local_user"] = user["sub"]
    search_kwargs: dict[str, Any] = {
        "limit": min(max(limit, 1), 100),
        "offset": max(offset, 0),
    }
    if metadata:
        search_kwargs["metadata"] = metadata
    for key in ("status",):
        if filters.get(key):
            search_kwargs[key] = filters[key]
    if filters.get("sort_by"):
        search_kwargs["sort_by"] = filters["sort_by"]
        search_kwargs["sort_order"] = filters.get("sort_order") or "desc"
    try:
        return await client.threads.search(**search_kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("threads.search 失败: %s", exc)
        raise HTTPException(status_code=502, detail="runtime unreachable")


# ---------------------------------------------------------------------------
# 列表 / 详情 / 删除 / resolve / cancel
# ---------------------------------------------------------------------------


@router.get("/threads")
async def list_threads(
    limit: int = Query(50, le=100),
    offset: int = Query(0, ge=0),
    all: bool = Query(False),
    resolved: bool | None = Query(None),
    viewed: bool | None = Query(None),
    source: str | None = Query(None),
    status: str | None = Query(None),
    q: str | None = Query(None),
    user: dict[str, Any] = Depends(require_session),
) -> dict[str, Any]:
    """线程列表(侧栏/首页数据源);按 local_user 过滤,admin all=true 看全量。

    返回:``{"items": [ThreadListItem], "limit", "offset", "hasMore"}``。
    """
    started = perf_counter()
    filters: dict[str, Any] = {"metadata": {}, "status": status}
    if resolved is not None:
        filters["metadata"]["resolved"] = resolved
    if viewed is not None:
        filters["metadata"]["viewed"] = viewed
    if source:
        filters["metadata"]["source"] = source
    threads = await _search_threads(
        user, limit=limit, offset=offset, all_users=all, filters=filters
    )
    # q 全文搜索:metadata 过滤后在内存里按 title 模糊筛(本地变体够用)
    if q:
        needle = q.lower()
        threads = [t for t in threads if needle in str((t.get("metadata") or {}).get("title", "")).lower()]
    items = [_thread_summary(t, user) for t in threads]
    return JSONResponse(
        content={
            "items": items,
            "limit": limit,
            "offset": offset,
            "hasMore": len(items) == limit,
        },
        headers={"Server-Timing": server_timing_header((perf_counter() - started) * 1000)},
    )


@router.get("/threads/page")
async def list_threads_page(
    limit: int = Query(25, le=100),
    offset: int = Query(0, ge=0),
    all: bool = Query(False),
    resolved: bool | None = Query(None),
    viewed: bool | None = Query(None),
    source: str | None = Query(None),
    status: str | None = Query(None),
    q: str | None = Query(None),
    user: dict[str, Any] = Depends(require_session),
) -> dict[str, Any]:
    """分页列表变体(契约同 GET /threads)。"""
    return await list_threads(
        limit=limit,
        offset=offset,
        all=all,
        resolved=resolved,
        viewed=viewed,
        source=source,
        status=status,
        q=q,
        user=user,
    )


@router.get("/threads/sidebar")
async def sidebar_threads(
    user: dict[str, Any] = Depends(require_session)
) -> dict[str, Any]:
    """侧栏精简列表(active/resolved 两段;底层同一 search)。"""
    started = perf_counter()
    active = await _search_threads(
        user, limit=50, offset=0, all_users=False,
        filters={"metadata": {"resolved": False}},
    )
    resolved = await _search_threads(
        user, limit=50, offset=0, all_users=False,
        filters={"metadata": {"resolved": True}},
    )
    return JSONResponse(
        content={
            "active": [_thread_summary(t, user) for t in active],
            "resolved": [_thread_summary(t, user) for t in resolved],
        },
        headers={"Server-Timing": server_timing_header((perf_counter() - started) * 1000)},
    )


@router.get("/threads/{thread_id}")
async def get_thread(
    thread_id: str,
    mark_viewed: bool = Query(True),
    user: dict[str, Any] = Depends(require_session),
) -> dict[str, Any]:
    """线程详情元数据(transcript 不在这——前端另调 /state hydrate)。

    ``mark_viewed=true``(默认)顺带置已读。404 归一化(不存在或非本身份)。
    """
    thread = await _get_thread_or_404(thread_id, user)
    metadata = thread.get("metadata") or {}
    if mark_viewed and not metadata.get("viewed"):
        try:
            client = _client_for(user)
            await client.threads.update(thread_id, metadata={"viewed": True, "viewed_at_ms": int(perf_counter() * 1000)})
        except Exception:  # noqa: BLE001 —— 置读失败不阻断详情
            pass
    summary = _thread_summary(thread, user)
    summary["isOwner"] = (metadata.get("local_user") == user["sub"]) or _is_admin(user)
    return summary


@router.delete("/threads/{thread_id}", status_code=204)
async def delete_thread(
    thread_id: str, user: dict[str, Any] = Depends(require_session)
) -> Response:
    """删线程 + 其 checkpoint/流 + llm_reasoning 该线程全部行(不可恢复)。"""
    thread = await _get_thread_or_404(thread_id, user)
    # best-effort 清理沙箱容器(删线程时)
    from ..integrations.docker import DockerSandbox, _container_name

    if (os.environ.get("SANDBOX_TYPE", "docker") == "docker"):
        try:
            DockerSandbox(thread_id).destroy()
        except Exception:  # noqa: BLE001
            logger.debug("删线程时清理沙箱失败(best-effort)", exc_info=True)
    # 级联删推理行(设计书 §5.2.1)
    from ..reasoning_store import delete_thread as delete_reasoning

    try:
        await delete_reasoning(thread_id)
    except Exception:  # noqa: BLE001
        logger.warning("删线程时清理 llm_reasoning 失败", exc_info=True)
    client = _client_for(user)
    try:
        await client.threads.delete(thread_id)
    except Exception as exc:  # noqa: BLE001
        if "404" in str(exc) or "not found" in str(exc).lower():
            raise HTTPException(status_code=404, detail="not found")
        raise HTTPException(status_code=502, detail="runtime unreachable")
    # 失效 state 缓存
    state_cache().invalidate(thread_id)
    _ = _container_name
    return Response(status_code=204)


class ResolveBody(BaseModel):
    """resolve 请求体。"""

    # 是否标记为已解决
    resolved: bool


@router.post("/threads/{thread_id}/resolve")
async def resolve_thread(
    thread_id: str, body: ResolveBody, user: dict[str, Any] = Depends(require_session)
) -> dict[str, Any]:
    """标记线程已解决/未解决(列表分组)。"""
    thread = await _get_thread_or_404(thread_id, user)
    client = _client_for(user)
    try:
        updated = await client.threads.update(
            thread_id,
            metadata={
                "resolved": body.resolved,
                "resolved_at_ms": int(perf_counter() * 1000) if body.resolved else None,
            },
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail="runtime unreachable")
    return _thread_summary(updated or thread, user)


@router.post("/threads/{thread_id}/cancel")
async def cancel_thread(
    thread_id: str, user: dict[str, Any] = Depends(require_session)
) -> dict[str, Any]:
    """取消线程全部活动 run(interrupt 语义;幂等:无活动 run 也 200)。"""
    thread = await _get_thread_or_404(thread_id, user)
    client = _client_for(user)
    cancelled = False
    try:
        runs = await client.runs.list(thread_id)
    except Exception:  # noqa: BLE001
        runs = []
    for run in runs or []:
        run_status = run.get("status")
        run_id = run.get("run_id")
        if run_status in _LIVE_RUN_STATUSES and run_id:
            try:
                await client.runs.cancel(thread_id, run_id, action="interrupt")
                cancelled = True
            except Exception:  # noqa: BLE001
                logger.debug("cancel run 失败(%s)", run_id, exc_info=True)
    if cancelled:
        try:
            await client.threads.update(thread_id, metadata={"latest_run_status": "interrupted"})
        except Exception:  # noqa: BLE001
            pass
    summary = _thread_summary(thread, user)
    summary["status"] = "interrupted" if cancelled else summary["status"]
    return summary


# ---------------------------------------------------------------------------
# 发消息(busy 入队 / idle 409)与判活
# ---------------------------------------------------------------------------


async def get_thread_active_status(thread_id: str, user: dict[str, Any]) -> bool | None:
    """线程是否"活跃"(有 live run)。

    语义(设计书 §5.2):
    - True:``thread.status == busy`` 且有 live pending/running run;
    - False:busy 但无 live run(僵尸,如 dev server 重启残留)→ 允许开新 run;
    - None:判活抛异常(调用方 502)。
    """
    client = _client_for(user)
    try:
        thread = await client.threads.get(thread_id)
        runs = await client.runs.list(thread_id)
    except Exception:  # noqa: BLE001
        return None
    is_busy = (thread or {}).get("status") == "busy"
    has_live = any(r.get("status") in _LIVE_RUN_STATUSES for r in (runs or []))
    return bool(is_busy and has_live)


class ImagePart(BaseModel):
    """图片部分(base64)。"""

    # base64 数据
    data: str
    # MIME 类型
    media_type: str
    # 文件名(可空)
    filename: str | None = None


class ThreadMessageBody(BaseModel):
    """发消息请求体(触发 run 的用户主动交互入口)。"""

    # 正文(≤20000)
    content: str = Field(min_length=1, max_length=_MAX_CONTENT_LEN)
    # 图片(可空)
    images: list[ImagePart] | None = None
    # 模型覆盖(可空)
    model_id: str | None = None
    # 推理力度(可空)
    effort: str | None = None
    # plan mode 开关
    plan_mode: bool = False


@router.post("/threads/{thread_id}/messages")
async def send_message(
    thread_id: str,
    body: ThreadMessageBody,
    request: Request,
    user: dict[str, Any] = Depends(require_session),
) -> dict[str, Any]:
    """发消息(busy → 入 store 队列;idle → 409 引导 run.start;无法判活 → 502)。

    流程(设计书 §5.2 send_dashboard_message):
    1. 取线程 → 404 归一化;
    2. 更新 metadata(source/plan_mode/model/participant_logins/resolved→False);
    3. 判活:None→502;不活跃→409;
    4. 活跃 → 消息写入 store 队列,由 check_message_queue 中间件在下一次
       model call 前注入(FIFO,上限 100)。
    """
    await require_same_origin_for_mutations(request)
    thread = await _get_thread_or_404(thread_id, user, postable=True)
    client = _client_for(user)
    metadata = thread.get("metadata") or {}
    # 2) metadata 更新
    new_metadata: dict[str, Any] = {
        "source": "dashboard",
        "plan_mode": body.plan_mode,
        "resolved": False,
        "local_user": metadata.get("local_user") or user["sub"],
        "last_user_message": body.content[:200],
    }
    if body.model_id:
        new_metadata["model"] = body.model_id
    if body.effort:
        new_metadata["effort"] = body.effort
    participants = list(metadata.get("participant_logins") or [])
    if user["sub"] not in participants:
        participants.append(user["sub"])
    new_metadata["participant_logins"] = participants
    try:
        await client.threads.update(thread_id, metadata=new_metadata)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail="runtime unreachable")
    # 3) 判活
    active = await get_thread_active_status(thread_id, user)
    if active is None:
        raise HTTPException(status_code=502, detail="runtime unreachable")
    if not active:
        # idle:引导走 /commands 的 run.start(lazy 建线程/开新 run)
        raise HTTPException(
            status_code=409,
            detail="thread is idle; start a run via the stream commands endpoint",
        )
    # 4) busy:入队
    content: str | list[dict[str, Any]] = body.content
    if body.images:
        content = [{"type": "text", "text": body.content}] + [
            {"type": "image", "data": img.data, "media_type": img.media_type}
            for img in body.images
        ]
    try:
        item = await client.store.get_item(("queue", thread_id), "pending_messages")
        existing = (item.value if not isinstance(item, dict) else item.get("value")) or {}
        messages = list(existing.get("messages") or [])
    except Exception:  # noqa: BLE001
        messages = []
    messages.append({"text": body.content, "images": body.images or []})
    # FIFO 上限 100,超限丢最旧
    messages = messages[-100:]
    await client.store.put_item(
        ("queue", thread_id), "pending_messages", {"messages": messages}
    )
    updated = await _get_thread_or_404(thread_id, user)
    return _thread_summary(updated, user)


# ---------------------------------------------------------------------------
# SDK command 透传 + 富化(前端 StreamProvider 主口)
# ---------------------------------------------------------------------------

# dashboard 的 v2 run 流通道(REST 不接受 tools/lifecycle,由 v2 标记隐式启用)
_DASHBOARD_STREAM_MODES = ["values", "updates", "messages", "custom", "tasks", "checkpoints"]
# 服务端补的命令 id 单调计数(UI 未带 id 时)
_command_id_counter = 0
# v3 流协议标记(镜像 langgraph_api 的 key,不 import)
_EVENT_STREAMING_V2_KEY = "__event_streaming_v2"


def _enrich_run_start(
    params: dict[str, Any],
    user: dict[str, Any],
    thread_id: str,
    creating: bool,
    existing_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """富化 run.start:注入 configurable + metadata(source=dashboard)。

    已有线程且本次未带 repo 时,回退线程创建时的 ``metadata.repo``
    (沙箱缓存丢失重建时仍能挂回同一仓库)。
    """
    import uuid as _uuid

    params = dict(params)
    config = dict(params.get("config") or {})
    configurable = dict(config.get("configurable") or {})
    # 触发源 = dashboard(§15.4.3)
    configurable["source"] = "dashboard"
    configurable["local_user"] = user["sub"]
    # repo 归一化:UI 传 full_name/本地路径字符串 → dict 元信息
    # (路径仓库带 local_path,沙箱挂载直达该目录;§11)
    repo_meta = normalize_repo_meta(configurable.get("repo"))
    if repo_meta is None and not creating:
        stored = (existing_metadata or {}).get("repo")
        repo_meta = normalize_repo_meta(stored if isinstance(stored, dict) else None)
    if repo_meta is not None:
        configurable["repo"] = repo_meta
    # prepare_run_id:关联 PrepareRun 中间件(每次 run 新 uuid)
    configurable.setdefault("prepare_run_id", _uuid.uuid4().hex)
    # 必须置位:v3 流协议标记
    configurable[_EVENT_STREAMING_V2_KEY] = True
    config["configurable"] = configurable
    params["config"] = config
    # assistant 固定 agent
    params["assistant_id"] = "agent"
    # v2 全通道 + resumable(晚连接可重放)
    params.setdefault("stream_mode", list(_DASHBOARD_STREAM_MODES))
    params["stream_resumable"] = True
    params.setdefault("multitask_strategy", "interrupt")
    # 新建线程时打全量 metadata(owner/title/repo/plan_mode)
    if creating:
        metadata = dict(params.get("metadata") or {})
        metadata.setdefault("kind", "agent")
        metadata.setdefault("source", "dashboard")
        metadata.setdefault("local_user", user["sub"])
        metadata.setdefault("thread_category", "interactive")
        metadata.setdefault("trigger_kind", "user")
        # 缺省未解决:侧栏/列表按 resolved 分组,缺 key 的线程两边都查不到
        metadata.setdefault("resolved", False)
        # 标题:首条消息前 80 字符
        title = ""
        input_messages = (params.get("input") or {}).get("messages") or []
        if input_messages:
            first = input_messages[0]
            fc = first.get("content") if isinstance(first, dict) else None
            if isinstance(fc, str):
                title = fc[:80]
            elif isinstance(fc, list):
                for block in fc:
                    if isinstance(block, dict) and block.get("type") == "text":
                        title = str(block.get("text", ""))[:80]
                        break
        metadata.setdefault("title", title or "(未命名)")
        # 线程稳定分支:local/<slug>(slug = 标题 slug 化)
        slug = re.sub(r"[^a-z0-9]+", "-", (title or "thread").lower()).strip("-")[:40] or "thread"
        metadata.setdefault("branch", f"local/{slug}")
        metadata.setdefault("base_branch", "main")
        # 本次显式选择了仓库(含"添加路径")→ 覆盖默认
        if repo_meta is not None:
            metadata["repo"] = repo_meta
        params["metadata"] = metadata
    # 已有线程:configurable 已归一化(或回退 metadata.repo),metadata 保持创建时形态
    return params


@router.post("/threads/{thread_id}/commands")
async def proxy_commands(
    thread_id: str,
    request: Request,
    user: dict[str, Any] = Depends(require_session),
) -> JSONResponse:
    """SDK command 信封透传 + 服务端富化(§15.4.4)。

    - ``run.start`` 且线程不存在 → lazy 建线程(打全量 metadata);
    - 富化 configurable + metadata.source="dashboard";
    - 运行时 ``{"type":"success","run_id"?}`` → 200 原样透传;
      ``{"type":"error",…}`` → 200 + 同体透传(业务错在体里,前端按 type 判);
    - 线程不存在且 method≠run.start → 404;非 JSON → 415;运行时不可达 → 502。
    """
    await require_same_origin_for_mutations(request)
    content_type = request.headers.get("content-type", "")
    if "application/json" not in content_type:
        raise HTTPException(status_code=415, detail="unsupported media type")
    try:
        envelope = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=415, detail="unsupported media type")
    if not isinstance(envelope, dict):
        raise HTTPException(status_code=400, detail="envelope must be an object")
    method = envelope.get("method") or ""
    # 运行时协议要求整数 id(请求/响应关联);UI 未带时服务端补(单调计数)
    global _command_id_counter
    if not isinstance(envelope.get("id"), int):
        _command_id_counter += 1
        envelope["id"] = _command_id_counter
    params = dict(envelope.get("params") or {})
    client = _client_for(user)
    # 线程是否存在(run.start 可 lazy 建)
    creating = False
    existing_metadata: dict[str, Any] = {}
    try:
        existing = await client.threads.get(thread_id)
        if existing is None:
            if method != "run.start":
                raise HTTPException(status_code=404, detail="not found")
            creating = True
        else:
            # 归属判断(非 run.start 必须已存在且可访问)
            metadata = existing.get("metadata") or {}
            existing_metadata = dict(metadata)
            owner = metadata.get("local_user")
            if owner and not _is_admin(user) and owner != user["sub"]:
                raise HTTPException(status_code=404, detail="not found")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        if "404" in str(exc) or "not found" in str(exc).lower():
            if method != "run.start":
                raise HTTPException(status_code=404, detail="not found")
            creating = True
        else:
            raise HTTPException(status_code=502, detail="runtime unreachable")
    if method == "run.start":
        params = _enrich_run_start(
            params, user, thread_id, creating, existing_metadata
        )
    # 透传到运行时同名端点(SDK 无 commands 命名空间,走 raw httpx + 身份头)
    import httpx

    body = dict(envelope)
    body["params"] = params
    url = f"{runtime_url()}/threads/{thread_id}/commands"
    try:
        async with httpx.AsyncClient(timeout=60) as http:
            resp = await http.post(
                url,
                content=json.dumps(body).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    **raw_httpx_headers(user["sub"]),
                },
            )
            result = json.loads(resp.content) if resp.content else {}
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=502, detail="runtime unreachable")
    # run.start 成功 → 异步更新 latest_run 元 + 失效缓存
    if method == "run.start" and isinstance(result, dict) and result.get("type") == "success":
        run_id = result.get("run_id")
        if run_id:
            try:
                await client.threads.update(
                    thread_id,
                    metadata={"latest_run_status": "pending", "latest_run_id": run_id},
                )
            except Exception:  # noqa: BLE001
                pass
        state_cache().invalidate(thread_id)
    # 业务错(200 体)与成功(200)都原样透传
    return JSONResponse(content=result if isinstance(result, dict) else {"type": "success"})


# ---------------------------------------------------------------------------
# 有界 state / history 代理(主 transcript 读取;防超大线程冻结单循环)
# ---------------------------------------------------------------------------


async def _refresh_latest_run_metadata(thread_id: str, user: dict[str, Any]) -> dict[str, Any]:
    """查 runs 列表拿 latest_run_status/id,与 metadata 不同则回写(denormalize)。"""
    client = _client_for(user)
    try:
        thread = await client.threads.get(thread_id)
        runs = await client.runs.list(thread_id, limit=1)
    except Exception:  # noqa: BLE001
        return {}
    metadata = (thread or {}).get("metadata") or {}
    latest = runs[0] if runs else None
    new_status = latest.get("status") if latest else None
    new_run_id = latest.get("run_id") if latest else None
    changed = (
        metadata.get("latest_run_status") != new_status
        or (new_run_id and metadata.get("latest_run_id") != new_run_id)
    )
    if changed:
        patch: dict[str, Any] = {}
        if new_status:
            patch["latest_run_status"] = new_status
        if new_run_id:
            patch["latest_run_id"] = new_run_id
        # 展示态同步
        if new_status in ("pending", "running"):
            patch["status"] = "running"
        elif new_status in ("error", "timeout"):
            patch["status"] = "error"
        elif new_status == "interrupted":
            patch["status"] = "interrupted"
        elif new_status == "success":
            patch["status"] = "idle"
        try:
            await client.threads.update(thread_id, metadata=patch)
        except Exception:  # noqa: BLE001
            pass
        metadata.update(patch)
    return metadata


def _run_in_flight(thread: dict[str, Any], metadata: dict[str, Any]) -> bool:
    """是否 run 进行中(busy 或 latest run pending/running)。"""
    if thread.get("status") == "busy":
        return True
    latest = metadata.get("latest_run_status")
    return latest in _LIVE_RUN_STATUSES


@router.get("/threads/{thread_id}/state")
async def get_state(
    thread_id: str, user: dict[str, Any] = Depends(require_session)
) -> JSONResponse:
    """有界 state 代理(主 transcript 唯一来源)。

    - 归一化:线程存在但从未 run → ``{"values": {}, "next": []}``(200 非 404);
    - run-in-flight → 失效缓存 + 直读 + 删结果 next(让 SDK 认为活跃);
    - idle → 先查 state_cache(命中不进运行时单循环);miss → 15s 预算直读;
      超时 → 降级为线程记录上已有 denormalized values(而非 500)。
    """
    thread = await _get_thread_or_404(thread_id, user)
    client = _client_for(user)
    metadata = await _refresh_latest_run_metadata(thread_id, user)
    thread = dict(thread)
    thread["metadata"] = metadata
    in_flight = _run_in_flight(thread, metadata)
    cache = state_cache()
    if in_flight:
        cache.invalidate(thread_id)
    else:
        # idle:版本命中缓存则直接返回(不进单循环)
        version = str(thread.get("state_updated_at") or thread.get("updated_at") or "")
        if version:
            hit = cache.get(thread_id, version)
            if hit is not None:
                return JSONResponse(content=hit)
    # 直读单快照(15s 预算)
    try:
        state = await asyncio.wait_for(
            client.threads.get_state(thread_id), timeout=_STATE_PROXY_TIMEOUT
        )
    except (TimeoutError, asyncio.TimeoutError):
        # 超时降级:用线程记录上已有的 denormalized values
        values = metadata.get("values")
        if not isinstance(values, dict):
            values = {}
        return JSONResponse(
            content={
                "values": values,
                "next": [],
                "configurable": {"thread_id": thread_id},
                "metadata": metadata,
            }
        )
    except Exception as exc:  # noqa: BLE001
        if "404" in str(exc) or "not found" in str(exc).lower():
            raise HTTPException(status_code=404, detail="not found")
        if "5" in str(exc)[:3]:
            raise HTTPException(status_code=502, detail="runtime unreachable")
        raise HTTPException(status_code=502, detail="runtime unreachable")
    # 归一化:从未 run 的空快照
    if not isinstance(state, dict):
        state = {}
    values = state.get("values")
    if values is None:
        state["values"] = {}
    state.setdefault("next", [])
    if in_flight:
        # run 进行中:删掉 next,SDK useStream 认为活跃并开 live 订阅
        state.pop("next", None)
    else:
        # idle:缓存结果(版本=state_updated_at/updated_at)
        version = str(thread.get("state_updated_at") or thread.get("updated_at") or "")
        if version:
            cache.put(thread_id, version, state)
    return JSONResponse(content=state)


async def _upstream_history(
    client, thread_id: str, payload: dict[str, Any], budget: float
) -> list[dict[str, Any]]:
    """带预算的上游 history 读取。"""
    return await asyncio.wait_for(
        client.threads.get_history(thread_id, **payload), timeout=budget
    )


def _clamp_history_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """无过滤请求 limit 强制 ≤5(发现/跳转用,禁止全量翻页)。"""
    payload = dict(payload)
    has_filter = any(k in payload for k in ("before", "after", "checkpoint", "metadata"))
    if not has_filter:
        limit = payload.get("limit")
        if not isinstance(limit, int) or limit > _DISCOVERY_HISTORY_LIMIT:
            payload["limit"] = _DISCOVERY_HISTORY_LIMIT
    return payload


@router.post("/threads/{thread_id}/history")
async def proxy_history(
    thread_id: str, request: Request, user: dict[str, Any] = Depends(require_session)
) -> JSONResponse:
    """有界 history 代理(三层防御:有界 + 合并 + 退避)。

    - 无过滤 limit ≤5;90s 预算;
    - 超时退避:``{limit:1}`` 重试 3 次×1.5s(指数)→ 仍败降级 /state 单条
      (同 history 元素形状,客户端无感;30s 快照缓存去重);
    - 退避耗尽仍败 → 503 ``thread history request timed out; retry``。
    """
    await require_same_origin_for_mutations(request)
    content_type = request.headers.get("content-type", "")
    if "application/json" not in content_type:
        raise HTTPException(status_code=415, detail="unsupported media type")
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload = _clamp_history_payload(payload)
    client = _client_for(user)
    # 先确认线程存在(404 归一化)
    await _get_thread_or_404(thread_id, user)
    # 合并:相同 payload 的并发请求合并为一次上游调用(asyncio.Event 共享)。
    # 单事件循环下,第一个进入者持锁计算并 set,其余等待后读共享结果。
    coalesce_key = f"{thread_id}:{json.dumps(payload, sort_keys=True, default=str)}"
    entry = _history_inflight.get(coalesce_key)
    if entry is None:
        entry = {"event": asyncio.Event(), "result": None, "done": False}
        _history_inflight[coalesce_key] = entry
        is_leader = True
    else:
        is_leader = False
    if not is_leader:
        await entry["event"].wait()
        if entry.get("result") is not None:
            return JSONResponse(content=entry["result"])
        # 领导者失败且无共享结果:本请求自算一次(退避后一般能过)
        is_leader = True
    try:
        try:
            items = await _upstream_history(client, thread_id, payload, _HISTORY_PROXY_TIMEOUT)
            result: Any = list(items)
        except (TimeoutError, asyncio.TimeoutError):
            result = await _history_fallback(client, thread_id, user)
        entry["result"] = result
        return JSONResponse(content=result)
    finally:
        entry["done"] = True
        entry["event"].set()
        # 合并表上限(防泄漏):超阈值清理已完成项
        if len(_history_inflight) > _HISTORY_COALESCING_PRUNE_AT:
            for key in [k for k, v in _history_inflight.items() if v.get("done")]:
                _history_inflight.pop(key, None)


async def _history_fallback(
    client, thread_id: str, user: dict[str, Any]
) -> list[dict[str, Any]]:
    """history 退避:先 {limit:1} 重试(指数 1.5s),仍败降级 /state 单条。"""
    # 1) {limit:1} 重试
    for attempt in range(_FALLBACK_ATTEMPTS):
        delay = _FALLBACK_RETRY_BASE_DELAY * (2 ** attempt)
        await asyncio.sleep(delay)
        try:
            items = await asyncio.wait_for(
                client.threads.get_history(thread_id, limit=1),
                timeout=_HISTORY_PROXY_TIMEOUT,
            )
            if items:
                return items
        except (TimeoutError, asyncio.TimeoutError):
            continue
        except Exception:  # noqa: BLE001
            continue
    # 2) 降级 /state 单快照,包成 1 条 history 元素(30s 缓存去重)
    now = time.time()
    cached = _fallback_snapshot_cache.get(thread_id)
    if cached and now - cached[0] < _FALLBACK_SNAPSHOT_CACHE_TTL_SECONDS:
        return cached[1]
    try:
        state = await asyncio.wait_for(
            client.threads.get_state(thread_id), timeout=_STATE_PROXY_TIMEOUT
        )
    except Exception:  # noqa: BLE001
        state = {}
    values = state.get("values") if isinstance(state, dict) else None
    snapshot = [
        {
            "checkpoint_id": (state or {}).get("configurable", {}).get("checkpoint_id")
            if isinstance(state, dict)
            else None,
            "values": values or {},
            "metadata": (state or {}).get("metadata", {}) if isinstance(state, dict) else {},
            "timestamp": None,
            "next": [],
        }
    ]
    _fallback_snapshot_cache[thread_id] = (now, snapshot)
    return snapshot


# ---------------------------------------------------------------------------
# SSE 流代理(透传 + 客户端断开必须释放上游订阅)
# ---------------------------------------------------------------------------


async def _sse_upstream(
    thread_id: str,
    user: dict[str, Any],
    request: Request,
    join: bool,
) -> StreamingResponse:
    """上游 SSE 透传:逐 chunk 回传;客户端断开/空闲超时/最大存活 → 释放上游。"""
    import httpx

    headers = raw_httpx_headers(user["sub"])
    base = runtime_url()
    if join:
        url = f"{base}/threads/{thread_id}/stream"
        method = "GET"
        last_event_id = request.headers.get("last-event-id")
        params = {"last_event_id": last_event_id} if last_event_id else None
    else:
        url = f"{base}/threads/{thread_id}/stream/events"
        method = "POST"
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        params = None

    async def _generate():
        deadline_start = asyncio.get_event_loop().time()
        client = httpx.AsyncClient(timeout=None, headers=headers)
        read_task: asyncio.Task | None = None
        disconnect_task: asyncio.Task | None = None
        try:
            # join 形态:GET 流;events 形态:POST 流
            if join:
                upstream = client.stream(method, url, params=params)
            else:
                upstream = client.stream(method, url, json=body)
            response = await upstream.__aenter__()
            if response.status_code >= 400:
                detail = (await response.aread()).decode("utf-8", errors="ignore")[:500]
                yield f"event: error\ndata: {json.dumps({'status': response.status_code, 'detail': detail})}\n\n"
                return
            # 断线监控:消费 ASGI receive(http.disconnect)
            async def _watch_disconnect() -> None:
                msg = await request.receive()
                if msg.get("type") == "http.disconnect":
                    return

            disconnect_task = asyncio.ensure_future(_watch_disconnect())
            # 逐 chunk 读,受空闲/存活上限约束。
            # 注意:aiter_bytes() 只能调用一次(httpx 流式响应一次性消费,
            # 二次调用抛 StreamConsumed),必须在循环外创建迭代器;超时/断连
            # 通过取消 read_task 实现,而不是丢弃半消费的迭代器。
            chunks = response.aiter_bytes()
            while True:
                remaining_lifetime = (
                    deadline_start + _STREAM_MAX_LIFETIME - asyncio.get_event_loop().time()
                )
                budget = max(0.1, min(_STREAM_IDLE_TIMEOUT, remaining_lifetime))
                read_task = asyncio.ensure_future(anext(chunks))
                try:
                    chunk = await asyncio.wait_for(read_task, timeout=budget)
                except StopAsyncIteration:
                    break  # 上游正常结束(run 终止帧后关流)
                except (TimeoutError, asyncio.TimeoutError):
                    break  # 空闲/存活超时
                except httpx.HTTPError as exc:
                    read_task.cancel()
                    yield f"event: error\ndata: {json.dumps({'detail': str(exc)[:300]})}\n\n"
                    break
                if not chunk:
                    break
                yield chunk
                # 客户端断开检查
                if disconnect_task.done():
                    read_task.cancel()
                    break
        except httpx.HTTPError as exc:
            yield f"event: error\ndata: {json.dumps({'detail': str(exc)[:300]})}\n\n"
        finally:
            # 客户端断开/超时/结束:必须取消并释放上游订阅(否则泄漏事件循环)
            for task in (read_task, disconnect_task):
                if task is not None and not task.done():
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
            try:
                await upstream.aclose()
            except Exception:  # noqa: BLE001
                pass
            await client.aclose()

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/threads/{thread_id}/stream")
async def stream_join(
    thread_id: str,
    request: Request,
    user: dict[str, Any] = Depends(require_session),
) -> StreamingResponse:
    """SSE join 线程流(带 last-event-id 头重放)。404 先于流校验。"""
    await _get_thread_or_404(thread_id, user)
    return await _sse_upstream(thread_id, user, request, join=True)


@router.post("/threads/{thread_id}/stream/events")
async def stream_events(
    thread_id: str,
    request: Request,
    user: dict[str, Any] = Depends(require_session),
) -> StreamingResponse:
    """SSE 透传 v2 事件流(前端 StreamProvider 主口;晚连接可见历史事件)。"""
    await _get_thread_or_404(thread_id, user)
    return await _sse_upstream(thread_id, user, request, join=False)


# ---------------------------------------------------------------------------
# 推理(reasoning)懒加载(§5.2.1):只读短连接直查 Postgres 专表
# ---------------------------------------------------------------------------


@router.get("/threads/{thread_id}/reasoning")
async def list_reasoning(
    thread_id: str, user: dict[str, Any] = Depends(require_session)
) -> list[dict[str, Any]]:
    """该线程推理清单(不含正文;按 created_at 升序,limit 1000)。

    归属校验先于查表(防跨用户枚举):threads.get 判存在 + local_user。
    """
    await _get_thread_or_404(thread_id, user)
    from ..reasoning_store import list_thread

    rows = await list_thread(thread_id)
    return [
        {
            "message_id": r.get("message_id"),
            "run_id": r.get("run_id"),
            "model": r.get("model"),
            "created_at": str(r.get("created_at")) if r.get("created_at") else None,
            "content_length": r.get("content_length"),
        }
        for r in rows
    ]


@router.get("/threads/{thread_id}/reasoning/{message_id}")
async def get_reasoning_one(
    thread_id: str,
    message_id: str,
    user: dict[str, Any] = Depends(require_session),
) -> dict[str, Any]:
    """单条推理正文(404 = 线程不存在/非本身份/该消息无推理)。

    无推理的 AI 消息点 > 也走此路径 → 404,UI 显示"无推理记录"。
    """
    await _get_thread_or_404(thread_id, user)
    from ..reasoning_store import get_one

    row = await get_one(thread_id, message_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return {"message_id": row.get("message_id"), "content": row.get("content") or ""}


# ---------------------------------------------------------------------------
# 本地 git diff(纯本地能力,不依赖 GitHub;设计书 §5.8)
# ---------------------------------------------------------------------------


def _sandbox_host_repo(thread_id: str, repo: dict | None = None) -> str | None:
    """线程 → 宿主仓库路径(docker provider 的 bind mount 源)。"""
    from ..utils.sandbox import sandbox_type

    if sandbox_type() == "docker":
        from ..integrations.docker import _repo_mount_point

        _workdir, host = _repo_mount_point(repo)
        return host
    if sandbox_type() == "local":
        from ..integrations.local import _thread_root

        return _thread_root(thread_id)
    return None


def _run_git(repo_dir: str, *args: str) -> str:
    """在仓库目录跑 git 命令,返回 stdout(失败返回空)。"""
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "-C", repo_dir, *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return proc.stdout or ""
    except Exception:  # noqa: BLE001
        return ""


@router.get("/threads/{thread_id}/working-tree-diff")
async def working_tree_diff(
    thread_id: str, user: dict[str, Any] = Depends(require_session)
) -> dict[str, Any]:
    """工作树 diff(未提交改动,turn 粒度)。无 git 仓 → 空 diff。"""
    thread = await _get_thread_or_404(thread_id, user)
    repo = (thread.get("metadata") or {}).get("repo")
    repo_dir = _sandbox_host_repo(thread_id, repo if isinstance(repo, dict) else None)
    if repo_dir is None or not os.path.isdir(os.path.join(repo_dir, ".git")):
        return {"diff": "", "branch": None, "status": "missing"}
    diff = _run_git(repo_dir, "diff")
    branch = _run_git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD").strip()
    return {"diff": diff, "branch": branch or None}


@router.get("/threads/{thread_id}/branch-diff")
async def branch_diff(
    thread_id: str, user: dict[str, Any] = Depends(require_session)
) -> dict[str, Any]:
    """分支 vs base 的 diff(相对 base 的已提交改动)。

    返回:``{"branch", "baseRef", "headSha", "baseSha", "truncated",
    "files": [ThreadDiffFile]}``(去 prNumber,本地化语义)。
    """
    thread = await _get_thread_or_404(thread_id, user)
    metadata = thread.get("metadata") or {}
    repo_meta = metadata.get("repo")
    repo_dir = _sandbox_host_repo(thread_id, repo_meta if isinstance(repo_meta, dict) else None)
    branch = metadata.get("branch")
    base_ref = metadata.get("base_branch") or "main"
    if repo_dir is None or not os.path.isdir(os.path.join(repo_dir, ".git")):
        return {
            "branch": branch,
            "baseRef": base_ref,
            "baseSha": None,
            "headSha": None,
            "truncated": False,
            "files": [],
        }
    head_sha = _run_git(repo_dir, "rev-parse", "HEAD").strip() or None
    # base 解析:base_ref 存在则用之,否则 HEAD 祖先(本地无远端时退化为空 diff)
    base_sha = _run_git(repo_dir, "rev-parse", base_ref).strip() or None
    files: list[dict[str, Any]] = []
    if head_sha and base_sha and head_sha != base_sha:
        name_status = _run_git(repo_dir, "diff", "--name-status", base_sha, head_sha)
        numstat = _run_git(repo_dir, "diff", "--numstat", base_sha, head_sha)
        stat_map: dict[str, tuple[str, str]] = {}
        for line in numstat.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                stat_map[parts[2]] = (parts[0], parts[1])
        for line in name_status.splitlines():
            cols = line.split("\t")
            if not cols:
                continue
            status = cols[0][0] if cols else "?"
            path = cols[-1]
            additions, deletions = stat_map.get(path, ("0", "0"))
            files.append(
                {
                    "path": path,
                    "status": status,
                    "additions": int(additions) if additions.isdigit() else 0,
                    "deletions": int(deletions) if deletions.isdigit() else 0,
                }
            )
        if len(files) > 200:
            files = files[:200]
    return {
        "branch": branch,
        "baseRef": base_ref,
        "baseSha": base_sha,
        "headSha": head_sha,
        "truncated": len(files) >= 200,
        "files": files,
    }


# ---------------------------------------------------------------------------
# admin 跨线程取消(多用户 admin 能力,§3.4)
# ---------------------------------------------------------------------------


@router.post("/admin/threads/{thread_id}/cancel")
async def admin_cancel_thread(
    thread_id: str, user: dict[str, Any] = Depends(require_session)
) -> dict[str, Any]:
    """admin 取消任意线程的活动 run(跨线程管理面)。"""
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="forbidden")
    # admin 取消:用机器身份直读(绕过归属)
    client = langgraph_client(user["sub"])
    cancelled = False
    try:
        runs = await client.runs.list(thread_id)
    except Exception:  # noqa: BLE001
        runs = []
    for run in runs or []:
        if run.get("status") in _LIVE_RUN_STATUSES and run.get("run_id"):
            try:
                await client.runs.cancel(thread_id, run["run_id"], action="interrupt")
                cancelled = True
            except Exception:  # noqa: BLE001
                logger.debug("admin cancel run 失败", exc_info=True)
    return {"cancelled": cancelled}
