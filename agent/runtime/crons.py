"""运行时 cron 调度(替代 langgraph_runtime_inmem 的 ``/runs/crons`` 面)。

- 内存注册表(与 run 记录同纪律:重启丢失;schedule 记录本身在 Postgres
  store,本地单用户场景可接受,与 inmem 行为一致);
- 每条 cron 一个调度轮询(主事件循环内,croniter 算下次触发);
- 触发 = ``RunRegistry.engine(...)`` 上直接开一个 scheduler/agent run
  (与 HTTP ``POST /runs`` 共享同一引擎,事件进同一线程缓冲);
- ``after_seconds`` 延时 run 复用同一注册表(单次触发)。

cron 记录字段与 inmem wire 对齐(取 dashboard ``schedules.py`` 实际发送的
子集):``cron``/``assistant_id``/``input``/``metadata``/``thread_id``?。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from datetime import UTC, datetime

from .engines import RunRegistry
from .threads import ThreadRegistry

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 1.0


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class CronRecord:
    cron_id: str
    cron: str
    assistant_id: str
    input: Any
    metadata: dict[str, Any] = field(default_factory=dict)
    thread_id: str | None = None
    created_at: str = field(default_factory=_utcnow)
    next_fire: float | None = None
    _task: asyncio.Task[None] | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cron_id": self.cron_id,
            "cron": self.cron,
            "assistant_id": self.assistant_id,
            "input": self.input,
            "metadata": self.metadata,
            "thread_id": self.thread_id,
            "created_at": self.created_at,
        }


class CronRegistry:
    """cron 注册表 + 调度轮询(单事件循环,无锁)。"""

    def __init__(self, *, run_registry: RunRegistry, threads: ThreadRegistry) -> None:
        self._runs = run_registry
        self._threads = threads
        self._crons: dict[str, CronRecord] = {}
        self._swept = False

    # -- CRUD ----------------------------------------------------------------

    def add(
        self,
        *,
        cron: str,
        assistant_id: str,
        input: Any,
        metadata: dict[str, Any] | None = None,
        thread_id: str | None = None,
    ) -> CronRecord:
        from croniter import croniter as _croniter

        if not isinstance(cron, str) or not _croniter.is_valid(cron):
            raise ValueError(f"invalid cron expression: {cron!r}")
        record = CronRecord(
            cron_id=str(uuid.uuid4()),
            cron=cron,
            assistant_id=assistant_id,
            input=input,
            metadata=dict(metadata or {}),
            thread_id=thread_id,
        )
        record.next_fire = self._next_fire(record)
        self._crons[record.cron_id] = record
        self._ensure_sweeper()
        logger.info(
            "cron 注册 id=%s expr=%s assistant=%s next=%.0f",
            record.cron_id, cron, assistant_id, record.next_fire or 0,
        )
        return record

    def get(self, cron_id: str) -> CronRecord | None:
        return self._crons.get(cron_id)

    def list(self) -> list[CronRecord]:
        return list(self._crons.values())

    def remove(self, cron_id: str) -> bool:
        record = self._crons.pop(cron_id, None)
        if record is None:
            return False
        self._cancel_task(record)
        logger.info("cron 删除 id=%s", cron_id)
        return True

    # -- 延时 run(after_seconds) --------------------------------------------

    def add_delayed_run(
        self,
        *,
        thread_id: str,
        assistant_id: str,
        input: Any,
        metadata: dict[str, Any] | None,
        after_seconds: float,
        user: str,
        source: str,
    ) -> str:
        """``after_seconds`` 延时 run:到点走 start_run。"""
        run_id = str(uuid.uuid4())
        delay = max(float(after_seconds), 0.0)

        async def _fire() -> None:
            try:
                await asyncio.sleep(delay)
                engine = self._runs.engine(thread_id, graph_id=self._graph_id(assistant_id))
                await engine.start_run(
                    assistant_id=assistant_id,
                    input=input,
                    metadata=metadata,
                    user=user,
                    source=source,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("延时 run 启动失败 thread=%s run=%s", thread_id, run_id, exc_info=True)

        task = asyncio.create_task(_fire(), name=f"delayed-run-{run_id[:8]}")
        self._crons[run_id] = CronRecord(
            cron_id=run_id,
            cron=f"after:{delay:.0f}s",
            assistant_id=assistant_id,
            input=input,
            metadata=dict(metadata or {}),
            thread_id=thread_id,
            _task=task,
        )
        logger.info("延时 run 注册 run=%s thread=%s 后 %.0fs", run_id, thread_id, delay)
        return run_id

    # -- 触发 ----------------------------------------------------------------

    def _graph_id(self, assistant_id: str) -> str:
        return assistant_id if assistant_id in ("agent", "scheduler") else "agent"

    @staticmethod
    def _next_fire(record: CronRecord) -> float | None:
        from croniter import croniter as _croniter

        try:
            return float(_croniter(record.cron, time.time()).get_next(float))
        except Exception:
            logger.warning("cron %s 计算下次触发失败", record.cron_id, exc_info=True)
            return None

    async def _fire(self, record: CronRecord) -> None:
        """到点:在 (新建) 线程上开 scheduler/agent run。"""
        thread_id = record.thread_id
        user = str((record.metadata or {}).get("local_user") or "local-user")
        if not thread_id or not self._threads.exists(thread_id):
            thread_id = str(uuid.uuid4())
            self._threads.create(
                thread_id,
                metadata={
                    "kind": "agent",
                    "source": "schedule",
                    "local_user": user,
                    "trigger_kind": "schedule",
                    "resolved": False,
                },
            )
        try:
            engine = self._runs.engine(thread_id, graph_id=self._graph_id(record.assistant_id))
            await engine.start_run(
                assistant_id=record.assistant_id,
                input=record.input,
                metadata=record.metadata,
                user=user,
                source="schedule",
            )
            logger.info("cron 触发 id=%s thread=%s", record.cron_id, thread_id)
        except Exception:
            logger.warning("cron 触发失败 id=%s", record.cron_id, exc_info=True)
        finally:
            record.next_fire = self._next_fire(record)

    # -- 调度轮询 -------------------------------------------------------------

    def _ensure_sweeper(self) -> None:
        if self._swept:
            return
        self._swept = True
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # 非运行期注册(测试)→ 不建任务
        asyncio.create_task(self._sweep(), name="cron-sweeper")

    async def _sweep(self) -> None:
        while True:
            now = time.time()
            for record in list(self._crons.values()):
                if record._task is not None:  # 延时 run:任务自己睡
                    if record._task.done():
                        self._crons.pop(record.cron_id, None)
                    continue
                if record.next_fire is not None and record.next_fire <= now:
                    record.next_fire = self._next_fire(record)
                    asyncio.create_task(self._fire(record), name=f"cron-{record.cron_id[:8]}")
            await asyncio.sleep(_POLL_INTERVAL)

    @staticmethod
    def _cancel_task(record: CronRecord) -> None:
        if record._task is not None and not record._task.done():
            record._task.cancel()
        record._task = None

    def close(self) -> None:
        for record in list(self._crons.values()):
            self._cancel_task(record)
        self._crons.clear()
