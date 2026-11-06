"""主编程 agent 图工厂(``traced_agent`` = langgraph.json 指向的图)。

- 每线程一个本地沙箱后端(docker 容器 / local 目录),线程级缓存复用;
- 图工厂是 **async context manager**(``langgraph_api.as_asynccontextmanager``
  会 ``await`` 协程返回值,故 ``traced_agent`` 为 async 函数,
  ``get_agent`` 为 async 工厂,运行时每次取图都会重新进入);
- 无 thread_id 或**非执行态**(``__is_for_execution__=False``,如 state 读)
  → 兜底 agent(无沙箱、静态提示),避免把 state 读变成容器创建;
- 中间件链(外层→内层):
  LLMGateMiddleware(全进程 LLM gate,排队时间不计模型超时)
  → PlanModeMiddleware(plan mode 激活时剔除变更类工具)
  → ModelCallTimeoutMiddleware(单模型调用 deadline)
  → ModelCallLimitMiddleware(每 run 模型调用次数上限)
  → StripThoughtsMiddleware(推理先写 llm_reasoning 专表,再剥离出 checkpoint)
  → ToolErrorMiddleware(工具异常转 ToolMessage,防 run 崩溃)
  → QueueInjectorMiddleware(线程内 follow-up 消息注入)
  → PrepareRunMiddleware(系统提示渲染 + 发送者上下文 + metadata 同步);
- 系统提示由 PrepareRunMiddleware 每次 model call 覆盖(动态,按线程/源渲染);
- 本地 vLLM 为主模型(``local:<name>``),推理流开关按属主 profile 透传。
"""

import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, AgentState
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.pregel import Pregel
from langgraph.runtime import Runtime

from deepagents import create_deep_agent
from deepagents.backends.protocol import SandboxBackendProtocol

from ..middleware.model_call_limit import ModelCallLimitMiddleware
from ..middleware.model_call_timeout import ModelCallTimeoutMiddleware
from ..middleware.plan_mode import PlanModeMiddleware
from ..middleware.prepare_run import PrepareRunMiddleware
from ..middleware.queue_injector import QueueInjectorMiddleware
from ..middleware.strip_thoughts import StripThoughtsMiddleware
from ..middleware.tool_error_handler import ToolErrorMiddleware
from ..prompt import construct_sender_context, construct_system_prompt
from ..sandbox_state import ensure_sandbox_for_thread
from ..utils.event_loop import pin_single_event_loop
from ..utils.llm_gate import LLMGateMiddleware
from ..utils.model import install_local_reasoning_patch, make_model

logger = logging.getLogger(__name__)

# 默认递归上限(步骤数;防失控循环)
DEFAULT_RECURSION_LIMIT = 100
# 无 thread_id 时的兜底工作目录
_DEFAULT_WORK_DIR = "/workspace"
# 默认触发源
_DEFAULT_SOURCE = "web"
# plan mode 激活时剔除的"变更类"工具(本地沙箱内文件变更也禁,强制只读)
PLAN_MODE_EXCLUDED_TOOLS = frozenset({
    "write_file",
    "edit_file",
    "delete",
    "execute",
})


def _thread_id_of(config: RunnableConfig) -> str | None:
    """从 config 取 thread_id(可空)。"""
    configurable = (config or {}).get("configurable") or {}
    thread_id = configurable.get("thread_id")
    return str(thread_id) if isinstance(thread_id, str) and thread_id else None


def _configurable_of(config: RunnableConfig) -> dict[str, Any]:
    """从 config 取 configurable(缺省空 dict)。"""
    configurable = (config or {}).get("configurable") or {}
    return configurable if isinstance(configurable, dict) else {}


def _is_for_execution(config: RunnableConfig) -> bool:
    """是否执行态(threads.create_run;state 读等为非执行态)。"""
    return bool(_configurable_of(config).get("__is_for_execution__", False))


def _resolve_model_and_effort(configurable: dict[str, Any]) -> tuple[str, str | None]:
    """解析 (模型 id, effort):configurable 覆盖 > 本地缺省。"""
    model_id = configurable.get("model") or configurable.get("model_id")
    if not isinstance(model_id, str) or not model_id:
        from ..utils.model import local_model_id

        model_id = f"local:{local_model_id()}"
    effort = configurable.get("effort")
    effort = effort if isinstance(effort, str) and effort else None
    return model_id, effort


def _profile_reasoning_stream() -> bool | None:
    """属主 profile 的推理流开关(§10.6 单一事实源的 profile 半)。

    图工厂不在关键路径做重活:此处返回 None(=Inherit env 默认);
    profile 级覆盖由 prepare_run 的 metadata 读取在运行层补充
    (本地单用户部署,env 默认即属主默认,语义等价)。
    """
    return None


def _build_tools() -> list[Any]:
    """agent 工具集(本地化保留面:网络/检索 + plan 三件套)。"""
    from ..tools.approve_plan import approve_plan
    from ..tools.enter_plan_mode import enter_plan_mode
    from ..tools.fetch_url import fetch_url
    from ..tools.http_request import http_request
    from ..tools.save_plan import save_plan
    from ..tools.web_search import web_search

    return [
        http_request,
        fetch_url,
        web_search,
        enter_plan_mode,
        save_plan,
        approve_plan,
    ]


class _StaticPromptMiddleware(AgentMiddleware):
    """无沙箱 run(无 thread_id / 非执行态)的静态系统提示中间件。"""

    name: str = "StaticPromptMiddleware"

    def __init__(self, prompt: str, sender_context: str) -> None:
        super().__init__()
        # 静态系统提示(渲染一次)
        self._prompt: str = prompt
        # 发送者上下文消息
        self._sender_context: str = sender_context

    async def abefore_agent(self, state: AgentState, runtime: Runtime) -> dict[str, Any]:
        """run 开始:附发送者上下文(user 消息;SystemMessage 会被 vLLM 拒)。"""
        return {"messages": [HumanMessage(content=self._sender_context)]}

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        """model call:注入静态系统提示。"""
        request = request.override(system_message=SystemMessage(content=self._prompt))
        return await handler(request)


async def _build_threaded_agent(config: RunnableConfig, sandbox: SandboxBackendProtocol) -> CompiledStateGraph:
    """构造带沙箱的完整 agent(中间件链 + 本地模型 + 工具)。"""
    configurable = _configurable_of(config)
    thread_id = _thread_id_of(config) or ""
    source = configurable.get("source") or _DEFAULT_SOURCE
    plan_mode = configurable.get("plan_mode") is True
    model_id, effort = _resolve_model_and_effort(configurable)
    profile_rs = _profile_reasoning_stream()

    # 本地模型(推理流开关透传;仅 local: 生效)
    model = await make_model(model_id, effort=effort, reasoning_stream=profile_rs)
    # 本地推理捕获补丁(幂等;local: 模型的推理流进 llm_reasoning)
    install_local_reasoning_patch()

    # 中间件链(声明顺序 = 包裹顺序:先声明者在外层)
    middlewares: list[AgentMiddleware] = [
        # 最外层:全进程 LLM gate(排队不计模型超时)
        LLMGateMiddleware(),
        # plan mode 工具门控(每次 model call 重算工具列表)
        PlanModeMiddleware(excluded=PLAN_MODE_EXCLUDED_TOOLS, initial=plan_mode),
        # 单模型调用 deadline
        ModelCallTimeoutMiddleware(),
        # 每 run 模型调用次数上限
        ModelCallLimitMiddleware(),
        # 推理:先写 llm_reasoning 专表,再剥离出 checkpoint
        StripThoughtsMiddleware(),
        # 工具异常转 ToolMessage(防 run 崩溃)
        ToolErrorMiddleware(),
        # 线程内 follow-up 消息注入(before_model)
        QueueInjectorMiddleware(),
        # run 准备:系统提示渲染 + 发送者上下文 + metadata 同步
        PrepareRunMiddleware(
            thread_id,
            source=str(source),
            model_id=model_id,
            effort=effort,
            plan_mode=plan_mode,
        ),
    ]

    return create_deep_agent(
        model=model,
        tools=_build_tools(),
        system_prompt="",
        middleware=middlewares,
        backend=sandbox,
        name="tailorbird-agent",
    )


async def _build_stub_agent(config: RunnableConfig) -> CompiledStateGraph:
    """无沙箱兜底 agent(无 thread_id / 非执行态)。"""
    configurable = _configurable_of(config)
    source = configurable.get("source") or _DEFAULT_SOURCE
    plan_mode = configurable.get("plan_mode") is True
    local_user = configurable.get("local_user") or "local-user"
    model_id, effort = _resolve_model_and_effort(configurable)
    model = await make_model(model_id, effort=effort)
    install_local_reasoning_patch()
    prompt = construct_system_prompt(
        working_dir=_DEFAULT_WORK_DIR,
        source=str(source),
        plan_mode=plan_mode,
    )
    return create_deep_agent(
        model=model,
        tools=[],
        system_prompt="",
        middleware=[
            LLMGateMiddleware(),
            ModelCallTimeoutMiddleware(),
            ModelCallLimitMiddleware(),
            _StaticPromptMiddleware(prompt, construct_sender_context(str(local_user))),
        ],
        name="tailorbird-stub",
    )


async def get_agent(config: RunnableConfig) -> Pregel:
    """按线程创建/复用带沙箱的 agent。

    - 无 thread_id 或非执行态 → 兜底 agent(不碰沙箱);
    - 执行态 + 有 thread_id → 确保线程沙箱(复用/重建)→ 完整 agent。
    """
    config = dict(config or {})
    config.setdefault("recursion_limit", DEFAULT_RECURSION_LIMIT)
    thread_id = _thread_id_of(config)
    if thread_id is None or not _is_for_execution(config):
        logger.debug("无 thread_id 或非执行态,构造无沙箱兜底 agent")
        return (await _build_stub_agent(config)).with_config(config)
    # 确保线程沙箱(缓存命中直接复用;丢失则重建)
    sandbox = await ensure_sandbox_for_thread(thread_id, config)
    agent = await _build_threaded_agent(config, sandbox)
    return agent.with_config(config)


@contextlib.asynccontextmanager
async def traced_agent(config: RunnableConfig) -> AsyncIterator[Pregel]:
    """langgraph.json 指向的图工厂(async context manager 形态)。

    ``langgraph_api`` 取图时:``invoke_factory`` 调用本函数得到协程,
    ``as_asynccontextmanager`` ``await`` 之并 yield 图实例;
    每次 run/state 读取都会重新进入(按线程重建 agent,
    沙箱缓存保证容器复用)。
    """
    # 单循环铁律:图工厂运行在单事件循环内
    pin_single_event_loop()
    yield await get_agent(config)
