"""沙箱 provider 注册与创建(设计书 §4.1.1)。

``SANDBOX_FACTORIES`` 是纯 dict(name → (module, function)):
本变体只有 ``docker``(唯一推荐 provider)与 ``local``(无 Docker 裸开发回退)。
云 provider(langsmith/daytona/modal/runloop/e2b)全部删除。
"""

import asyncio
import inspect
import logging
import os
from collections.abc import Callable
from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from deepagents.backends.protocol import SandboxBackendProtocol

logger = logging.getLogger(__name__)

# 沙箱工厂签名
SandboxFactory = Callable[..., Any]


class SandboxGoneError(RuntimeError):
    """线程绑定的沙箱已不存在(被删)。

    与"沙箱不可达"不同:已删除的沙箱没有工作树,
    调用方应重建而不是让 run 失败。
    """


# 唯一保留的两个 provider(docker = 推荐;local = 裸开发回退)
SANDBOX_FACTORIES: dict[str, tuple[str, str]] = {
    "docker": ("agent.integrations.docker", "create_docker_sandbox"),
    "local": ("agent.integrations.local", "create_local_sandbox"),
}


def _load_sandbox_factory(sandbox_type: str) -> SandboxFactory:
    """按名称加载工厂函数;未知类型抛 ValueError。"""
    factory_path = SANDBOX_FACTORIES.get(sandbox_type)
    if factory_path is None:
        supported = ", ".join(sorted(SANDBOX_FACTORIES))
        raise ValueError(f"Invalid sandbox type: {sandbox_type}. Supported types: {supported}")
    module_name, function_name = factory_path
    factory = getattr(import_module(module_name), function_name)
    if not callable(factory):
        raise TypeError(f"Sandbox factory {module_name}.{function_name} is not callable")
    return factory


def sandbox_type() -> str:
    """当前配置的沙箱类型(缺省 docker)。"""
    return os.getenv("SANDBOX_TYPE", "docker")


async def create_sandbox(
    sandbox_id: str | None = None,
    *,
    repo: dict | None = None,
    owner_username: str | None = None,
) -> "SandboxBackendProtocol":
    """按 ``SANDBOX_TYPE`` 创建或重连沙箱。

    参数:
        sandbox_id: 线程 id(容器/目录命名键);docker 必填。
        repo: 仓库元信息(解析宿主挂载路径,本地路径语义)。
        owner_username: 线程 owner(docker 沙箱的 commit 作者身份)。
    """
    active = sandbox_type()
    factory = _load_sandbox_factory(active)
    kwargs: dict[str, Any] = {}
    if active == "docker":
        kwargs = {"repo": repo, "owner_username": owner_username}
    if inspect.iscoroutinefunction(factory):
        return await factory(sandbox_id, **kwargs)
    # 同步工厂走 to_thread(不阻塞单事件循环)
    return await asyncio.to_thread(factory, sandbox_id, **kwargs)


def validate_sandbox_startup_config() -> None:
    """启动时校验当前 provider 配置(失败拒启)。"""
    active = sandbox_type()
    if active == "docker":
        from agent.integrations.docker import validate_docker_startup_config

        validate_docker_startup_config()
    elif active == "local":
        if not os.getenv("LOCAL_SANDBOX_ROOT_DIR"):
            logger.warning("SANDBOX_TYPE=local 但未设 LOCAL_SANDBOX_ROOT_DIR,使用 /tmp 默认值")
    else:
        raise ValueError(f"未配置的沙箱类型: {active}")
