"""线程 state / history 读取(替代 langgraph_api 的 ``/state`` ``/history``)。

设计(针对超大线程,如 1795 条消息 / 28MB blob / delta checkpoint):

1. **不手拼 state**。直接用编译图的 ``aget_state`` / ``aget_state_history``:
   delta checkpoint 归并、blob 取回、消息反序列化、interrupt/tasks 解析全部
   由 langgraph 内部完成(与 inmem 运行时同一条代码路径)。checkpointer
   直接赋给图的 ``.checkpointer`` 属性(``_prepare_state_snapshot`` 读的是
   该属性,不是 config 注入——已实测验证)。

2. **同步重活不垄断事件循环**。整个"建图 + 读快照"在**专用工作线程**
   (常驻独立事件循环)里跑;psycopg 连接循环绑定,工作循环自建连接与
   checkpointer。主 loop 继续服务 SSE/心跳/小请求。工作循环只做
   **读**(stub 图,无沙箱、无工具、不碰 loop-affine 沙箱注册表——
   单事件循环铁律不受影响:沙箱只在 run 的模型调用里用,run 跑主循环)。

3. **快照 LRU 缓存**。version = 线程记录 ``state_updated_at``(缺失时回退
   最新 root checkpoint id)。一次慢读后,后续同版本读命中内存。
"""

import asyncio
import contextlib
import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable

from ..graphs.agent import traced_agent as _agent_factory

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 专用工作线程(常驻事件循环;承载"读 state"的重活)
# ---------------------------------------------------------------------------


class StateReader:
    """常驻工作线程 + 独立事件循环,执行 state/history 读取。

    - 工作循环里自建 psycopg 连接 + AsyncPostgresSaver(连接循环绑定);
    - 单 worker:读取之间天然串行,避免并行重建图;
    - 主 loop 用 ``loop.run_in_executor`` 提交协程工厂。
    """

    def __init__(self, database_uri: str) -> None:
        self._database_uri = database_uri
        self._loop: asyncio.AbstractEventLoop | None = None
        self._checkpointer: Any = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run_worker, name="runtime-state-reader", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(30):
            raise RuntimeError("state reader worker 启动超时")
        if self._error is not None:
            raise RuntimeError(f"state reader worker 启动失败: {self._error!r}")

    async def _init_worker(self) -> None:
        import psycopg
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        conn = await psycopg.AsyncConnection.connect(
            self._database_uri, autocommit=True, prepare_threshold=0
        )
        self._checkpointer = AsyncPostgresSaver(conn)

    def _run_worker(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._init_worker())
        except BaseException as exc:  # noqa: BLE001
            self._error = exc
            loop.close()
            self._ready.set()
            return
        self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                for task in asyncio.all_tasks(loop):
                    task.cancel()
                loop.run_until_complete(
                    asyncio.gather(*asyncio.all_tasks(loop), return_exceptions=True)
                )
            except Exception:  # noqa: BLE001
                pass
            finally:
                loop.close()
                asyncio.set_event_loop(None)

    async def run(self, coro_factory: Callable[[], Awaitable[Any]]) -> Any:
        """在主 loop 提交协程到工作线程执行。"""
        if self._loop is None or self._error is not None:
            raise RuntimeError(f"state reader 不可用: {self._error!r}")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            _get_reader_executor(), self._submit, coro_factory
        )

    def _submit(self, coro_factory: Callable[[], Awaitable[Any]]) -> Any:
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(coro_factory(), self._loop)
        return future.result()

    def close(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)


_reader_executor: Any = None


def _get_reader_executor() -> Any:
    global _reader_executor
    if _reader_executor is None:
        from concurrent.futures import ThreadPoolExecutor

        _reader_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="state-reader-exec"
        )
    return _reader_executor


# ---------------------------------------------------------------------------
# 读取逻辑(在工作线程的事件循环里执行)
# ---------------------------------------------------------------------------


async def _graph_for(checkpointer: Any, thread_id: str, **extra: Any) -> tuple[Any, Any]:
    """建 stub 图并挂 checkpointer(非执行态 → 无沙箱,loop 安全)。

    返回 (async_ctx, graph):``traced_agent`` 是 async 生成器,
    ``__aenter__`` yield 出图,**必须**经 async_ctx ``__aexit__`` 收尾。
    """
    configurable: dict[str, Any] = {"thread_id": thread_id, **extra}
    ctx = _agent_factory({"configurable": configurable})
    graph = await ctx.__aenter__()
    graph.checkpointer = checkpointer
    return ctx, graph


async def _read_state(
    checkpointer: Any, thread_id: str, checkpoint_id: str | None = None
) -> dict[str, Any]:
    extra = {"checkpoint_id": checkpoint_id} if checkpoint_id else {}
    ctx, graph = await _graph_for(checkpointer, thread_id, **extra)
    try:
        config = {"configurable": {"thread_id": thread_id, **extra}}
        snapshot = await graph.aget_state(config)
    finally:
        await ctx.__aexit__(None, None, None)
    if snapshot is None:
        raise KeyError(thread_id)
    return state_snapshot_to_thread_state(snapshot)


async def _read_history(
    checkpointer: Any, thread_id: str, limit: int, before: str | None
) -> list[dict[str, Any]]:
    ctx, graph = await _graph_for(checkpointer, thread_id)
    try:
        configurable: dict[str, Any] = {"thread_id": thread_id}
        if before:
            configurable["checkpoint_id"] = before
        config = {"configurable": configurable}
        items: list[dict[str, Any]] = []
        async for snapshot in graph.aget_state_history(config):
            items.append(state_snapshot_to_thread_state(snapshot))
            if len(items) >= limit:
                break
    finally:
        await ctx.__aexit__(None, None, None)
    return items


# ---------------------------------------------------------------------------
# ThreadState 组装(字段与 langgraph_api.state 逐一对齐)
# ---------------------------------------------------------------------------


def _patch_interrupt(interrupt: Any) -> dict[str, Any]:
    """Interrupt → 线形 schema(与 inmem 的 patch_interrupt 同语义)。"""
    try:
        from langgraph.types import Interrupt

        if not isinstance(interrupt, Interrupt):
            raw = dict(interrupt) if isinstance(interrupt, dict) else {}
            raw.pop("id", None)
            interrupt = Interrupt(**raw)
        return {
            "id": getattr(interrupt, "id", None),
            "value": interrupt.value,
            "resumable": getattr(interrupt, "resumable", None),
            "ns": getattr(interrupt, "ns", None),
            "when": getattr(interrupt, "when", None),
        }
    except Exception:  # noqa: BLE001
        if isinstance(interrupt, dict):
            return {
                "id": interrupt.get("id"),
                "value": interrupt.get("value"),
                "resumable": interrupt.get("resumable"),
                "ns": interrupt.get("ns"),
                "when": interrupt.get("when"),
            }
        return {
            "id": getattr(interrupt, "id", None),
            "value": getattr(interrupt, "value", None),
        }


def _config_to_checkpoint(config: dict[str, Any] | None) -> dict[str, Any] | None:
    if not config:
        return None
    configurable = config.get("configurable") or {}
    thread_id = configurable.get("thread_id")
    checkpoint_id = configurable.get("checkpoint_id")
    if not thread_id or not checkpoint_id:
        return None
    checkpoint: dict[str, Any] = {
        "thread_id": thread_id,
        "checkpoint_id": checkpoint_id,
    }
    if "checkpoint_ns" in configurable:
        checkpoint["checkpoint_ns"] = configurable["checkpoint_ns"] or ""
    if "checkpoint_map" in configurable:
        checkpoint["checkpoint_map"] = configurable["checkpoint_map"]
    return checkpoint


def _iso(value: Any) -> Any:
    if value is None or isinstance(value, str):
        return value
    with contextlib.suppress(Exception):
        return value.isoformat()
    return str(value)


def _jsonable(value: Any) -> Any:
    """把 state values 里残留的 LangChain 对象转成 JSON 可序列化线形。

    checkpoint 反序列化的 messages 通常是 BaseMessage 实例(带 tool_calls
    等),`model_dump(mode="json")` 与 SDK 客户端拿到的线形一致;未知对象
    退化为 dict / 字符串。
    """
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return _jsonable(dump(mode="json"))
        except Exception:  # noqa: BLE001
            pass
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:
        return str(value)
    except Exception:  # noqa: BLE001
        return None


def state_snapshot_to_thread_state(snapshot: Any) -> dict[str, Any]:
    """StateSnapshot → ThreadState 线形(与 langgraph_api 对齐)。"""

    def _task(t: Any) -> dict[str, Any]:
        t_state = getattr(t, "state", None)
        if t_state is not None and hasattr(t_state, "values"):
            nested = state_snapshot_to_thread_state(t_state)
            checkpoint = _config_to_checkpoint(getattr(t_state, "config", None))
        elif isinstance(t_state, dict):
            checkpoint = t_state.get("configurable")
            nested = None
        else:
            checkpoint = None
            nested = None
        return {
            "id": getattr(t, "id", None),
            "name": getattr(t, "name", None),
            "path": getattr(t, "path", None),
            "error": getattr(t, "error", None),
            "interrupts": [
                _patch_interrupt(i) for i in (getattr(t, "interrupts", None) or [])
            ],
            "checkpoint": checkpoint,
            "state": nested,
            "result": getattr(t, "result", None),
        }

    config = getattr(snapshot, "config", None)
    parent_config = getattr(snapshot, "parent_config", None)
    return _jsonable({
        "values": snapshot.values if snapshot.values is not None else {},
        "next": list(snapshot.next or []),
        "tasks": [_task(t) for t in (snapshot.tasks or [])],
        "metadata": snapshot.metadata or {},
        "created_at": _iso(getattr(snapshot, "created_at", None)),
        "checkpoint": _config_to_checkpoint(config),
        "parent_checkpoint": _config_to_checkpoint(parent_config),
        "interrupts": [
            _patch_interrupt(i) for i in (getattr(snapshot, "interrupts", None) or [])
        ],
        # 兼容旧字段
        "checkpoint_id": (
            (config or {}).get("configurable", {}).get("checkpoint_id")
            if config
            else None
        ),
        "parent_checkpoint_id": (
            (parent_config or {}).get("configurable", {}).get("checkpoint_id")
            if parent_config
            else None
        ),
    })


# ---------------------------------------------------------------------------
# 快照 LRU 缓存(version 键)
# ---------------------------------------------------------------------------


class _CacheEntry:
    __slots__ = ("version", "snapshot", "stored_at")

    def __init__(self, version: str, snapshot: dict[str, Any]) -> None:
        self.version = version
        self.snapshot = snapshot
        self.stored_at = time.time()


class SnapshotCache:
    """thread_id → (version, ThreadState) 的进程内 LRU。"""

    def __init__(self, max_entries: int) -> None:
        self._entries: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._max = max(1, max_entries)
        self._lock = threading.Lock()

    def get(self, thread_id: str, version: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._entries.get(thread_id)
            if entry is None or entry.version != version:
                return None
            self._entries.move_to_end(thread_id)
            return entry.snapshot

    def put(self, thread_id: str, version: str, snapshot: dict[str, Any]) -> None:
        with self._lock:
            self._entries[thread_id] = _CacheEntry(version, snapshot)
            self._entries.move_to_end(thread_id)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)

    def invalidate(self, thread_id: str) -> None:
        with self._lock:
            self._entries.pop(thread_id, None)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"entries": len(self._entries)}


_state_cache: SnapshotCache | None = None


def state_cache() -> SnapshotCache:
    global _state_cache
    if _state_cache is None:
        import os

        _state_cache = SnapshotCache(
            int(os.environ.get("RUNTIME_STATE_CACHE_MAX", "64") or "64")
        )
    return _state_cache


# ---------------------------------------------------------------------------
# 进程级 StateReader(由 app lifespan 创建)
# ---------------------------------------------------------------------------


_reader: StateReader | None = None


def set_reader(reader: StateReader) -> None:
    global _reader
    _reader = reader


def get_reader() -> StateReader:
    if _reader is None:
        raise RuntimeError("StateReader 未初始化(app lifespan 未启动?)")
    return _reader


# ---------------------------------------------------------------------------
# 路由调用的异步入口
# ---------------------------------------------------------------------------


async def latest_checkpoint_id(checkpointer: Any, thread_id: str) -> str | None:
    """最新 root checkpoint id(主 loop 上跑;单行索引查询,轻量)。

    供 state 缓存 version 的兜底:`state_updated_at` 缺失的老线程,
    version 用它(state 变 ⇒ checkpoint 变 ⇒ 缓存自然失效)。
    """
    tup = await checkpointer.aget_tuple(
        {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    )
    if tup is None:
        return None
    return tup.checkpoint.get("id")


async def read_state(
    thread_id: str,
    *,
    version: str,
    budget: float,
) -> tuple[dict[str, Any], bool]:
    """读最新 state。返回 (ThreadState, 是否命中缓存)。

    缓存 miss → 丢工作线程;``budget`` 秒超时抛 asyncio.TimeoutError
    (调用方 503,不降级空值——降级空值是旧故障的根源)。
    """
    if version:
        hit = state_cache().get(thread_id, version)
        if hit is not None:
            return hit, True
    reader = get_reader()

    async def _run() -> dict[str, Any]:
        return await _read_state(reader._checkpointer, thread_id)

    state = await asyncio.wait_for(reader.run(_run), timeout=budget)
    if version:
        state_cache().put(thread_id, version, state)
    return state, False


async def reader_state_at_checkpoint(
    thread_id: str,
    checkpoint_id: str,
    *,
    budget: float,
) -> dict[str, Any]:
    """读指定 checkpoint 的 state(不缓存)。"""
    reader = get_reader()

    async def _run() -> dict[str, Any]:
        return await _read_state(
            reader._checkpointer, thread_id, checkpoint_id=checkpoint_id
        )

    return await asyncio.wait_for(reader.run(_run), timeout=budget)


async def read_history(
    thread_id: str,
    limit: int,
    *,
    before: str | None,
    budget: float,
) -> list[dict[str, Any]]:
    """读 history(新→旧;limit 有界,不缓存)。"""
    reader = get_reader()

    async def _run() -> list[dict[str, Any]]:
        return await _read_history(reader._checkpointer, thread_id, limit, before)

    return await asyncio.wait_for(reader.run(_run), timeout=budget)
