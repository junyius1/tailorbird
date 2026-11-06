"""Postgres 持久化(2024 运行时 inmem 模式的 checkpointer 与 store 后端)。

设计书 §7:Postgres 是**唯一**持久化,无 InMemory / 文件 / pickle 降级。
- ``DATABASE_URI`` 缺失 → ``create_checkpointer``/``create_store`` 直接抛异常,
  使进程启动失败(fail-closed),绝不带内存态运行;
- ``.langgraph_api/`` 的 pickle 落盘(inmem 默认)不作为状态来源。

通过 ``langgraph.json`` 接线:

    "checkpointer": {"path": "agent.postgres_persistence:create_checkpointer", ...}
    "store":        {"path": "agent.postgres_persistence:create_store"}
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from psycopg.rows import dict_row

# .env 文件路径:优先于进程 env(sandbox/CI 的 shell 可能导出占位值,不能盖掉部署配置)
_ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")


def _database_uri() -> str:
    """读取 DATABASE_URI。

    .env 优先于进程 env;两者都取不到则返回空串(调用方负责 fail-closed)。
    """
    try:
        from dotenv import dotenv_values

        file_value = (dotenv_values(_ENV_FILE).get("DATABASE_URI") or "").strip()
        if file_value:
            return file_value
    except Exception:  # dotenv 缺失时退回纯进程 env
        pass
    return os.environ.get("DATABASE_URI", "").strip()


# 进程内缓存的 URI(模块加载时读取一次)
_DATABASE_URI: str = _database_uri()
# 连接池大小(可经 env 调小;运行时单循环下连接不宜多)
_POOL_MIN_SIZE: int = int(os.environ.get("POSTGRES_PERSISTENCE_POOL_MIN_SIZE", "1"))
_POOL_MAX_SIZE: int = int(os.environ.get("POSTGRES_PERSISTENCE_POOL_MAX_SIZE", "5"))
# 连接公共参数:autocommit(checkpointer 自管事务)、prepare_threshold=0
# (psycopg3 预处理语句在部分代理/网络下不稳定)、dict_row(字典行)
_CONN_KWARGS: dict[str, Any] = {"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}


def require_database_uri() -> str:
    """返回已设置的 DATABASE_URI;未设置时抛 RuntimeError(fail-closed)。"""
    if not _DATABASE_URI:
        raise RuntimeError(
            "DATABASE_URI is required; there is no in-memory or file fallback"
        )
    return _DATABASE_URI


def create_checkpointer():
    """返回 async 上下文管理器,产出 ``AsyncPostgresSaver``(setup 时跑迁移)。

    本变体无内存降级:``DATABASE_URI`` 缺失时直接抛异常拒启。
    """
    uri = require_database_uri()

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg_pool import AsyncConnectionPool

    @asynccontextmanager
    async def _make() -> AsyncIterator[AsyncPostgresSaver]:
        # 连接池:checkpointer 的所有读写都走这里
        pool = AsyncConnectionPool(
            uri,
            min_size=_POOL_MIN_SIZE,
            max_size=_POOL_MAX_SIZE,
            kwargs=dict(_CONN_KWARGS),
        )
        async with pool:
            # saver.setup() 建 checkpoint 三表(失败 → 异常外抛 → 启动失败)
            saver = AsyncPostgresSaver(conn=pool)
            await saver.setup()
            yield saver

    return _make()


def create_store():
    """返回 async 上下文管理器,产出 ``AsyncPostgresStore``(setup 时跑迁移)。

    长期记忆(plan/技能/指令/profile/用户注册表/消息队列)都落在这。
    """
    uri = require_database_uri()

    from langgraph.store.postgres.aio import AsyncPostgresStore

    @asynccontextmanager
    async def _make() -> AsyncIterator[AsyncPostgresStore]:
        async with AsyncPostgresStore.from_conn_string(
            uri,
            pool_config={
                "min_size": _POOL_MIN_SIZE,
                "max_size": _POOL_MAX_SIZE,
                "kwargs": dict(_CONN_KWARGS),
            },
        ) as store:
            await store.setup()
            yield store

    return _make()
