"""运行时 app 共享状态访问器(FastAPI 依赖)。

运行时只有一个 checkpointer、一个 store、一个 ThreadRegistry、一个
StateReader——都在 lifespan 里建好,挂到 ``app.state``。这里提供类型化
访问器,避免各路由各自 ``request.app.state.x``。
"""

from typing import Any

from fastapi import Request

from .config import RuntimeConfig
from .state import StateReader
from .threads import ThreadRegistry


def get_config(request: Request) -> RuntimeConfig:
    return request.app.state.runtime_config


def get_checkpointer(request: Request) -> Any:
    return request.app.state.checkpointer


def get_store(request: Request) -> Any:
    return request.app.state.store


def get_threads(request: Request) -> ThreadRegistry:
    return request.app.state.threads


def get_state_reader(request: Request) -> StateReader:
    return request.app.state.state_reader


# 模块级单例(供无法拿 Request 的场景,如 state 缓存/reader 内部)
_state: dict[str, Any] = {}


def set_globals(
    *,
    config: Any,
    checkpointer: Any,
    store: Any,
    threads: ThreadRegistry,
    state_reader: StateReader,
) -> None:
    _state["config"] = config
    _state["checkpointer"] = checkpointer
    _state["store"] = store
    _state["threads"] = threads
    _state["state_reader"] = state_reader


def global_checkpointer() -> Any:
    return _state["checkpointer"]


def global_store() -> Any:
    return _state["store"]


def global_threads() -> ThreadRegistry:
    return _state["threads"]
