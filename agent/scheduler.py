"""scheduler 图:cron tick 扇出到 agent run。

``StateGraph`` 单节点 ``launch``,按 ``task`` 分派:
- ``reconcile``:清理僵死 run(busy 但无 live run → 置 idle);
- 默认(缺省 task = schedule tick):``launch_scheduled_agent_run(schedule_id)``
  —— 读 store 里的 schedule 记录,新开一个 agent 线程并分发 durable run
  (source=schedule)。

本变体删除:baby_sit(CI 监视,强依赖 GitHub 检查)、
session_cost(按 Slack 线程刷成本)。
"""

import logging
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import RunnableConfig
from langgraph_sdk import get_client

from .dispatch import create_durable_run, dispatch_agent_run
from .input_messages import RunInput, build_run_input

logger = logging.getLogger(__name__)


class SchedulerState(TypedDict, total=False):
    """scheduler 图的 state(扁平字段,tick 输入)。"""

    # 任务类型(reconcile / schedule)
    task: str
    # schedule id
    schedule_id: str
    # schedule 名称
    name: str
    # 到点执行的 prompt
    prompt: str
    # 仓库自由文本 owner/name(可空)
    repo: str | None
    # 模型覆盖(可空)
    model_id: str | None
    # 推理力度(可空)
    effort: str | None
    # 是否启用
    enabled: bool
    # 触发人(本地 username)
    local_user: str
    # 扇出结果
    result: dict[str, Any]


async def launch_scheduled_agent_run(schedule_id: str) -> dict[str, Any]:
    """schedule tick:读记录 → 新开 agent 线程 → 分发 durable run。

    参数:
        schedule_id: store 里的 schedule 记录 id。
    返回:
        ``{"status": "launched", "thread_id", "run_id"}`` 或
        ``{"status": "skipped|error", "reason"}``。
    """
    client = get_client()
    # 读 schedule 记录(store 命名空间 schedules)
    try:
        item = await client.store.get_item(["schedules"], schedule_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("schedule %s 读取失败: %s", schedule_id, exc)
        return {"status": "error", "reason": "schedule record read failed"}
    value = getattr(item, "value", None) if not isinstance(item, dict) else item.get("value")
    if not isinstance(value, dict):
        return {"status": "skipped", "reason": "schedule record missing"}
    if not value.get("enabled", True):
        return {"status": "skipped", "reason": "schedule disabled"}
    prompt = value.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return {"status": "skipped", "reason": "schedule has no prompt"}
    owner = value.get("owner") or "local-user"
    name = value.get("name") or "scheduled run"

    # 新开一个 agent 线程(每次 tick 独立线程,便于按任务浏览)
    metadata = {
        "kind": "agent",
        "source": "schedule",
        "local_user": owner,
        "thread_category": "interactive",
        "trigger_kind": "schedule",
        "automation_id": schedule_id,
        "automation_name": name,
        "title": f"[定时] {name}",
        "model": value.get("model_id"),
        "effort": value.get("effort"),
    }
    try:
        thread = await client.threads.create(metadata=metadata)
    except Exception as exc:  # noqa: BLE001
        logger.warning("schedule %s 建线程失败: %s", schedule_id, exc)
        return {"status": "error", "reason": "thread create failed"}
    thread_id = thread.get("thread_id")

    # 解析仓库自由文本 → metadata(可选)
    if value.get("repo"):
        from .utils.repo import extract_repo_from_text

        repo = extract_repo_from_text(str(value["repo"]))
        if repo:
            metadata["repo"] = repo
            try:
                await client.threads.update(thread_id, metadata={"repo": repo})
            except Exception:  # noqa: BLE001
                pass

    # 分发 durable run(source=schedule)
    try:
        run = await dispatch_agent_run(
            thread_id,
            prompt,
            source="schedule",
            configurable={
                "source": "schedule",
                "local_user": owner,
                "model": value.get("model_id"),
                "effort": value.get("effort"),
            },
            metadata={"kind": "schedule", "trigger": "cron"},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("schedule %s 分发失败: %s", schedule_id, exc)
        return {"status": "error", "reason": "dispatch failed"}
    run_id = run.get("run_id") if isinstance(run, dict) else None
    return {"status": "launched", "thread_id": thread_id, "run_id": run_id}


async def reconcile_stale_runs() -> dict[str, Any]:
    """清理僵死 run:busy 但无 live run 的线程置回 idle。

    dev server 重启后,run 会消失但线程仍标 busy;
    本任务把这类线程的展示态修正为 idle(不丢 checkpoint)。
    """
    client = get_client()
    reconciled = 0
    try:
        # 只查 busy 线程(按 status 过滤;单用户本地,量小)
        threads = await client.threads.search(status="busy", limit=100)
    except Exception as exc:  # noqa: BLE001
        logger.warning("reconcile: 线程查询失败: %s", exc)
        return {"status": "error", "reason": "thread search failed"}
    for thread in threads or []:
        thread_id = thread.get("thread_id")
        if not thread_id:
            continue
        try:
            runs = await client.runs.list(thread_id, limit=5)
        except Exception:  # noqa: BLE001
            continue
        has_live = any(r.get("status") in ("pending", "running") for r in (runs or []))
        if not has_live:
            try:
                await client.threads.update(
                    thread_id, metadata={"status": "idle", "latest_run_status": None}
                )
                reconciled += 1
            except Exception:  # noqa: BLE001
                pass
    return {"status": "ok", "reconciled": reconciled}


async def _launch(state: SchedulerState, config: RunnableConfig) -> dict[str, Any]:
    """单节点:按 task 分派。"""
    configurable = config.get("configurable") or {}
    task = state.get("task") or configurable.get("task")
    if task == "reconcile":
        return {"result": await reconcile_stale_runs()}
    # 默认:schedule tick
    schedule_id = state.get("schedule_id") or configurable.get("schedule_id")
    if not isinstance(schedule_id, str) or not schedule_id:
        # 允许 tick 直接携带 prompt(手动 trigger 形态)
        prompt = state.get("prompt")
        if isinstance(prompt, str) and prompt.strip():
            local_user = state.get("local_user") or "local-user"
            try:
                client = get_client()
                thread = await client.threads.create(
                    metadata={
                        "kind": "agent",
                        "source": "schedule",
                        "local_user": local_user,
                        "trigger_kind": "schedule",
                        "title": f"[定时] {state.get('name') or 'scheduled run'}",
                    }
                )
                run = await dispatch_agent_run(
                    thread.get("thread_id", ""),
                    prompt,
                    source="schedule",
                    configurable={"source": "schedule", "local_user": local_user},
                )
                return {
                    "result": {
                        "status": "launched",
                        "thread_id": thread.get("thread_id"),
                        "run_id": run.get("run_id") if isinstance(run, dict) else None,
                    }
                }
            except Exception as exc:  # noqa: BLE001
                return {"result": {"status": "error", "reason": str(exc)[:200]}}
        logger.warning("schedule tick 缺 schedule_id 且无 prompt")
        return {"result": {"status": "missing_schedule_id"}}
    return {"result": await launch_scheduled_agent_run(schedule_id)}


def get_scheduler(config: RunnableConfig | None = None):
    """scheduler 图工厂(langgraph.json 指向;同步,无状态)。"""
    builder = StateGraph(SchedulerState)
    builder.add_node("launch", _launch)
    builder.add_edge(START, "launch")
    builder.add_edge("launch", END)
    return builder.compile().with_config(config or {})
