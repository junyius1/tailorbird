"""FastAPI 应用组装(本地自托管变体,无 GitHub/Slack/Linear)。

同一份 app 两种部署形态(见 ``agent.webapp``):
- 同进程:``langgraph.json`` 的 ``http.app`` 挂进运行时(缺省 :2026);
- 双进程:``uvicorn agent.webapp:app --port 8002``,浏览器只连 :8002,内部以复合 bearer 调运行时。

lifespan(启动校验,fail-closed):
1. ``pin_single_event_loop()``(单循环铁律,二次);
2. ``validate_persistence_startup()``(Postgres 必填:checkpointer/store
   setup 成功,失败拒启);
3. ``validate_sandbox_startup_config()``(docker:daemon 可达 + 镜像可解析);
4. ``validate_local_dev_llm_config()``(本地 vLLM 配置);
5. ``reasoning_store.setup()``(建 llm_reasoning 专表)。
退出:``close_cached_models()``。
"""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# .env 优先于进程 env(项目铁律)。
# 同进程形态下 CLI 已按 langgraph.json 加载 .env,这里重复加载幂等;
# 双进程形态下 standalone ``uvicorn agent.webapp:app`` 不读 .env,
# 必须在此显式加载(否则拿不到 token/CORS/webhook secret/LANGGRAPH_URL)。
try:
    from dotenv import load_dotenv

    load_dotenv(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".env"),
        override=True,
    )
except Exception:  # dotenv 缺失不阻断启动(下方各 env 读取自行 fail-closed)
    logging.getLogger(__name__).debug("dotenv 不可用,跳过 .env 加载", exc_info=True)

from ..dashboard.agent_instructions import router as agent_instructions_router
from ..dashboard.auth import router as auth_router
from ..dashboard.options import router as options_router
from ..dashboard.langgraph_proxy import router as langgraph_proxy_router
from ..dashboard.plan_api import plan_router
from ..dashboard.profiles import router as profiles_router
from ..dashboard.schedules import router as schedules_router
from ..dashboard.skills import router as skills_router
from ..dashboard.thread_api import router as thread_router
from ..dashboard.user_instructions import router as user_instructions_router
from ..dashboard.workflow_approval_api import workflow_approval_router
from ..utils.event_loop import pin_single_event_loop
from ..utils.model import close_cached_models, validate_local_dev_llm_config
from ..utils.queue_debug import QueueDebugMiddleware, enable_from_env
from ..utils.sandbox import validate_sandbox_startup_config
from .health import router as health_router

logger = logging.getLogger(__name__)

# 队列调试 worker 读此 env 建 worker;Open SWE 不能容忍它们落在不同循环
pin_single_event_loop()


async def validate_persistence_startup() -> None:
    """Postgres-only 持久化启动校验(fail-closed)。

    - ``DATABASE_URI`` 必填;
    - checkpointer / store 的 ``setup()`` 必须成功(真实连库建表);
    - ``reasoning_store.setup()`` 建 llm_reasoning 专表;
    任一失败 → 抛异常拒启(无 InMemory/文件备用)。

    checkpointer/store 工厂返回 async 上下文管理器(连接池随其生命周期);
    这里只**借用**其 setup 阶段做真实连库校验,校验完立即释放
    (运行时的池由 langgraph 运行时在取 checkpointer/store 时各自建立)。
    """
    from ..postgres_persistence import (
        create_checkpointer,
        create_store,
        require_database_uri,
    )
    from ..reasoning_store import setup as reasoning_setup

    require_database_uri()

    async def _verify(cm_factory: object) -> None:
        """进入工厂产出的 async CM 一次(setup 连库建表),再退出释放。"""
        cm = cm_factory()
        async with cm:
            pass

    # checkpointer:setup 真实连 Postgres(失败即抛)
    await _verify(create_checkpointer)
    # store:setup 真实连 Postgres
    await _verify(create_store)
    # llm_reasoning 专表
    await reasoning_setup()
    logger.info("持久化校验通过(Postgres checkpointer/store/llm_reasoning)")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """启动校验链(fail-closed)+ 退出清理。"""
    pin_single_event_loop()
    await validate_persistence_startup()
    # docker 校验会跑 `docker info`(阻塞 subprocess):进线程池,不堵单事件循环
    import asyncio

    await asyncio.to_thread(validate_sandbox_startup_config)
    validate_local_dev_llm_config()
    logger.info("Tailorbird dashboard app 启动完成")
    try:
        yield
    finally:
        await close_cached_models()


def create_app() -> FastAPI:
    """组装 FastAPI app(中间件 + 路由)。"""
    app = FastAPI(lifespan=lifespan)
    # 队列调试(在飞请求表)
    app.add_middleware(QueueDebugMiddleware)
    enable_from_env()
    # CORS:按 DASHBOARD_ALLOWED_ORIGINS;拒绝 *
    allowed_origins = [
        origin.strip()
        for origin in os.environ.get("DASHBOARD_ALLOWED_ORIGINS", "").split(",")
        if origin.strip()
    ]
    if "*" in allowed_origins:
        raise RuntimeError(
            "DASHBOARD_ALLOWED_ORIGINS must not include '*' when allow_credentials=True"
        )
    if allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["*"],
        )
    # 路由(全部挂 /dashboard/api 前缀,UI 只代理该前缀)
    app.include_router(auth_router, prefix="/dashboard/api")
    app.include_router(thread_router)
    # LangGraph 协议代理须在 thread_router 之后注册:同路径时 thread_router 的
    # 定制端点(有界 state/history、SSE 透传)优先,协议面补齐 runs/* 等
    app.include_router(langgraph_proxy_router)
    app.include_router(plan_router)
    app.include_router(workflow_approval_router)
    app.include_router(schedules_router)
    app.include_router(options_router)
    app.include_router(skills_router)
    app.include_router(agent_instructions_router)
    app.include_router(user_instructions_router)
    app.include_router(profiles_router)
    # 健康 / 完成 webhook / 调试(无前缀,运行时 loopback 直接打)
    app.include_router(health_router)
    return app


app = create_app()
