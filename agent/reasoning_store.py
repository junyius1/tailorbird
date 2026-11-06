"""LLM 推理消息独立大表持久化 + 懒加载支撑(设计书 §7.1,本变体新增)。

推理(reasoning/thinking)消息必须显示在 UI,但不能放进 checkpoint——
它是超大线程体积的主因。方案:推理全文**另存 Postgres 专表**
``llm_reasoning``,checkpoint 里继续剥离;UI 打开线程**不加载**推理,
用户点开 "Thought" 的 ``>`` 时才拉取单条。

为什么专表而不是塞 checkpoint_blobs / BaseStore:checkpoint 是 pickle 整状态
快照,读一条要反序列化全量 state(单事件循环重活);BaseStore 走 JSON 序列化,
设计存小记录。专表 + 窄 SQL 只读一行 TOAST 文本,不碰任何 Python 反序列化。

- 写入方(唯一写者):``middleware/strip_thoughts.py`` 在剥离前 upsert;
  best-effort(5s 超时 + 1 次重试,失败仅 warning,不得让 run 失败)。
- 读方:dashboard 进程用**只读短连接**直查(独立小池,不进运行时单循环)。
- TTL:随 checkpoint 12h TTL 同周期(60min)清扫;删线程时级联删该线程全部行。
"""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)

# 建表 DDL(幂等)
# psycopg 单语句 execute:建表与建索引分开(两条 DDL)
_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS llm_reasoning (
  thread_id      TEXT NOT NULL,
  message_id     TEXT NOT NULL,        -- AIMessage.id,UI 消息对象的 id,天然对齐
  run_id         TEXT NOT NULL,
  model          TEXT,                 -- 产生该推理的模型 id
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  content        TEXT NOT NULL,        -- 推理全文(单条可达数百 KB)
  content_length INT  NOT NULL,        -- 预计算,列表接口不碰 TOAST
  PRIMARY KEY (thread_id, message_id)
)
"""
_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS ix_llm_reasoning_thread_time
  ON llm_reasoning (thread_id, created_at)
"""
# 幂等 upsert:重复写入(同 run 重试)不产生重复行
_UPSERT_SQL = """
INSERT INTO llm_reasoning (thread_id, message_id, run_id, model, content, content_length)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT (thread_id, message_id) DO NOTHING
"""
# 删线程级联清理
_DELETE_THREAD_SQL = "DELETE FROM llm_reasoning WHERE thread_id = %s"
# 推理清单(不含正文;按时间升序)
_LIST_THREAD_SQL = """
SELECT message_id, run_id, model, created_at, content_length
FROM llm_reasoning
WHERE thread_id = %s
ORDER BY created_at ASC
LIMIT 1000
"""
# 单条正文
_GET_ONE_SQL = """
SELECT message_id, content
FROM llm_reasoning
WHERE thread_id = %s AND message_id = %s
LIMIT 1
"""
# TTL 清扫:thread_id 已不在 checkpoints 表的线程,其推理行整体删除
_SWEEP_SQL = """
DELETE FROM llm_reasoning
WHERE thread_id NOT IN (SELECT DISTINCT thread_id FROM checkpoints)
"""
# 写超时(秒):推理是展示数据,不得让 run 失败
_WRITE_TIMEOUT_SECONDS = 5.0
# 写重试次数(1 次)
_WRITE_RETRIES = 1

# dashboard 只读连接池(独立小池 min=1/max=2,不进运行时单循环)
_read_pool: Any = None


@asynccontextmanager
async def _connect(
    *,
    row_factory=None,
    connect_timeout: float | None = None,
) -> AsyncIterator[psycopg.AsyncConnection]:
    """短连接 async CM:psycopg 3.3 的 ``AsyncConnection.connect`` 是协程,
    不是 async CM,这里统一包一层(用后必关)。"""
    kwargs: dict[str, Any] = dict(
        autocommit=True,
        prepare_threshold=0,
    )
    if row_factory is not None:
        kwargs["row_factory"] = row_factory
    if connect_timeout is not None:
        kwargs["connect_timeout"] = connect_timeout
    conn = await psycopg.AsyncConnection.connect(_DATABASE_URI, **kwargs)
    try:
        yield conn
    finally:
        await conn.close()


def _database_uri() -> str:
    """读取 DATABASE_URI(.env 优先,与 postgres_persistence 同语义)。"""
    try:
        from dotenv import dotenv_values

        env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
        file_value = (dotenv_values(env_file).get("DATABASE_URI") or "").strip()
        if file_value:
            return file_value
    except Exception:
        pass
    return os.environ.get("DATABASE_URI", "").strip()


_DATABASE_URI = _database_uri()


async def setup() -> None:
    """建表/建索引(随 validate_persistence_startup 执行;失败拒启)。"""
    if not _DATABASE_URI:
        raise RuntimeError("DATABASE_URI 未设置,无法建 llm_reasoning 表")
    async with _connect() as conn:
        await conn.execute(_CREATE_TABLE_SQL)
        await conn.execute(_CREATE_INDEX_SQL)


async def upsert(
    thread_id: str,
    message_id: str,
    run_id: str,
    model: str | None,
    content: str,
) -> bool:
    """幂等写入一条推理(best-effort:超时/失败仅 warning,不抛)。

    返回:
        是否写入成功(True/False);失败不中断 run。
    """
    if not _DATABASE_URI or not content:
        return False
    attempts = 1 + _WRITE_RETRIES
    for attempt in range(attempts):
        try:
            async with _connect(connect_timeout=_WRITE_TIMEOUT_SECONDS) as conn:
                await conn.execute(
                    _UPSERT_SQL,
                    (thread_id, message_id, run_id, model, content, len(content)),
                )
            return True
        except Exception as exc:  # noqa: BLE001 —— 推理是展示数据,不得让 run 失败
            logger.warning(
                "llm_reasoning 写入失败(第 %d/%d 次,thread=%s msg=%s): %s",
                attempt + 1,
                attempts,
                thread_id,
                message_id,
                exc,
            )
    return False


async def delete_thread(thread_id: str) -> int:
    """删线程时级联删除该线程全部推理行;返回删除行数。"""
    if not _DATABASE_URI:
        return 0
    async with _connect() as conn:
        cursor = await conn.execute(_DELETE_THREAD_SQL, (thread_id,))
        return cursor.rowcount or 0


async def list_thread(thread_id: str) -> list[dict[str, Any]]:
    """推理清单(不含正文,按 created_at 升序,limit 1000)。"""
    if not _DATABASE_URI:
        return []
    async with _connect(row_factory=dict_row) as conn:
        cursor = await conn.execute(_LIST_THREAD_SQL, (thread_id,))
        return list(await cursor.fetchall())


async def get_one(thread_id: str, message_id: str) -> dict[str, Any] | None:
    """单条推理正文;无则 None(UI 显示"无推理记录")。"""
    if not _DATABASE_URI:
        return None
    async with _connect(row_factory=dict_row) as conn:
        cursor = await conn.execute(_GET_ONE_SQL, (thread_id, message_id))
        row = await cursor.fetchone()
        return row


async def sweep_orphans() -> int:
    """TTL 清扫:删掉 checkpoints 表里已不存在的线程的推理行。

    由调度周期任务(或 dashboard 启动时的后台任务)每 60min 调一次,
    与 checkpointer TTL 同周期。
    """
    if not _DATABASE_URI:
        return 0
    try:
        async with _connect() as conn:
            cursor = await conn.execute(_SWEEP_SQL)
            return cursor.rowcount or 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("llm_reasoning 清扫失败: %s", exc)
        return 0


@asynccontextmanager
async def read_pool() -> AsyncIterator[psycopg.AsyncConnection]:
    """dashboard 只读短连接(每次取一个短连接,用完即还)。

    独立于运行时 checkpointer 的连接池;最小化池避免占 Postgres 连接数。
    """
    async with _connect(row_factory=dict_row) as conn:
        yield conn
