"""模型返回后:先把推理全文写入 llm_reasoning 专表,再从响应里剥离思考。

设计书 §7.1:推理消息必须显示但**不能**进 checkpoint(超大线程体积主因)。
本中间件是 ``llm_reasoning`` 表的**唯一写者**:

1. model call 返回后,按现有提取逻辑取出推理文本
   (``additional_kwargs.reasoning_content`` + ``reasoning``/``thinking`` content
   块;Anthropic 带 ``signature`` 的 thinking 块**不**剥离、也**不**入表);
2. ``reasoning_store.upsert(thread_id, message.id, run_id, model, content)``
   幂等写入(best-effort:5s 超时 + 1 次重试,仍失败仅 warning——
   **推理是展示数据,不得让 run 失败**);
3. 照旧剥离再写 checkpoint。

实时流不受影响:reasoning-delta 事件在模型调用进行中边跑边发,
UI 实时渲染不变;本表只服务"历史线程点开 Thought"的懒加载。
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage

logger = logging.getLogger(__name__)

# additional_kwargs 里承载推理文本的键名(按优先级)
_REASONING_KWARG_KEYS: tuple[str, ...] = ("reasoning_content", "reasoning")


def _extract_reasoning(message: Any) -> str:
    """从一条 AIMessage 提取推理全文(kwarg + reasoning/thinking content 块)。

    Anthropic 带非空 signature 的 thinking 块跳过(需原样往返,不入表)。
    """
    if not isinstance(message, AIMessage):
        return ""
    parts: list[str] = []
    kwargs = message.additional_kwargs
    if isinstance(kwargs, dict):
        for key in _REASONING_KWARG_KEYS:
            value = kwargs.get(key)
            if isinstance(value, str) and value:
                parts.append(value)
    content = message.content
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "reasoning":
                text = block.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
            elif block_type == "thinking" and not block.get("signature"):
                text = block.get("thinking") or block.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
    return "\n".join(parts).strip()


def _strip_message(message: Any) -> None:
    """从一条消息里剥离思考(kwarg + reasoning/thinking 块;保留签名块)。"""
    if not isinstance(message, AIMessage):
        return
    kwargs = message.additional_kwargs
    if isinstance(kwargs, dict):
        for key in _REASONING_KWARG_KEYS:
            kwargs.pop(key, None)
    content = message.content
    if not isinstance(content, list):
        return
    kept = [
        block
        for block in content
        if not (
            isinstance(block, dict)
            and (
                block.get("type") == "reasoning"
                or (block.get("type") == "thinking" and not block.get("signature"))
            )
        )
    ]
    if len(kept) != len(content):
        message.content = kept


def _result_messages(response: Any) -> list[Any] | None:
    """从 ModelResponse 里取消息列表(兼容 .model_response 包装)。"""
    inner = getattr(response, "model_response", None)
    if inner is not None:
        response = inner
    result = getattr(response, "result", None)
    return result if isinstance(result, list) else None


def _run_context() -> tuple[str, str]:
    """从当前 run 的 config 取 (thread_id, run_id);取不到返回空串。"""
    try:
        from langgraph.config import get_config

        config = get_config()
        configurable = config.get("configurable") or {}
        thread_id = str(configurable.get("thread_id") or "")
        run_id = str(configurable.get("run_id") or "")
        return thread_id, run_id
    except Exception:
        return "", ""


class StripThoughtsMiddleware(AgentMiddleware):
    """写推理专表(幂等、best-effort)后剥离思考,再写 checkpoint。"""

    name: str = "StripThoughtsMiddleware"

    async def awrap_model_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        """模型调用返回后:逐条消息 → 提取推理 → upsert → 剥离。"""
        response = await handler(request)
        messages = _result_messages(response)
        if not messages:
            return response
        # 模型 id(观测/入表用)
        model = getattr(request, "model", None)
        model_id = getattr(model, "model_name", None) or getattr(model, "model", None)
        thread_id, run_id = _run_context()
        for message in messages:
            reasoning = _extract_reasoning(message)
            message_id = getattr(message, "id", None)
            # 先入表(best-effort),再剥离
            if reasoning and message_id and thread_id:
                from ..reasoning_store import upsert

                await upsert(
                    thread_id,
                    str(message_id),
                    run_id,
                    str(model_id) if model_id else None,
                    reasoning,
                )
            _strip_message(message)
        return response
