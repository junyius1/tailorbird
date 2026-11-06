"""LLM 调用并发控制(设计书 §10.5):队列模式 / 并发模式。

本地 vLLM 常以 ``--max-num-seqs 1`` 启动(同时只处理 1 个序列),
多线程/多用户的 run 不能并发向 LLM 发请求。本模块提供进程内 FIFO 闸:

- ``OPEN_SWE_LLM_MAX_CONCURRENCY=1``(缺省,队列/串行):全进程同一时刻
  最多 1 个进行中的 LLM 调用,其余在进程内队列按 FIFO 等待;
- ``≥ 2``(并发模式):全进程最多 N 个并发 LLM 调用。

gate 挂在模型调用**外层**(见 ``LLMGateMiddleware``):
**等待 gate 的时间不计模型调用超时**(排队 5 分钟不触发误杀)。

为什么进程内 semaphore 就够:单事件循环铁律保证所有 model call 都在同一
loop 上,``asyncio.Semaphore`` 天然是全进程唯一的 FIFO 闸;
多进程部署下 dashboard 进程不直接调 LLM,无冲突。
"""

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware

logger = logging.getLogger(__name__)

# 观测端点用的等待者记录上限
_MAX_WAITING_RECORDS = 100


@dataclass
class _WaitingEntry:
    """一个正在 gate 外排队等待的 LLM 调用。"""

    # 发起调用的线程 id(观测用)
    thread_id: str
    # 入队时间(毫秒时间戳)
    queued_at_ms: int


def _max_concurrency() -> int:
    """读取并发上限(缺省 1 = 队列串行)。"""
    try:
        value = int(os.environ.get("OPEN_SWE_LLM_MAX_CONCURRENCY", "1"))
    except ValueError:
        value = 1
    return max(1, value)


class LLMGate:
    """进程内 LLM 并发闸(FIFO)。

    成员:
        _semaphore: 并发上限的 asyncio 信号量(单事件循环下即全进程闸)。
        _in_flight: 当前持有槽位的调用数(观测用)。
        _waiting: 排队等待者记录(观测用,先进先出)。
        _loop: 闸绑定的事件循环 id(gate 必须在单循环上创建/使用)。
    """

    def __init__(self, max_concurrency: int) -> None:
        # 并发上限
        self._max_concurrency: int = max_concurrency
        # FIFO 信号量(等待者按到达顺序放行)
        self._semaphore: asyncio.Semaphore = asyncio.Semaphore(max_concurrency)
        # 当前进行中的调用数
        self._in_flight: int = 0
        # 排队中的等待者(仅观测,不进闸逻辑)
        self._waiting: deque[_WaitingEntry] = deque(maxlen=_MAX_WAITING_RECORDS)
        # 创建时的 loop(跨 loop 使用时重建,防"不同 loop 的信号量")
        self._loop: int | None = id(asyncio.get_running_loop())

    @staticmethod
    def _current_thread_id() -> str:
        """从当前 run 的 config 里取 thread_id(观测用;取不到返回 ?)。"""
        try:
            from langgraph.config import get_config

            config = get_config()
            thread_id = (config.get("configurable") or {}).get("thread_id")
            return str(thread_id) if thread_id else "?"
        except Exception:
            return "?"

    async def acquire(self) -> float:
        """获取槽位;返回排队等待的秒数(0 = 未等待)。

        单事件循环下 Semaphore 等待者按到达顺序放行,
        因此"等待队列"与 _waiting 记录严格 FIFO 对齐。
        """
        now = time.monotonic()
        thread_id = self._current_thread_id()
        # 热路径:有可用槽位时不记等待者
        if self._semaphore.locked():
            self._waiting.append(_WaitingEntry(thread_id, int(time.time() * 1000)))
        await self._semaphore.acquire()
        waited = time.monotonic() - now
        if self._waiting:
            # 自己是 FIFO 队首,摘除
            self._waiting.popleft()
            logger.info("llm-gate: thread=%s 获得槽位(等待 %.1fs)", thread_id, waited)
        # 持有槽位 → 进行中 +1(与 release 严格配对)
        self._in_flight += 1
        return waited

    def release(self) -> None:
        """释放槽位(与 acquire 严格配对)。"""
        self._in_flight = max(0, self._in_flight - 1)
        self._semaphore.release()

    def snapshot(self) -> dict[str, Any]:
        """观测端点快照:``/debug/llm-gate`` 的响应体。"""
        return {
            "mode": "queue" if self._max_concurrency == 1 else "concurrent",
            "max_concurrency": self._max_concurrency,
            "in_flight": self._in_flight,
            "waiting": [
                {"thread_id": entry.thread_id, "queued_at_ms": entry.queued_at_ms}
                for entry in list(self._waiting)
            ],
        }


# 模块级单例:全进程唯一一个 gate(单事件循环 = 全进程唯一 FIFO 闸)
_gate: LLMGate | None = None

def get_llm_gate() -> LLMGate:
    """取进程内 LLM gate 单例(缺省并发 1 = 队列串行)。

    懒创建:import 期没有事件循环,不能在模块级建 Semaphore。
    """
    global _gate
    if _gate is None:
        _gate = LLMGate(_max_concurrency())
    return _gate


# ---------------------------------------------------------------------------
# 中间件:gate 挂在模型调用外层
# ---------------------------------------------------------------------------


class LLMGateMiddleware(AgentMiddleware):
    """模型调用外层 gate 中间件(排队时间不计模型超时)。

    ``awrap_model_call`` 先拿 gate 槽位、再调 handler(handler 内层才有
    ModelCallTimeoutMiddleware 的 deadline),因此排队等待不触发模型超时误杀。
    必须继承 ``AgentMiddleware``:langchain ``create_agent`` 工厂按类属性
    探测各 hook,裸类会被拒绝。
    """

    name: str = "LLMGateMiddleware"

    async def awrap_model_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        """包一层 gate:``async with gate: await handler(request)``。"""
        gate = get_llm_gate()
        await gate.acquire()
        try:
            # 拿到槽位后才开始内层(含超时)的计时
            return await handler(request)
        finally:
            gate.release()
