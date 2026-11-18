"""线程级沙箱后端缓存(单循环铁律:线程 ↔ 沙箱 ↔ 事件循环 亲和)。

- 每线程一个沙箱后端(docker 容器或 local 目录),run 间复用;
- 缓存按 thread_id,存 (provider 类型, 后端);**类型按 run 解析**
  (run 级 configurable.sandbox_type > 线程 metadata > env),与缓存类型
  不一致 → best-effort 停旧容器后按新类型重建(统一布局,目录共用,
  来回切换不丢状态);
- 缓存丢失/沙箱被删 → 重建;
- 仅在同一事件循环内使用(单循环保证),无跨进程共享。
"""

import logging
from typing import Any

from langchain_core.runnables import RunnableConfig

from deepagents.backends.protocol import SandboxBackendProtocol

from .utils.sandbox import SandboxGoneError, create_sandbox, resolve_sandbox_type

logger = logging.getLogger(__name__)

# 线程 → (provider 类型, 沙箱后端) 的进程内缓存
_SANDBOX_BACKENDS: dict[str, tuple[str, SandboxBackendProtocol]] = {}


def clear_sandbox_backend(thread_id: str) -> None:
    """移除线程的沙箱缓存(delete/recreate 线程时调用)。"""
    _SANDBOX_BACKENDS.pop(str(thread_id), None)


def get_sandbox_backend_type(thread_id: str) -> str | None:
    """线程当前缓存的 provider 类型(无缓存返回 None)。"""
    entry = _SANDBOX_BACKENDS.get(str(thread_id))
    return entry[0] if entry else None


def _stop_backend(backend: SandboxBackendProtocol) -> None:
    """best-effort 停掉旧后端(切 provider 时调用;docker = stop 容器)。"""
    stop = getattr(backend, "stop", None)
    if callable(stop):
        try:
            stop()
        except Exception:  # noqa: BLE001
            logger.debug("停止旧沙箱后端失败(best-effort)", exc_info=True)


async def _create(
    thread_id: str,
    *,
    repo: dict | None,
    owner_username: str | None,
    active: str,
) -> SandboxBackendProtocol:
    new_backend = await create_sandbox(
        thread_id,
        repo=repo,
        owner_username=owner_username,
        sandbox_type=active,
    )
    _SANDBOX_BACKENDS[thread_id] = (active, new_backend)
    return new_backend


async def _ensure(
    thread_id: str,
    *,
    repo: dict | None,
    owner_username: str | None,
    active: str,
) -> SandboxBackendProtocol:
    """确保线程有**指定类型**的可用沙箱。

    缓存命中且类型一致 → 复用;类型不同 → 停旧后端(不删数据)后重建;
    缓存丢失/沙箱被删 → 重建(统一布局:新旧后端共用同一宿主目录)。
    """
    cached = _SANDBOX_BACKENDS.get(thread_id)
    if cached is not None:
        cached_type, backend = cached
        if cached_type == active:
            return backend
        logger.info(
            "沙箱类型切换 thread=%s %s → %s(目录共用,数据无损)",
            thread_id,
            cached_type,
            active,
        )
        _stop_backend(backend)
    try:
        new_backend = await _create(
            thread_id,
            repo=repo,
            owner_username=owner_username,
            active=active,
        )
    except SandboxGoneError:
        # 沙箱被删 → 重建(幂等)
        logger.info("沙箱已丢失,重建 thread=%s type=%s", thread_id, active)
        new_backend = await _create(
            thread_id,
            repo=repo,
            owner_username=owner_username,
            active=active,
        )
    logger.info("新建沙箱后端 thread=%s type=%s id=%s", thread_id, active, new_backend.id)
    return new_backend


async def get_sandbox_backend(
    thread_id: str,
    *,
    repo: dict | None = None,
    owner_username: str | None = None,
) -> SandboxBackendProtocol:
    """取线程沙箱后端(工具侧入口;类型缺省 env,缓存命中直接返回)。"""
    cached = _SANDBOX_BACKENDS.get(thread_id)
    if cached is not None:
        return cached[1]
    active = resolve_sandbox_type()
    return await _ensure(
        thread_id, repo=repo, owner_username=owner_username, active=active
    )


def set_sandbox_backend(
    thread_id: str,
    backend: SandboxBackendProtocol,
    *,
    provider: str | None = None,
) -> None:
    """由图工厂注入线程沙箱(创建/复用后登记缓存)。"""
    provider = provider or str(getattr(backend, "provider", "") or "")
    _SANDBOX_BACKENDS[str(thread_id)] = (provider or "unknown", backend)


async def ensure_sandbox_for_thread(
    thread_id: str,
    config: RunnableConfig | None = None,
    *,
    repo: dict | None = None,
    owner_username: str | None = None,
    metadata: dict | None = None,
) -> SandboxBackendProtocol:
    """图工厂入口:确保线程沙箱可用并登记缓存。

    从 config 取 repo/owner/sandbox_type(缺省),然后创建或复用;
    类型 = run 级 configurable > 线程 metadata > env。
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
    active = resolve_sandbox_type(cfg, metadata)
    backend = await _ensure(
        thread_id,
        repo=repo,
        owner_username=owner_username,
        active=active,
    )
    return backend


def unwrap_sandbox_backend(backend: Any) -> Any:
    """解包可能的代理(本变体无代理,直接返回)。"""
    return backend
