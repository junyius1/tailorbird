"""V2 run 引擎(单事件循环,无 Redis)。

两条创建路径共享同一引擎:
- dashboard V1 ``POST /threads/{id}/runs``(fire-and-forget,完成靠 webhook);
- 浏览器 V2 ``run.start`` 命令(``POST /threads/{id}/commands``,
  观察走 ``POST /threads/{id}/stream/events`` SSE)。

每个线程一个 :class:`ThreadRunEngine`:
- 单调 ``seq`` + 有界环形事件缓冲(支持 ``since`` 重放 / ``resume_gap``);
- 运行记录(内存;Postgres 无 runs 表——与 inmem 一致,崩溃后丢活跃 run);
- 活跃 run 的 asyncio task + ``RunControl``(取消=drain 语义,interrupt
  时 checkpoint 里保留 pending 状态,与 ``multitask_strategy=interrupt`` 一致);
- 终态:run 记录 + 线程 metadata(latest_run_status/latest_run_id)+
  状态缓存失效 + 合成根级 lifecycle 终帧 + webhook loopback。

执行路径铁律:run 跑在**主事件循环**(沙箱注册表 loop-affine;
``traced_agent`` 内部 ``pin_single_event_loop()``)。绝不在
StateReader 的 worker 线程/循环里驱动图。
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from .normalize import EventNormalizer, synth_lifecycle
from .state import state_cache
from .threads import ThreadRegistry

logger = logging.getLogger(__name__)

# run 状态机(与 SDK RunStatus 对齐)
RUN_PENDING = "pending"
RUN_RUNNING = "running"
RUN_SUCCESS = "success"
RUN_ERROR = "error"
RUN_TIMEOUT = "timeout"
RUN_INTERRUPTED = "interrupted"
_TERMINAL = frozenset({RUN_SUCCESS, RUN_ERROR, RUN_TIMEOUT, RUN_INTERRUPTED})

# 终态 → 线程 status 映射(inmem wire 词汇:busy/idle/error/interrupted)。
# dashboard ``get_thread_active_status`` 判活与 reconcile 搜索都按 ``status=="busy"``
# 过滤,必须与 inmem 一致(展示态由 dashboard 从 latest_run_status 另行推导)。
_STATUS_TO_THREAD = {
    RUN_PENDING: "busy",
    RUN_RUNNING: "busy",
    RUN_ERROR: "error",
    RUN_TIMEOUT: "error",
    RUN_INTERRUPTED: "interrupted",
    RUN_SUCCESS: "idle",
}


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _custom_matches(name: Any, channels: frozenset[str]) -> bool:
    if "custom" in channels:
        return True
    if isinstance(name, str):
        return f"custom:{name}" in channels
    return False


# ---------------------------------------------------------------------------
# run 记录
# ---------------------------------------------------------------------------


@dataclass
class RunRecord:
    run_id: str
    thread_id: str
    assistant_id: str
    status: str
    created_at: str
    updated_at: str
    metadata: dict[str, Any] = field(default_factory=dict)
    multitask_strategy: str = "interrupt"

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "assistant_id": self.assistant_id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
            "multitask_strategy": self.multitask_strategy,
        }


# ---------------------------------------------------------------------------
# 每线程引擎
# ---------------------------------------------------------------------------


@dataclass
class _Sub:
    """一条 SSE 连接 = 一个订阅(channels/namespaces/depth 过滤)。"""

    channels: frozenset[str]
    namespaces: tuple[tuple[str, ...], ...]
    depth: int
    queue: asyncio.Queue[dict[str, Any] | None] = field(default_factory=asyncio.Queue)

    def matches(self, method: str, namespace: list[str], data: Any = None) -> bool:
        channel = "input" if method == "input.requested" else method
        if channel == "custom":
            # custom:<name> 按 data.name 过滤;裸 custom 通配
            name = data.get("name") if isinstance(data, dict) else None
            if not _custom_matches(name, self.channels):
                return False
        elif channel not in self.channels:
            return False
        if not self.namespaces:
            return True
        return any(
            tuple(namespace[: len(prefix)]) == prefix and len(namespace) - len(prefix) <= self.depth
            for prefix in self.namespaces
        )


class ThreadRunEngine:
    """单线程的 run 编排 + 事件缓冲(单事件循环,无锁)。"""

    def __init__(
        self,
        thread_id: str,
        *,
        checkpointer: Any,
        store: Any,
        threads: ThreadRegistry,
        buffer_size: int,
        graph_id: str = "agent",
    ) -> None:
        self.thread_id = thread_id
        self._checkpointer = checkpointer
        self._store = store
        self._threads = threads
        self._buffer: deque[dict[str, Any]] = deque(maxlen=max(buffer_size, 16))
        self._seq = 0
        self._runs: dict[str, RunRecord] = {}
        self._subs: dict[str, _Sub] = {}
        self._active: str | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._run_control: Any = None
        self._pending_interrupts: dict[str, list[str]] = {}
        self.last_assistant_id: str | None = None
        self._graph_id = graph_id
        self._cancel_action: str | None = None
        self._terminal_emitted = False

    # -- 事件 -----------------------------------------------------------------

    def _assign(self, method: str, namespace: list[str], data: Any, node: str | None = None) -> tuple[int, str]:
        self._seq += 1
        return self._seq, f"{self.thread_id}:{self._seq}:{uuid.uuid4().hex[:8]}"

    def append(self, envelope: dict[str, Any]) -> None:
        self._buffer.append(envelope)
        params = envelope.get("params") or {}
        for sub in list(self._subs.values()):
            if sub.matches(envelope.get("method", ""), params.get("namespace", []), params.get("data")):
                try:
                    sub.queue.put_nowait(envelope)
                except asyncio.QueueFull:
                    pass

    def _buffer_since(self, since: int | None) -> tuple[list[dict[str, Any]], bool]:
        """since 重放。(events, resume_gap)"""
        if since is None or since < 0:
            return list(self._buffer), False
        events = [e for e in self._buffer if isinstance(e.get("seq"), int) and e["seq"] > since]
        if self._buffer and events and events[0]["seq"] > since + 1:
            return events, True
        return events, False

    # -- 查询 -----------------------------------------------------------------

    @property
    def seq(self) -> int:
        return self._seq

    def pending_interrupt_ids(self) -> set[str]:
        """引擎跟踪的 pending 中断 id 集合(空集 = 尚未跟踪,不做硬校验)。"""
        return set(self._pending_interrupts.keys())

    # -- 订阅 -----------------------------------------------------------------

    def subscribe(self, channels: set[str], namespaces: list[list[str]], depth: int) -> tuple[str, _Sub]:
        sub = _Sub(
            channels=frozenset(channels),
            namespaces=tuple(tuple(ns) for ns in namespaces),
            depth=max(depth, 1),
        )
        sid = uuid.uuid4().hex
        self._subs[sid] = sub
        return sid, sub

    def unsubscribe(self, sub_id: str) -> None:
        self._subs.pop(sub_id, None)

    # -- run 生命周期 -----------------------------------------------------------

    def active_run(self) -> RunRecord | None:
        if self._active is None:
            return None
        return self._runs.get(self._active)

    def latest_run(self) -> RunRecord | None:
        if self._runs:
            return sorted(self._runs.values(), key=lambda r: r.created_at, reverse=True)[0]
        return None

    def list_runs(self, limit: int = 10) -> list[RunRecord]:
        runs = sorted(self._runs.values(), key=lambda r: r.created_at, reverse=True)
        return runs[: max(limit, 1)]

    def get_run(self, run_id: str) -> RunRecord | None:
        return self._runs.get(run_id)

    # -- 启动 -----------------------------------------------------------------

    async def start_run(
        self,
        *,
        assistant_id: str,
        input: Any,
        config: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        multitask_strategy: str = "interrupt",
        user: str = "local-user",
        source: str = "dashboard",
        durability: str | None = None,
    ) -> RunRecord:
        """创建 run 并后台驱动。返回 RunRecord(创建即 pending)。"""
        # multitask_strategy=interrupt:中止活跃 run
        active = self.active_run()
        if active is not None and active.status in (RUN_PENDING, RUN_RUNNING):
            if multitask_strategy == "interrupt":
                self.request_cancel(active.run_id, action="interrupt", internal=True)
            elif multitask_strategy == "reject":
                raise RunBusyError(active.run_id)
            elif multitask_strategy == "enqueue":
                # 简化:enqueue 未实现 → 按 interrupt 处理(本地场景无并发排队)
                self.request_cancel(active.run_id, action="interrupt", internal=True)

        self.last_assistant_id = assistant_id
        run_id = str(uuid.uuid4())
        now = _utcnow()
        record = RunRecord(
            run_id=run_id,
            thread_id=self.thread_id,
            assistant_id=assistant_id,
            status=RUN_PENDING,
            created_at=now,
            updated_at=now,
            metadata=dict(metadata or {}),
            multitask_strategy=multitask_strategy,
        )
        self._runs[run_id] = record
        self._active = run_id
        self._terminal_emitted = False
        self._pending_interrupts.clear()
        # 线程 status(inmem 词汇 busy)
        self._threads.set_status(self.thread_id, "busy")
        self._threads.patch_metadata(
            self.thread_id,
            {"latest_run_status": RUN_RUNNING, "latest_run_id": run_id, "status": "busy"},
        )

        self._run_task = asyncio.create_task(
            self._drive(
                record,
                input=input,
                config=config or {},
                user=user,
                source=source,
                durability=durability,
            ),
            name=f"run-{run_id[:8]}",
        )
        logger.info(
            "run 启动 thread=%s run=%s assistant=%s source=%s",
            self.thread_id,
            run_id,
            assistant_id,
            source,
        )
        return record

    # -- 驱动 -----------------------------------------------------------------

    async def _drive(
        self,
        record: RunRecord,
        *,
        input: Any,
        config: dict[str, Any],
        user: str,
        source: str,
        durability: str | None = None,
    ) -> None:
        from .graph_exec import build_run_config, get_exec_graph

        logger.debug(
            "run config 构建 thread=%s run=%s keys=%s",
            self.thread_id,
            record.run_id,
            sorted((config.get("configurable") or {}).keys()),
        )

        configurable = dict(config.get("configurable") or {})
        configurable["thread_id"] = self.thread_id
        configurable["local_user"] = configurable.get("local_user") or user
        configurable.setdefault("source", source)
        # 线程级沙箱默认(run 级 configurable.sandbox_type 优先)
        meta_sandbox = (record.metadata or {}).get("sandbox_type")
        if isinstance(meta_sandbox, str) and meta_sandbox.strip():
            configurable.setdefault("sandbox_type", meta_sandbox.strip())
        run_config, run_control = build_run_config(
            self.thread_id,
            configurable,
            checkpointer=self._checkpointer,
            store=self._store,
            metadata=record.metadata,
            run_id=record.run_id,
            user=user,
            source=source,
        )
        record.metadata["source"] = source
        record.metadata["local_user"] = user
        self._run_control = run_control

        normalizer = EventNormalizer(self._assign)
        record.status = RUN_RUNNING
        record.updated_at = _utcnow()
        self._threads.patch_metadata(self.thread_id, {"latest_run_status": RUN_RUNNING})
        self.append(synth_lifecycle(self._assign, [], "started", graph_name=self._graph_id))

        status = RUN_SUCCESS
        error_msg: str | None = None
        try:
            graph, ctx = await get_exec_graph(
                self._checkpointer,
                self._store,
                run_config,
                graph_id=self._graph_id,
            )
            try:
                drive_kwargs: dict[str, Any] = dict(_RUN_TRANSFORMERS())
                if durability:
                    drive_kwargs["durability"] = durability
                stream = await graph.astream_events(
                    input,
                    run_config,
                    version="v3",
                    **drive_kwargs,
                )
                async with stream:
                    async for event in stream:
                        for envelope in normalizer.handle(event):
                            self._track_pending(envelope)
                            self.append(envelope)
            finally:
                await ctx.__aexit__(None, None, None)
            # 终态判定:drain(取消) → interrupted;
            # 自然结束但状态有 pending 中断 → interrupted;否则 success。
            drained = bool(getattr(run_control, "drain_requested", False))
            if drained or await self._has_pending_interrupts_in_state(graph, run_config):
                status = RUN_INTERRUPTED
        except asyncio.CancelledError:
            # 仅在 drain 前 task.cancel 兜底路径到达;补发终态后重抛
            await self._finish(record, RUN_INTERRUPTED, None, run_control, force=True)
            raise
        except Exception as exc:
            # drain 取消:GraphDrained 是预期终止,不是错误
            if type(exc).__name__ == "GraphDrained" or getattr(run_control, "drain_requested", False):
                status = RUN_INTERRUPTED
                logger.info("run 被 drain 取消 thread=%s run=%s", self.thread_id, record.run_id)
            else:
                status = RUN_ERROR
                error_msg = f"{type(exc).__name__}: {exc}"
                logger.exception("run 失败 thread=%s run=%s", self.thread_id, record.run_id)
        finally:
            await self._finish(record, status, error_msg, run_control)

    def _track_pending(self, envelope: dict[str, Any]) -> None:
        method = envelope.get("method")
        if method == "input.requested":
            data = (envelope.get("params") or {}).get("data") or {}
            iid = data.get("interrupt_id")
            ns = envelope.get("params", {}).get("namespace", [])
            if isinstance(iid, str) and iid:
                self._pending_interrupts[iid] = ns

    async def _has_pending_interrupts_in_state(self, graph: Any, config: dict[str, Any]) -> bool:
        try:
            snapshot = await graph.aget_state(config)
            if snapshot is None:
                logger.warning("aget_state 返回 None thread=%s", self.thread_id)
                return False
            tasks = getattr(snapshot, "tasks", None) or ()
            for task in tasks:
                interrupts = (
                    task.get("interrupts") if isinstance(task, dict) else getattr(task, "interrupts", None)
                )
                if interrupts:
                    for it in interrupts:
                        iid = getattr(it, "id", None)
                        if isinstance(iid, str):
                            self._pending_interrupts.setdefault(iid, [])
                    return True
            logger.info(
                "终态判定 thread=%s tasks=%s next=%s → 无 pending 中断",
                self.thread_id,
                [getattr(t, "name", None) if not isinstance(t, dict) else t.get("name") for t in tasks],
                getattr(snapshot, "next", None),
            )
        except Exception:
            logger.warning("aget_state 读中断失败 thread=%s", self.thread_id, exc_info=True)
        return False

    # -- 取消 -----------------------------------------------------------------

    def request_cancel(self, run_id: str, action: str = "interrupt", *, internal: bool = False) -> bool:
        """取消活跃 run:``RunControl.request_drain`` → 在**节点边界**优雅停,
        checkpoint 保留已完成进度(interrupt 语义;rollback 本地按 interrupt 处理)。"""
        record = self._runs.get(run_id)
        if record is None:
            return False
        if record.status in _TERMINAL:
            return False
        self._cancel_action = action
        if self._run_control is not None:
            try:
                self._run_control.request_drain("cancel:" + action)
                logger.info("run drain 请求 thread=%s run=%s action=%s", self.thread_id, run_id, action)
                return True
            except Exception:
                logger.debug("request_drain 失败,回退 task.cancel", exc_info=True)
        if self._run_task is not None and not self._run_task.done():
            self._run_task.cancel()
        return True

    # -- 终态 -----------------------------------------------------------------

    async def _finish(
        self,
        record: RunRecord,
        status: str,
        error_msg: str | None,
        run_control: Any,
        *,
        force: bool = False,
    ) -> None:
        if not force and record.status in _TERMINAL:
            return
        record.status = status
        record.updated_at = _utcnow()
        self._active = None if self._active == record.run_id else self._active
        self._run_task = None
        self._run_control = None
        self._cancel_action = None

        if not self._terminal_emitted:
            self._terminal_emitted = True
            if status == RUN_INTERRUPTED and not self._pending_interrupts:
                # 被 drain 但没有 pending 中断(如启动前取消)→ 按 interrupted 发
                pass
            self.append(
                synth_lifecycle(
                    self._assign,
                    [],
                    {"success": "completed", "error": "failed", "timeout": "failed"}.get(
                        status, "interrupted" if status == RUN_INTERRUPTED else status
                    ),
                    graph_name=self._graph_id,
                    error=error_msg,
                )
            )
        # 线程展示态 + 状态缓存失效
        self._threads.set_status(self.thread_id, _STATUS_TO_THREAD.get(status, "idle"))
        self._threads.touch_state(self.thread_id)
        self._threads.patch_metadata(
            self.thread_id,
            {
                "latest_run_status": status,
                "latest_run_id": record.run_id,
                "status": _STATUS_TO_THREAD.get(status, "idle"),
            },
        )
        state_cache().invalidate(self.thread_id)
        logger.info("run 终态 thread=%s run=%s status=%s", self.thread_id, record.run_id, status)
        await _dispatch_webhook(self.thread_id, record)


class RunBusyError(Exception):
    def __init__(self, active_run_id: str) -> None:
        super().__init__(f"thread has an active run: {active_run_id}")
        self.active_run_id = active_run_id


def _RUN_TRANSFORMERS() -> dict[str, Any]:
    """v3 默认 mux 只有 values/messages/lifecycle/subgraph;补上
    checkpoints/updates 两个变换器以对齐 V2 通道。"""
    try:
        from langgraph.stream.transformers import (
            CheckpointsTransformer,
            UpdatesTransformer,
        )

        return {"transformers": [CheckpointsTransformer, UpdatesTransformer]}
    except Exception:
        logger.debug("stream transformers 不可用,仅默认通道", exc_info=True)
        return {}


# ---------------------------------------------------------------------------
# webhook loopback
# ---------------------------------------------------------------------------


async def _dispatch_webhook(thread_id: str, record: RunRecord) -> None:
    """run 终结 → 回调 dashboard 的 /webhooks/run-complete(token fail-closed)。

    与 langgraph dev 的进程内 loopback 等价:同进程形态下运行时与 dashboard
    挂载在同一 FastAPI app 上;独立进程形态(webapp :8002)回调打到
    dashboard 服务。secret 未设 → 不投递(路由本身 fail-closed)。
    """
    secret = os.environ.get("RUN_COMPLETE_WEBHOOK_SECRET", "").strip()
    if not secret:
        return
    path = os.environ.get("COMPLETION_WEBHOOK_URL") or "/webhooks/run-complete"
    if path.startswith("http://") or path.startswith("https://"):
        url = path
    else:
        separator = "&" if "?" in path else "?"
        base = os.environ.get("RUNTIME_WEBHOOK_TARGET") or "http://localhost:8002"
        url = f"{base.rstrip('/')}{path}{separator}token={secret}"
    payload = {
        "status": record.status,
        "thread_id": thread_id,
        "run_id": record.run_id,
        "metadata": record.metadata,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            logger.debug(
                "run-complete webhook thread=%s run=%s → %d",
                thread_id,
                record.run_id,
                resp.status_code,
            )
    except Exception as exc:
        logger.warning(
            "run-complete webhook 投递失败 thread=%s run=%s: %s",
            thread_id,
            record.run_id,
            exc,
        )


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------


class RunRegistry:
    """线程 → 引擎(懒创建)。单事件循环,无锁。"""

    def __init__(
        self,
        *,
        checkpointer: Any,
        store: Any,
        threads: ThreadRegistry,
        buffer_size: int,
    ) -> None:
        self._checkpointer = checkpointer
        self._store = store
        self._threads = threads
        self._buffer_size = buffer_size
        self._engines: dict[str, ThreadRunEngine] = {}

    def engine(self, thread_id: str, graph_id: str = "agent") -> ThreadRunEngine:
        eng = self._engines.get(thread_id)
        if eng is None:
            eng = ThreadRunEngine(
                thread_id,
                checkpointer=self._checkpointer,
                store=self._store,
                threads=self._threads,
                buffer_size=self._buffer_size,
                graph_id=graph_id,
            )
            self._engines[thread_id] = eng
        return eng

    def get_engine(self, thread_id: str) -> ThreadRunEngine | None:
        return self._engines.get(thread_id)
