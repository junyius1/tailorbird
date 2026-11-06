"""Tailorbird 每个进程只允许运行在一个事件循环上(单事件循环铁律)。

沙箱后端按线程缓存在**进程级全局注册表**里,且与事件循环亲和(loop-affine)。
若 inmem 运行时的后台任务队列开了"隔离循环"(每个 worker 用自己的循环),
某次 run 留在旧循环注册表里的沙箱状态,下一次 run 换了循环就取不到 →
该线程被永久卡死。因此本模块强制关隔离循环,并在运行时模型变化时拒启动。
"""

import logging
import os
import sys

logger = logging.getLogger(__name__)

# 运行时后台任务队列的隔离循环开关(env 变量名,与 langgraph_api 一致)
ISOLATED_LOOPS_ENV = "BG_JOB_ISOLATED_LOOPS"
# 视为"开启"的取值集合(小写后比较)
_TRUTHY = {"1", "true", "yes", "on"}


def pin_single_event_loop() -> None:
    """强制队列把每个任务都跑在服务端主循环上。

    若开关无法强制关闭(运行时模型变了),直接抛 RuntimeError 拒启动——
    跑在隔离循环上比不启动更糟,因为它会失败成"永久不可用的线程"。
    """
    # 用户是否显式请求了隔离循环(仅用于告警)
    requested = os.environ.get(ISOLATED_LOOPS_ENV, "").strip().lower() in _TRUTHY
    # 强制关掉:覆盖"服务端在我们之后读配置"的场景;
    # 这里不 import langgraph_api.config,因为该模块一加载就要求 DATABASE_URI。
    os.environ[ISOLATED_LOOPS_ENV] = "false"

    # 运行时可能尚未加载 langgraph_api.config(dev 模式下 app import 期通常还没加载)
    langgraph_config = sys.modules.get("langgraph_api.config")
    if langgraph_config is None:
        return

    # 运行时模型变了(开关不复存在)→ 无法保证单循环,拒启动
    if not hasattr(langgraph_config, ISOLATED_LOOPS_ENV):
        msg = (
            f"langgraph_api.config 没有 {ISOLATED_LOOPS_ENV} 开关。"
            "队列的循环模型变了,Tailorbird 无法再保证每进程单事件循环。"
        )
        raise RuntimeError(msg)

    # 直接改写运行时读到的开关值
    setattr(langgraph_config, ISOLATED_LOOPS_ENV, False)
    if getattr(langgraph_config, ISOLATED_LOOPS_ENV):
        msg = f"无法强制关闭 {ISOLATED_LOOPS_ENV}"
        raise RuntimeError(msg)

    if requested:
        logger.warning(
            "%s 被设置了,已强制关闭。Tailorbird 要求每进程单事件循环。",
            ISOLATED_LOOPS_ENV,
        )
