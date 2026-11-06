"""线程 state 的进程内 LRU 缓存(idle 线程读加速;真值在 Postgres)。

设计书 §5.2:run-in-flight 的线程**不缓存**(每次 live 读);
idle 线程按 ``version``(state_updated_at/updated_at)命中缓存,
命中时**直接返回内存快照,不进运行时单循环**(超大线程不冻结的
第一层防线)。重启即丢,与 Postgres 持久化不冲突。
"""

import os
import time
from collections import OrderedDict
from typing import Any

# 缓存最大条目(env 可调,缺省 256)
_MAX_ENTRIES = int(os.environ.get("DASHBOARD_STATE_CACHE_MAX_ENTRIES", "256"))


class _Entry:
    """一条缓存:版本 + 快照 + 写入时间。"""

    def __init__(self, version: str, snapshot: dict[str, Any]) -> None:
        # 快照版本(线程的 state_updated_at/updated_at)
        self.version: str = version
        # 快照内容(ThreadState)
        self.snapshot: dict[str, Any] = snapshot
        # 写入时间(秒)
        self.stored_at: float = time.time()


class StateCache:
    """线程 state LRU 缓存。

    成员:
        _entries: OrderedDict[thread_id → _Entry](LRU 顺序)。
        _max_entries: 上限(超限时逐出最旧)。
    """

    def __init__(self, max_entries: int = _MAX_ENTRIES) -> None:
        # LRU 容器
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        # 上限
        self._max_entries: int = max_entries

    def get(self, thread_id: str, version: str) -> dict[str, Any] | None:
        """按 (thread_id, version) 取;命中则移到 LRU 头。"""
        entry = self._entries.get(thread_id)
        if entry is None or entry.version != version:
            return None
        self._entries.move_to_end(thread_id)
        return entry.snapshot

    def put(self, thread_id: str, version: str, snapshot: dict[str, Any]) -> None:
        """写缓存(超上限逐出最旧)。"""
        self._entries[thread_id] = _Entry(version, snapshot)
        self._entries.move_to_end(thread_id)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def invalidate(self, thread_id: str) -> None:
        """失效某线程(run 开始/完成时调)。"""
        self._entries.pop(thread_id, None)


# 进程内单例(单事件循环;无锁需求)
_state_cache = StateCache()


def state_cache() -> StateCache:
    """取进程内 state 缓存单例。"""
    return _state_cache
