"""线程记录注册表(内存;替代 inmem 的 ``conn.store["threads"]``)。

事实(已对运行库核实):Postgres **没有 threads 表**——inmem 把线程记录
(thread_id/created_at/updated_at/state_updated_at/metadata/status)只放
进程内存,持久化载体是 **checkpoint**:
- ``checkpoints.metadata`` 携带线程级 metadata(PrepareRunMiddleware 每步
  同步 local_user/repo/plan_mode/model/title 等);
- ``checkpoints`` 表没有 created_at 列,时间编码在 checkpoint_id 的
  UUIDv7 前 48bit。

因此本注册表:
- 启动时从 ``checkpoints`` 重建(每线程最新 root checkpoint 的 metadata 即
  线程 metadata;时间从 checkpoint_id 的 UUIDv7 时间戳派生);
- 运行期由路由/运行引擎更新(create/patch/run 开始与结束);
- 进程重启后与 inmem 行为一致:metadata 可重建,精确的
  created_at/updated_at 不可恢复,用 checkpoint 时间近似。
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _ckpt_ts(checkpoint_id: str) -> datetime | None:
    """checkpoint_id(UUIDv7)→ 创建时间。

    UUIDv7 前 48bit 是 100ns 纪元(1582-10-15)的 100ns 计数;UUIDv1 兜底。
    """
    if not checkpoint_id:
        return None
    try:
        import uuid

        u = uuid.UUID(checkpoint_id)
    except (ValueError, TypeError):
        return None
    if u.version == 7:
        epoch_100ns = datetime(1582, 10, 15, 0, 0, 0, tzinfo=UTC)
        return epoch_100ns + timedelta(microseconds=u.time // 10)
    if u.version == 1:
        try:
            return datetime.fromtimestamp(u.time / 1e-7, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    return None


@dataclass
class ThreadRecord:
    thread_id: str
    created_at: str
    updated_at: str
    state_updated_at: str
    metadata: dict[str, Any] = field(default_factory=dict)
    status: str = "idle"
    config: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "state_updated_at": self.state_updated_at,
            "metadata": self.metadata,
            "status": self.status,
            "config": self.config,
        }


class ThreadRegistry:
    """线程记录内存表(单事件循环,无锁)。"""

    def __init__(self) -> None:
        self._threads: dict[str, ThreadRecord] = {}

    # -- 生命周期 ----------------------------------------------------------

    async def rebuild_from_checkpoints(self, uri: str) -> int:
        """从 checkpoints 表重建线程表。返回重建的线程数。"""
        con = await _connect(uri)
        try:
            rows = await (
                await con.execute(
                    """
                    SELECT thread_id,
                           (array_agg(checkpoint_id ORDER BY checkpoint_id ASC))[1]
                               AS first_ckpt,
                           (array_agg(checkpoint_id ORDER BY checkpoint_id DESC))[1]
                               AS last_ckpt,
                           (array_agg(metadata ORDER BY checkpoint_id DESC))[1]
                               AS latest_meta
                    FROM checkpoints
                    WHERE checkpoint_ns = ''
                    GROUP BY thread_id
                    """
                )
            ).fetchall()
        finally:
            await con.close()
        for row in rows:
            tid = row["thread_id"]
            meta = row["latest_meta"] if isinstance(row["latest_meta"], dict) else {}
            first_at = _ckpt_ts(row["first_ckpt"])
            last_at = _ckpt_ts(row["last_ckpt"])
            created = first_at.isoformat() if first_at else _utcnow()
            updated = last_at.isoformat() if last_at else created
            self._threads[tid] = ThreadRecord(
                thread_id=tid,
                created_at=created,
                updated_at=updated,
                state_updated_at=updated,
                metadata=dict(meta),
                status="idle",
            )
        logger.info("runtime: 从 checkpoints 重建线程表 %d 条", len(self._threads))
        return len(self._threads)

    # -- 查询 --------------------------------------------------------------

    def get(self, thread_id: str) -> ThreadRecord | None:
        return self._threads.get(thread_id)

    def exists(self, thread_id: str) -> bool:
        return thread_id in self._threads

    def list_all(self) -> list[ThreadRecord]:
        return list(self._threads.values())

    # -- 变更 --------------------------------------------------------------

    def create(
        self,
        thread_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> ThreadRecord:
        now = _utcnow()
        record = ThreadRecord(
            thread_id=thread_id,
            created_at=now,
            updated_at=now,
            state_updated_at=now,
            metadata=dict(metadata or {}),
            status="idle",
        )
        self._threads[thread_id] = record
        return record

    def patch_metadata(
        self,
        thread_id: str,
        metadata: dict[str, Any],
    ) -> ThreadRecord | None:
        record = self._threads.get(thread_id)
        if record is None:
            return None
        record.metadata.update(metadata)
        record.updated_at = _utcnow()
        return record

    def delete(self, thread_id: str) -> None:
        self._threads.pop(thread_id, None)

    def touch_state(self, thread_id: str) -> None:
        record = self._threads.get(thread_id)
        if record is not None:
            record.state_updated_at = _utcnow()
            record.updated_at = _utcnow()

    def set_status(self, thread_id: str, status: str) -> None:
        record = self._threads.get(thread_id)
        if record is not None:
            record.status = status


async def _connect(uri: str):
    import psycopg
    from psycopg.rows import dict_row

    return await psycopg.AsyncConnection.connect(
        uri, autocommit=True, prepare_threshold=0, row_factory=dict_row
    )
