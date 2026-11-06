"""run 完成信号处理(进程内 loopback webhook 的业务层)。

上游平台在 run 终结(成功/失败)时 POST ``/webhooks/run-complete?token=…``
(本变体经进程内 loopback 投递,不走网络)。

- ``verify_run_complete_token``:fail-closed token 校验(secret 未设 → 全拒);
- ``handle_run_completion``:按 ``run_id`` 幂等收尾——
  - success → 记日志(本变体无 Slack 成本刷新);
  - 终态失败(error/timeout)→ 线程级失败标记落 metadata(dashboard 可见);
  - interrupted → 不算失败,只同步状态。
"""

import hmac
import logging
import os
from typing import Any

from langgraph_sdk import get_client

logger = logging.getLogger(__name__)

# 完成 webhook 的共享 secret(与 dispatch 侧同一 env;未设 → 路由 fail-closed)
RUN_COMPLETE_WEBHOOK_SECRET = os.environ.get("RUN_COMPLETE_WEBHOOK_SECRET", "")
if not RUN_COMPLETE_WEBHOOK_SECRET:
    logger.warning(
        "RUN_COMPLETE_WEBHOOK_SECRET 未设置;/webhooks/run-complete 将 fail-closed"
    )

# 终态失败状态集
_TERMINAL_FAILURE_STATUSES = frozenset({"error", "timeout"})
# metadata 里"已处理失败回执"的 run id 列表键(幂等去重)
_POSTED_FAILURE_RUN_IDS = "failure_ack_run_ids"


def verify_run_complete_token(token: str | None) -> bool:
    """完成 webhook token 校验(fail-closed)。

    未配置 secret → 拒绝一切调用(而非放行匿名请求)。
    """
    if not RUN_COMPLETE_WEBHOOK_SECRET:
        return False
    return token is not None and hmac.compare_digest(token, RUN_COMPLETE_WEBHOOK_SECRET)


def _posted_failure_run_ids(metadata: dict[str, Any]) -> list[str]:
    """metadata 里已处理失败回执的 run id 列表。"""
    value = metadata.get(_POSTED_FAILURE_RUN_IDS)
    return [str(x) for x in value] if isinstance(value, list) else []


def _add_failure_run_id(metadata: dict[str, Any], run_id: str) -> dict[str, Any]:
    """把 run_id 加入已处理失败列表(去重,上限 50)。"""
    ids = _posted_failure_run_ids(metadata)
    if run_id not in ids:
        ids.append(run_id)
    return {"failure_ack_run_ids": ids[-50:]}


async def handle_run_completion(payload: dict[str, Any]) -> dict[str, str]:
    """处理 run 完成回调(按 run_id 幂等)。

    参数:
        payload: webhook 体 ``{"status", "thread_id", "run_id", "metadata"?}``。
    返回:
        ``{"status": "ok|ignored|error", "reason": ...}``。
    """
    status = payload.get("status")
    thread_id = payload.get("thread_id")
    raw_run_id = payload.get("run_id")
    run_id = raw_run_id if isinstance(raw_run_id, str) and raw_run_id else None
    if not isinstance(thread_id, str) or not thread_id:
        return {"status": "ignored", "reason": "missing thread_id"}

    client = get_client()

    if status == "success":
        # 成功:同步展示态为 idle(best-effort);本变体无 Slack 成本刷新
        try:
            await client.threads.update(
                thread_id, metadata={"latest_run_status": "success", "status": "idle"}
            )
        except Exception:  # noqa: BLE001
            logger.warning("run-complete: 成功态同步失败 thread=%s", thread_id, exc_info=True)
        logger.info("run 完成 success thread=%s run=%s", thread_id, run_id)
        return {"status": "ok", "reason": "success acknowledged"}

    # interrupted:不算失败,仅同步状态
    if status == "interrupted":
        try:
            await client.threads.update(
                thread_id, metadata={"latest_run_status": "interrupted", "status": "interrupted"}
            )
        except Exception:  # noqa: BLE001
            pass
        return {"status": "ok", "reason": "interrupted acknowledged"}

    if status not in _TERMINAL_FAILURE_STATUSES:
        return {"status": "ignored", "reason": f"non-failure status: {status}"}

    # 终态失败:幂等落线程级失败标记
    try:
        thread = await client.threads.get(thread_id)
    except Exception:  # noqa: BLE001
        logger.warning("run-complete: 取线程失败 thread=%s", thread_id, exc_info=True)
        return {"status": "error", "reason": "thread fetch failed"}
    metadata = (thread or {}).get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    # 幂等:同一 run 的失败回执只处理一次
    if run_id and run_id in _posted_failure_run_ids(metadata):
        return {"status": "ignored", "reason": "failure already handled for run"}
    try:
        update: dict[str, Any] = {
            "latest_run_status": status,
            "status": "error",
            "failure_text": f"run {status or 'failed'}",
        }
        if run_id:
            update.update(_add_failure_run_id(metadata, run_id))
        await client.threads.update(thread_id, metadata=update)
    except Exception:  # noqa: BLE001
        logger.warning("run-complete: 失败标记落盘失败 thread=%s", thread_id, exc_info=True)
        return {"status": "error", "reason": "metadata update failed"}
    logger.info("run 终态失败 thread=%s run=%s status=%s", thread_id, run_id, status)
    return {"status": "ok", "reason": "failure marker recorded"}
