"""langgraph.json 指向的 scheduler 图入口。

实现见 ``agent.scheduler.get_scheduler``(cron tick 扇出);
本模块仅为 graphs 命名空间提供入口符号。
"""

from agent.scheduler import get_scheduler

__all__ = ["get_scheduler"]
