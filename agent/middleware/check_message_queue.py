"""model 调用前:把线程队列里的待发消息注入 state(线程内 FIFO 语义)。

线程 busy 时,新消息由 dashboard 写进 store 队列
``("queue", thread_id) / "pending_messages"``;本中间件在**下一次 model
call 前**读出并注入为 human 消息(FIFO,``MAX_QUEUED_MESSAGES=100``,
超限丢最旧)。与 ``llm_gate`` 的跨线程排队互补、互不冲突:
本机制是**线程内** follow-up 语义。
"""

import asyncio
import logging
from typing import Any

from langchain.agents.middleware import AgentState
from langchain_core.messages import HumanMessage
from langgraph.config import get_config
from langgraph.store.base import BaseStore

logger = logging.getLogger(__name__)

# 单线程队列上限(超限丢最旧)
MAX_QUEUED_MESSAGES = 100
# store 命名空间前缀:("queue", <thread_id>)
_QUEUE_NAMESPACE = "queue"


def _queue_namespace(thread_id: str) -> tuple[str, str]:
    """线程的 store 队列命名空间。"""
    return (_QUEUE_NAMESPACE, thread_id)


async def _read_and_clear_queue(store: BaseStore, thread_id: str) -> list[dict[str, Any]]:
    """读出并清空队列;返回消息 payload 列表(文本块)。

    Postgres store 是 async(AIOSQL),必须走 aget/aput;
    若 store 无 async 接口(测试替身)则回退同步接口并放线程池。
    """
    namespace = _queue_namespace(thread_id)
    if hasattr(store, "aget"):
        try:
            item = await store.aget(namespace, "pending_messages")
        except Exception as exc:  # noqa: BLE001
            logger.warning("读消息队列失败(thread=%s): %s", thread_id, exc)
            return []
    else:
        try:
            item = await asyncio.to_thread(store.get, namespace, "pending_messages")
        except Exception as exc:  # noqa: BLE001
            logger.warning("读消息队列失败(thread=%s): %s", thread_id, exc)
            return []
    if item is None or not isinstance(item.value, dict):
        return []
    messages = item.value.get("messages")
    if not isinstance(messages, list):
        return []
    # 清空(下一次 model call 前注入)
    try:
        if hasattr(store, "aput"):
            await store.aput(namespace, "pending_messages", {"messages": []})
        else:
            await asyncio.to_thread(
                store.put, namespace, "pending_messages", {"messages": []}
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("清空消息队列失败(thread=%s): %s", thread_id, exc)
    return [m for m in messages if isinstance(m, dict)][:MAX_QUEUED_MESSAGES]


async def check_message_queue_before_model(
    state: AgentState,
    runtime: Any,
) -> dict[str, Any] | None:
    """before_model 钩子:有排队消息则注入。

    参数:
        state: 当前 agent state。
        runtime: 运行时(含 store 与 config)。
    返回:
        ``{"messages": [...]}``(注入的 human 消息)或 None。
    """
    store: BaseStore | None = getattr(runtime, "store", None)
    if store is None:
        return None
    try:
        config = get_config()
    except Exception:
        return None
    thread_id = str((config.get("configurable") or {}).get("thread_id") or "")
    if not thread_id:
        return None
    queued = await _read_and_clear_queue(store, thread_id)
    if not queued:
        return None
    # 文本块 → HumanMessage
    messages: list[HumanMessage] = []
    for payload in queued:
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        messages.append(HumanMessage(content=text))
    if not messages:
        return None
    logger.info("注入 %d 条排队消息(thread=%s)", len(messages), thread_id)
    return {"messages": messages}
