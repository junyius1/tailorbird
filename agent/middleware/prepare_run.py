"""run 准备中间件:渲染系统提示 + 注入发送者上下文 + 更新线程 metadata。

本变体简化版(去 GitHub token 解析、去 Linear 上下文):
1. ``abefore_agent``:解析线程属主 profile(reasoning_stream 等)→ 渲染
   ``rendered_system_prompt`` 写进 state;
2. ``awrap_model_call``:把渲染好的系统提示包进 ``<system-instructions>``
   覆盖 request.system_message(幂等:已包裹不再包);
3. 把 run 级上下文(local_user/repo/plan_mode/model)同步回线程 metadata,
   供 dashboard 列表/详情展示。
"""

import logging
from typing import Any

from langchain.agents.middleware import AgentState, AgentMiddleware
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.config import get_config
from langgraph.runtime import Runtime
from langgraph.store.base import BaseStore
from typing import NotRequired

from ..input_messages import wrap_system_prompt
from ..prompt import construct_sender_context, construct_system_prompt

logger = logging.getLogger(__name__)


class PrepareRunState(AgentState):
    """扩展 agent state:携带 run 级渲染上下文。"""

    # 渲染好的系统提示(由 abefore_agent 写入)
    rendered_system_prompt: NotRequired[str | None]
    # 沙箱工作目录
    work_dir: NotRequired[str | None]


class PrepareRunMiddleware(AgentMiddleware):
    """run 准备:渲染系统提示 + 发送者上下文 + metadata 同步。

    成员:
        thread_id: 线程 id。
        source: 触发源(web/dashboard/schedule)。
        model_id: 本次 run 的模型 id(profile/用户选择解析后)。
        effort: 推理力度(可空)。
        plan_mode: 是否 plan mode。
        plan_url: plan 评审链接(plan_mode 用)。
        profile_reasoning_stream: 属主 profile 的推理流开关(None=Inherit)。
    """

    name: str = "PrepareRunMiddleware"
    state_schema = PrepareRunState

    def __init__(
        self,
        thread_id: str,
        *,
        source: str = "web",
        model_id: str | None = None,
        effort: str | None = None,
        plan_mode: bool = False,
        plan_url: str = "",
        profile_reasoning_stream: bool | None = None,
    ) -> None:
        super().__init__()
        # 线程 id
        self._thread_id: str = thread_id
        # 触发源
        self._source: str = source
        # 模型 id
        self._model_id: str | None = model_id
        # 推理力度
        self._effort: str | None = effort
        # plan mode 开关
        self._plan_mode: bool = plan_mode
        # plan 评审链接
        self._plan_url: str = plan_url
        # 属主 profile 的推理流开关(§10.6 单一事实源)
        self._profile_reasoning_stream: bool | None = profile_reasoning_stream

    async def abefore_agent(self, state: PrepareRunState, runtime: Runtime) -> dict[str, Any]:
        """run 开始前:渲染系统提示写进 state(工作目录缺省 /workspace)。"""
        # 工作目录:由沙箱 backend 的 root 决定;缺省 /workspace
        work_dir = state.get("work_dir") or "/workspace"
        rendered = construct_system_prompt(
            working_dir=work_dir,
            source=self._source,
            plan_mode=self._plan_mode,
            plan_url=self._plan_url,
        )
        # 发送者上下文:作为 user 消息注入(与参考实现一致)。
        # 不能用 SystemMessage:本地 vLLM/Qwen 要求 system 消息必须在最前,
        # 真正的系统提示由 awrap_model_call 通过 request.system_message 提供,
        # 再追加一条 SystemMessage 进 state 会触发 "System message must be at
        # the beginning" 400。
        try:
            config = get_config()
            configurable = config.get("configurable") or {}
        except Exception:
            configurable = {}
        sender_username = configurable.get("local_user") or "local-user"
        sender_context = construct_sender_context(str(sender_username))
        return {
            "rendered_system_prompt": rendered,
            "work_dir": work_dir,
            "messages": [HumanMessage(content=sender_context)],
        }

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Any,
    ) -> Any:
        """每次 model call:用渲染好的系统提示覆盖 request.system_message。"""
        rendered = request.state.get("rendered_system_prompt")
        if isinstance(rendered, str) and rendered:
            existing = request.system_message.text if request.system_message is not None else ""
            content = f"{rendered}\n\n{existing}" if existing else rendered
            request = request.override(
                system_message=SystemMessage(content=wrap_system_prompt(content))
            )
        elif request.system_message is not None:
            request = request.override(
                system_message=SystemMessage(
                    content=wrap_system_prompt(request.system_message.text)
                )
            )
        return await handler(request)
