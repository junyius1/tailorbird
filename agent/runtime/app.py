"""自研运行时 FastAPI app(替代 ``langgraph dev`` 的进程)。

lifespan 启动顺序(全部 fail-closed,与 agent.api.app 同纪律):

1. ``load_config()``(.env 优先;DATABASE_URI 必填,缺失拒启);
2. checkpointer(store 表迁移自动跑)→ ``app.state.checkpointer``;
3. store → ``app.state.store``;
4. ThreadRegistry 从 checkpoints 重建 → ``app.state.threads``;
5. StateReader 工作线程(独立循环 + 自建连接)→ ``app.state.state_reader``。

端口:``RUNTIME_PORT``(缺省 2026,即顶替 ``langgraph dev``)。
"""

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import auth
from .config import load_config
from .deps import set_globals
from .state import StateReader, set_reader, state_cache
from .threads import ThreadRegistry

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from ..postgres_persistence import (
        create_checkpointer,
        create_store,
        require_database_uri,
    )

    config = load_config()
    require_database_uri()  # fail-closed
    app.state.runtime_config = config

    checkpointer_cm = create_checkpointer()
    store_cm = create_store()
    checkpointer = await checkpointer_cm.__aenter__()
    store = await store_cm.__aenter__()
    try:
        threads = ThreadRegistry()
        n = await threads.rebuild_from_checkpoints(config.database_uri)
        logger.info("runtime 启动:重建线程 %d 条", n)

        reader = StateReader(config.database_uri)
        set_reader(reader)

        app.state.checkpointer = checkpointer
        app.state.store = store
        app.state.threads = threads
        app.state.state_reader = reader

        set_globals(
            config=config,
            checkpointer=checkpointer,
            store=store,
            threads=threads,
            state_reader=reader,
        )
        auth.mark_ready()
        logger.info(
            "runtime 就绪 self_url=%s state_cache=%s",
            config.self_url,
            state_cache().stats(),
        )
        yield
    finally:
        reader_obj = getattr(app.state, "state_reader", None)
        if reader_obj is not None:
            reader_obj.close()
        try:
            await checkpointer_cm.__aexit__(None, None, None)
        finally:
            await store_cm.__aexit__(None, None, None)
        logger.info("runtime 关闭")


def create_app() -> FastAPI:
    config = load_config()
    app = FastAPI(
        title="tailorbird runtime",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/noauth/openapi.json",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(config.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from .routes import meta, store, threads as threads_routes

    app.include_router(meta.router)
    app.include_router(store.router)
    app.include_router(threads_routes.router)
    return app


app = create_app()


def main() -> None:
    """``python -m agent.runtime`` 入口(uvicorn)。"""
    import uvicorn

    port = int(os.environ.get("RUNTIME_PORT", "2026"))
    host = os.environ.get("RUNTIME_HOST", "0.0.0.0")
    uvicorn.run("agent.runtime.app:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
