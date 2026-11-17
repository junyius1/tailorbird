"""线程级沙箱后端缓存(单循环铁律:线程 ↔ 沙箱 ↔ 事件循环 亲和)。

- 每线程一个沙箱后端(docker 容器或 local 目录),run 间复用;
- 缓存按 thread_id;重连回调负责"沙箱丢了就重建";
- 仅在同一事件循环内使用(单循环保证),无跨进程共享。
"""

import logging
from typing import Any

from langchain_core.runnables import RunnableConfig

from deepagents.backends.protocol import SandboxBackendProtocol

from .utils.sandbox import SandboxGoneError, create_sandbox

logger = logging.getLogger(__name__)

# 线程 → 沙箱后端 的进程内缓存
_SANDBOX_BACKENDS: dict[str, SandboxBackendProtocol] = {}


def clear_sandbox_backend(thread_id: str) -> None:
    """移除线程的沙箱缓存(delete/recreate 线程时调用)。"""
    _SANDBOX_BACKENDS.pop(str(thread_id), None)


async def _ensure(
    thread_id: str,
    *,
    repo: dict | None,
    owner_username: str | None,
    backend: SandboxBackendProtocol | None,
) -> SandboxBackendProtocol:
    """确保线程有可用沙箱:缓存 → 复用;丢失/不存在 → 重建。"""
    if backend is not None:
        return backend
    try:
        new_backend = await create_sandbox(
            thread_id, repo=repo, owner_username=owner_username
        )
        _SANDBOX_BACKENDS[thread_id] = new_backend
        logger.info("新建沙箱后端 thread=%s id=%s", thread_id, new_backend.id)
        return new_backend
    except SandboxGoneError:
        # 沙箱被删 → 重建(幂等)
        logger.info("沙箱已丢失,重建 thread=%s", thread_id)
        new_backend = await create_sandbox(
            thread_id, repo=repo, owner_username=owner_username
        )
        _SANDBOX_BACKENDS[thread_id] = new_backend
        return new_backend


async def get_sandbox_backend(
    thread_id: str,
    *,
    repo: dict | None = None,
    owner_username: str | None = None,
) -> SandboxBackendProtocol:
    """取线程沙箱后端(工具侧入口;缓存命中直接返回)。"""
    cached = _SANDBOX_BACKENDS.get(thread_id)
    if cached is not None:
        return cached
    return await _ensure(
        thread_id, repo=repo, owner_username=owner_username, backend=cached
    )


def set_sandbox_backend(thread_id: str, backend: SandboxBackendProtocol) -> None:
    """由图工厂注入线程沙箱(创建/复用后登记缓存)。"""
    _SANDBOX_BACKENDS[str(thread_id)] = backend


async def ensure_sandbox_for_thread(
    thread_id: str,
    config: RunnableConfig | None = None,
    *,
    repo: dict | None = None,
    owner_username: str | None = None,
) -> SandboxBackendProtocol:
    """图工厂入口:确保线程沙箱可用并登记缓存。

    从 config 取 repo/owner(缺省),然后创建或复用。
    """
    cfg = (config or {}).get("configurable") or {}
    repo = repo if repo is not None else cfg.get("repo")
    # 字符串仓库(full_name 或本地绝对路径)归一化为 dict;路径仓库带
    # local_path,沙箱挂载直达该目录(§11)
    if isinstance(repo, str):
        from .utils.repo import normalize_repo_meta

        repo = normalize_repo_meta(repo)
    if not isinstance(repo, dict):
        repo = None
    owner_username = owner_username or cfg.get("local_user")
    backend = await _ensure(
        thread_id,
        repo=repo,
        owner_username=owner_username,
        backend=_SANDBOX_BACKENDS.get(thread_id),
    )
    return backend


def unwrap_sandbox_backend(backend: Any) -> Any:
    """解包可能的代理(本变体无代理,直接返回)。"""
    return backend
