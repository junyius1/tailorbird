"""run 执行期图获取 + config 装配(主事件循环内)。

对齐 ``langgraph_api.graph.get_graph`` 对
``access_context=threads.create_run`` 的注入语义(不 import 服务端运行时):

- ``configurable["__is_for_execution__"]=True`` → 图工厂建**带沙箱**的完整
  agent(``agent/graphs/agent.get_agent``);
- ``configurable[CONFIG_KEY_RUNTIME]=Runtime(store=...)`` → 中间件的
  ``Runtime.store`` 解析(Pregel 顶层 ``_defaults`` 从它取 store,再
  ``parent_runtime.merge`` 进任务 Runtime);
- ``configurable[CONFIG_KEY_CHECKPOINTER]`` + ``graph.checkpointer`` 双挂
  (astream 与 aget_state 都认 config 里的);
- ``RunControl`` 挂 configurable → Pregel 节点边界 drain(取消=interrupt);
- 单循环铁律:本模块只在**主 uvicorn 循环**被调用;``traced_agent`` 内部
  ``pin_single_event_loop()``。绝不从 StateReader worker 线程调用。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

try:
    from langgraph._internal._constants import (
        CONFIG_KEY_CHECKPOINTER,
        CONFIG_KEY_RUNTIME,
    )
except Exception:
    from langgraph.constants import (  # type: ignore[assignment]
        CONFIG_KEY_CHECKPOINTER,
        CONFIG_KEY_RUNTIME,
    )

from langgraph.runtime import RunControl, Runtime

from ..graphs.agent import traced_agent


def build_run_config(
    thread_id: str,
    configurable: dict[str, Any],
    *,
    checkpointer: Any,
    store: Any,
    metadata: dict[str, Any] | None,
    run_id: str,
    user: str,
    source: str,
) -> tuple[dict[str, Any], RunControl]:
    """run 执行 config。返回 (config, run_control)。"""
    configurable = dict(configurable or {})
    configurable["thread_id"] = thread_id
    configurable["local_user"] = user
    configurable["source"] = source
    configurable["__is_for_execution__"] = True
    run_control = RunControl()
    # 执行态 Runtime:store 供中间件(Runtime.store);control 供 Pregel 顶层
    # ``control=control or parent_runtime.control`` 解析 → 节点边界 drain(取消)
    configurable[CONFIG_KEY_RUNTIME] = Runtime(store=store, control=run_control)
    configurable[CONFIG_KEY_CHECKPOINTER] = checkpointer
    config: dict[str, Any] = {
        "configurable": configurable,
        "run_id": run_id,
        "recursion_limit": int(configurable.pop("recursion_limit", 200)),
    }
    metadata = dict(metadata or {})
    metadata.setdefault("source", source)
    config["metadata"] = metadata
    return config, run_control


async def get_exec_graph(
    checkpointer: Any,
    store: Any,
    run_config: dict[str, Any],
    *,
    graph_id: str = "agent",
) -> tuple[Any, Any]:
    """建执行态图(带沙箱)+ 挂 checkpointer。返回 (graph, async_ctx)。

    ``async_ctx`` 是 ``traced_agent`` 的异步上下文,调用方用完必须
    ``await async_ctx.__aexit__(None, None, None)``。
    """
    # 图工厂从 config 读 __is_for_execution__/local_user/model 等
    ctx = traced_agent(run_config)
    graph = await ctx.__aenter__()
    graph.checkpointer = checkpointer
    if hasattr(graph, "store"):
        try:
            graph.store = store
        except Exception:
            logger.debug("graph.store 不可写,依赖 Runtime.store", exc_info=True)
    return graph, ctx
