"""模型调用的墙钟 deadline(防 provider 挂死让整个 run 静默)。

provider 的 ``timeout`` kwargs 只约束 HTTP 请求;某些传输(如 Responses
websocket)可能挂死且从不抛读超时 → 整个 run 卡住(无流事件、无队列消息
拾取)。本中间件把挂死变成超时错误:run 通过完成 webhook 报告,而不是静默。
注意:本层在 ``LLMGateMiddleware`` **内层**——gate 排队时间不计入本 deadline。
"""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware

logger = logging.getLogger(__name__)

# 缺省 wall-clock 超时(秒):高于 provider 级 timeout,
# 让 provider 客户端先失败重试,这里只兜 provider 感知不到的挂死
DEFAULT_MODEL_CALL_TIMEOUT_SECONDS = 900.0


class ModelCallTimeoutError(TimeoutError):
    """模型调用超过墙钟 deadline。"""


def _configured_timeout_seconds() -> float:
    """读 env 配置(非法/未设 → 缺省 900s)。"""
    raw = os.environ.get("OPEN_SWE_MODEL_CALL_TIMEOUT_SECONDS")
    if not raw:
        return DEFAULT_MODEL_CALL_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_MODEL_CALL_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_MODEL_CALL_TIMEOUT_SECONDS


class ModelCallTimeoutMiddleware(AgentMiddleware):
    """超时的模型调用抛错,而不是永远挂住。"""

    name: str = "ModelCallTimeoutMiddleware"

    def __init__(self, timeout_seconds: float | None = None) -> None:
        super().__init__()
        # 实际生效的超时秒数
        self._timeout_seconds = timeout_seconds or _configured_timeout_seconds()

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        """包一层 asyncio.wait_for;超时 → ModelCallTimeoutError。"""
        try:
            return await asyncio.wait_for(handler(request), timeout=self._timeout_seconds)
        except (TimeoutError, asyncio.TimeoutError) as exc:
            logger.warning("模型调用超过 %ss deadline,终止", self._timeout_seconds)
            raise ModelCallTimeoutError(
                f"模型调用超过 {self._timeout_seconds}s deadline"
            ) from exc
