"""plan mode 工具门控。

plan mode 激活时(run 启动即为 plan mode,或模型中途调 ``enter_plan_mode``),
从每次 model 请求中**剔除**会变更外部系统的工具,强制只读规划。
每次 model call 都重算工具列表,因此中途激活也能约束**下一次** model 轮次。
"""

from collections.abc import Awaitable, Callable
from typing import Any, NotRequired

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ModelRequest,
    ModelResponse,
)
from langchain_core.tools import BaseTool


class PlanModeState(AgentState):
    """plan mode 状态通道(``enter_plan_mode`` 的 Command 更新写入此处)。"""

    # plan mode 开关(本 run 内可变)
    plan_mode: NotRequired[bool]


def _tool_name(tool: BaseTool | dict[str, Any] | Any) -> str | None:
    """工具名(兼容 BaseTool / dict / 其它)。"""
    if isinstance(tool, dict):
        name = tool.get("name")
        return name if isinstance(name, str) else None
    name = getattr(tool, "name", None)
    return name if isinstance(name, str) else None


class PlanModeMiddleware(AgentMiddleware):
    """plan mode 激活时,从每次 model 请求中剔除禁用工具。"""

    state_schema = PlanModeState

    def __init__(self, *, excluded: frozenset[str], initial: bool = False) -> None:
        super().__init__()
        # 禁用的工具名集合
        self._excluded = excluded
        # 本 run 启动时的初始 plan mode(来自线程 metadata)
        self._initial = initial

    def before_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:  # noqa: ARG002
        """run 开始:把 plan_mode 重置为本 run 解析值。

        防止上一次 run 的 ``enter_plan_mode`` 残留的 ``True``
        让后续(已批准/实现中)的 run 重新进入 plan mode;
        本 run 内 ``enter_plan_mode`` 仍可重新置位。
        """
        return {"plan_mode": self._initial}

    def _active(self, request: ModelRequest) -> bool:
        """plan mode 是否激活(state 优先,缺省用 initial)。"""
        state = getattr(request, "state", None)
        if isinstance(state, dict):
            value = state.get("plan_mode")
            if isinstance(value, bool):
                return value
        else:
            value = getattr(state, "plan_mode", None)
            if isinstance(value, bool):
                return value
        return self._initial

    def _filter(self, request: ModelRequest) -> ModelRequest:
        """从请求工具列表中剔除禁用工具(未激活则原样返回)。"""
        if not self._excluded or not self._active(request):
            return request
        filtered = [t for t in request.tools if _tool_name(t) not in self._excluded]
        if len(filtered) == len(request.tools):
            return request
        return request.override(tools=filtered)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """model call:先过滤工具,再执行。"""
        return await handler(self._filter(request))
