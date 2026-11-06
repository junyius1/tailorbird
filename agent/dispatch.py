"""统一的 durable run 分发契约(所有 agent run 触发的唯一入口)。

- ``multitask_strategy="interrupt"``:后续消息会中止活跃 run(进度由 sync
  checkpoint 保留),空闲则直接开跑;
- ``durability="sync"``:每步落 checkpoint,崩溃/回收后从最近 checkpoint 恢复;
- ``webhook=COMPLETION_WEBHOOK_URL``:run 终结(成功/失败)时平台回调本进程
  的 ``/webhooks/run-complete``(进程内 loopback);
- ``stream_resumable=True``:事件流可重放,晚连接可见历史事件;
- Protocol v2 run 形态:``stream_mode`` 集合 + ``stream_subgraphs`` +
  ``configurable`` 标记(同 dashboard 的 ``run.start``)。

本变体差异:
- ``source`` 只可能是 ``web``/``dashboard``/``automation``/``schedule``;
- 发送者身份 = 本地 username(无 GitHub/Slack 映射);
- 完成 webhook 走**进程内 loopback**(langgraph.json ``webhooks.url
  .disable_loopback=false``),``COMPLETION_WEBHOOK_URL`` 为相对路径。
"""

import logging
import os
import uuid
from typing import Any
from urllib.parse import urlparse

from langgraph_sdk import get_client
from langgraph_sdk.client import LangGraphClient
from langgraph_sdk.schema import Run

from .input_messages import (
    InputMessageContext,
    PersonIdentity,
    RunInput,
    build_run_input,
)

logger = logging.getLogger(__name__)

# 镜像 langgraph_api.event_streaming 的 EVENT_STREAMING_V2_CONFIG_KEY。
# 不 import:langgraph-api 是服务端运行时,不是本包依赖。
EVENT_STREAMING_V2_CONFIG_KEY = "__event_streaming_v2"
# dashboard 的 run.start 默认,减去 tools/lifecycle(REST POST /runs 不接受的协议通道)
V2_RUN_STREAM_MODES: tuple[str, ...] = (
    "values",
    "updates",
    "messages",
    "custom",
    "tasks",
    "checkpoints",
)


def _resolve_completion_webhook_url(configured: str | None) -> str | None:
    """解析完成 webhook URL(本地版:相对路径走进程内 loopback)。

    规则:
    - secret 未设 → 不挂 webhook(路由本身 fail-closed,挂了也 503);
    - 相对 URL(如 ``/webhooks/run-complete``)→ 直接返回(依赖
      ``disable_loopback=false`` 的进程内投递);
    - 绝对 http(s) URL → 原样返回;
    - 空 → None(不挂)。
    """
    if not configured:
        return None
    secret = os.environ.get("RUN_COMPLETE_WEBHOOK_SECRET", "").strip()
    if not secret:
        logger.warning(
            "RUN_COMPLETE_WEBHOOK_SECRET 未设置,完成 webhook 不挂载(路由 fail-closed)"
        )
        return None
    parsed = urlparse(configured)
    if parsed.scheme in ("http", "https"):
        return configured
    # 相对路径:拼上 secret token
    separator = "&" if "?" in configured else "?"
    return f"{configured}{separator}token={{RUN_COMPLETE_WEBHOOK_SECRET}}"


# 模块级解析(启动时一次;env 变更需重启)
COMPLETION_WEBHOOK_URL: str | None = _resolve_completion_webhook_url(
    os.environ.get("COMPLETION_WEBHOOK_URL") or "/webhooks/run-complete"
)


def _webhook_url() -> str | None:
    """把占位 token 换成真实 secret(每次创建时)。"""
    if not COMPLETION_WEBHOOK_URL:
        return None
    secret = os.environ.get("RUN_COMPLETE_WEBHOOK_SECRET", "").strip()
    if not secret:
        return None
    return COMPLETION_WEBHOOK_URL.format(RUN_COMPLETE_WEBHOOK_SECRET=secret)


def _dispatch_input(
    content: str | list[dict[str, Any]],
    source: str,
    configurable: dict[str, Any],
    injected_hashes: set[str] | None = None,
) -> RunInput:
    """构造 run 输入(本地身份 → XML 信封)。

    本地变体:发送者 = ``local_user``(configurable 里的 username);
    平台恒 ``open-swe``;surface 由 source 推导。
    """
    # surface:dashboard/web → web;schedule/automation → automation
    if source in ("schedule", "automation"):
        surface: str = "automation"
    elif source == "desktop":
        surface = "desktop"
    else:
        surface = "web"
    username = configurable.get("local_user") or "local-user"
    context: InputMessageContext = {
        "sender": username,
        "surface": surface,
        "kind": "human",
        "entity_id": f"open-swe:{username}",
    }
    # 本地用户身份(每次 run 都带,dynamic-context 去重由 dashboard 控制)
    person: PersonIdentity = {"id": f"open-swe:{username}", "platform": "open-swe"}
    return build_run_input(
        content,
        context,
        people=[person],
        injected_dynamic_context_hashes=injected_hashes or set(),
    )


def prepare_run_config(
    config: dict[str, Any] | None,
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    """归一化 run config:注入 v2 标记 + prepare_run_id + source。"""
    run_config = dict(config or {})
    configurable = dict(run_config.get("configurable") or {})
    # v2 流协议标记(必须置位,否则只流 values)
    configurable.setdefault(EVENT_STREAMING_V2_CONFIG_KEY, True)
    # prepare_run_id:关联 PrepareRun 中间件(缺则新 uuid)
    if not configurable.get("prepare_run_id"):
        configurable["prepare_run_id"] = uuid.uuid4().hex
    run_config["configurable"] = configurable
    # metadata 合并(触发源)
    run_meta = dict(metadata or {})
    run_config["metadata"] = run_meta
    return run_config


async def create_durable_run(
    thread_id: str,
    assistant_id: str,
    *,
    input: RunInput,
    source: str,
    config: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    client: LangGraphClient | None = None,
    multitask_strategy: str = "interrupt",
    durability: str = "sync",
    if_not_exists: str = "create",
    stream_resumable: bool = True,
    after_seconds: float | None = None,
) -> Run:
    """创建带 Open SWE durable 默认值的 run。"""
    client = client or get_client()
    run_config = prepare_run_config(config, metadata)
    # source 进 metadata(触发源)
    run_config["metadata"] = dict(run_config.get("metadata") or {})
    run_config["metadata"].setdefault("source", source)
    create_kwargs: dict[str, Any] = {
        "input": input,
        "config": run_config,
        "metadata": run_config["metadata"],
        "multitask_strategy": multitask_strategy,
        "durability": durability,
        "if_not_exists": if_not_exists,
        "stream_mode": list(V2_RUN_STREAM_MODES),
        "stream_subgraphs": True,
        "stream_resumable": stream_resumable,
    }
    webhook = _webhook_url()
    if webhook:
        create_kwargs["webhook"] = webhook
    if after_seconds is not None:
        create_kwargs["after_seconds"] = after_seconds

    run = await client.runs.create(thread_id, assistant_id, **create_kwargs)
    logger.info(
        "Dispatched %s run on thread %s (source=%s, run=%s)",
        assistant_id,
        thread_id,
        source,
        run.get("run_id") if isinstance(run, dict) else None,
    )
    return run


async def dispatch_agent_run(
    thread_id: str,
    content: str | list[dict[str, Any]],
    *,
    source: str,
    configurable: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    multitask_strategy: str = "interrupt",
    client: LangGraphClient | None = None,
) -> Run:
    """触发一次 agent run(本变体主入口:dashboard / scheduler 扇出)。

    参数:
        thread_id: 目标线程。
        content: 用户/系统正文(文本或结构化块)。
        source: 触发源(web/dashboard/schedule/automation)。
        configurable: 附加配置(local_user/repo/plan_mode/model 覆盖等)。
        metadata: 附加元数据。
        multitask_strategy: busy 语义(默认 interrupt)。
        client: 运行时客户端(默认 get_client(),本进程内)。
    """
    cfg = dict(configurable or {})
    # 触发源进 configurable(PrepareRun 中间件读 source 渲染提示)
    cfg.setdefault("source", source)
    # 已注入的 dynamic context hashes(从线程 metadata 读,避免重复注入身份)
    injected: set[str] = set()
    client = client or get_client()
    if thread_id:
        try:
            thread = await client.threads.get(thread_id)
            tmeta = (thread or {}).get("metadata") or {}
            raw = tmeta.get("injected_dynamic_context_hashes")
            if isinstance(raw, list):
                injected = {str(x) for x in raw}
        except Exception:  # noqa: BLE001
            # 线程不存在 → 首次 run,无去重
            injected = set()
    run_input = _dispatch_input(content, source, cfg, injected)
    return await create_durable_run(
        thread_id,
        "agent",
        input=run_input,
        source=source,
        config={"configurable": cfg},
        metadata=metadata,
        client=client,
        multitask_strategy=multitask_strategy,
    )
