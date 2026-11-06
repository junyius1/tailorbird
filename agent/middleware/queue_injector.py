"""线程内消息队列注入中间件(包装裸钩子 ``check_message_queue_before_model``)。

dashboard 在 busy 线程上收到新消息时写入 store 队列;本中间件把
``before_model`` 钩子挂进中间件链,下一次 model call 前注入为 human 消息。
"""

from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState

from .check_message_queue import check_message_queue_before_model


class QueueInjectorMiddleware(AgentMiddleware):
    """线程内 FIFO 消息队列注入(每线程实例)。"""

    name: str = "QueueInjectorMiddleware"

    async def abefore_model(self, state: AgentState, runtime: Any) -> dict[str, Any] | None:
        """model call 前:读并注入排队消息。"""
        return await check_message_queue_before_model(state, runtime)
