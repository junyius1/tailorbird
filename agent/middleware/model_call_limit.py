"""单次 run 的 model 调用次数上限(防失控循环烧 token)。

超限后以"收尾"语义结束 run(把剩余状态写进 checkpoint),而不是抛错。
"""

import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, SystemMessage

logger = logging.getLogger(__name__)

# 缺省单次 run 的 model 调用上限
DEFAULT_RUN_MODEL_CALL_LIMIT = 15000


class ModelCallLimitMiddleware(AgentMiddleware):
    """按 run 累计 model 调用次数;超限注入收尾指令并结束。

    成员:
        run_limit: 单次 run 允许的 model 调用次数上限。
        _count: 当前 run 已发生的 model 调用数(每次 model call 后 +1)。
    """

    name: str = "ModelCallLimitMiddleware"

    def __init__(self, run_limit: int | None = None) -> None:
        super().__init__()
        # 上限(env 可覆盖)
        self.run_limit: int = run_limit or int(
            os.environ.get("OPEN_SWE_RUN_MODEL_CALL_LIMIT", str(DEFAULT_RUN_MODEL_CALL_LIMIT))
        )
        # 本 run 已调用的 model 次数
        self._count: int = 0

    async def awrap_model_call(
        self, request: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        """放行前计数;超限时注入收尾 SystemMessage 并直接返回(不再调模型)。"""
        self._count += 1
        if self._count > self.run_limit:
            logger.warning("run 的 model 调用次数超过上限 %d,强制收尾", self.run_limit)
            # 返回一条 AIMessage 收尾(带工具调用历史,checkpoint 正常落盘)
            return AIMessage(
                content=f"[系统] 本次 run 的模型调用次数已达上限({self.run_limit}),请人工检查任务进度。",
            )
        return await handler(request)
