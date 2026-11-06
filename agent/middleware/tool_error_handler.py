"""工具调用异常兜底:工具抛异常 → 转成 ToolMessage 错误文本,不让 run 崩。

LLM 对"工具报错"有恢复能力(改参数重试/换路径);run 崩溃则整个线程卡死。
"""

import logging
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

logger = logging.getLogger(__name__)


class ToolErrorMiddleware(AgentMiddleware):
    """把工具异常转成带错误文本的 ToolMessage(或 Command 失败态)。"""

    name: str = "ToolErrorMiddleware"

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Any
    ) -> Any:
        """执行工具;异常时返回 ToolMessage(内容=错误,状态=error)。"""
        from langchain_core.messages import ToolMessage

        try:
            return await handler(request)
        except Exception as exc:  # noqa: BLE001 —— 这里必须兜住一切,防 run 崩溃
            logger.warning(
                "工具 %s 执行异常: %s",
                getattr(request, "name", "?"),
                exc,
            )
            tool_call_id = ""
            name = "tool"
            try:
                tool_call = getattr(request, "tool_call", None)
                if isinstance(tool_call, dict):
                    tool_call_id = str(tool_call.get("id") or "")
                    name = str(tool_call.get("name") or "tool")
            except Exception:
                pass
            return ToolMessage(
                content=f"工具执行失败: {exc}",
                tool_call_id=tool_call_id,
                name=name,
                status="error",
            )
