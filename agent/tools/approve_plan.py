"""工具:``approve_plan`` — 批准当前计划并退出 plan mode。

用户批准计划/要求离开 plan mode/要求开始实现时调用。
本地变体:无 PR、无 reviewer;批准即翻 plan store 状态 + 线程 metadata
(``plan_mode=False``),随后由同一次 run 继续实现(不新开 run)。
"""

import logging
from typing import Annotated, Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId
from langgraph.config import get_config
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from typing_extensions import TypedDict

from ..dashboard.plan_store import (
    ConflictError,
    get_plan,
    list_plan_comments,
    set_plan_status,
)
from ..dashboard.runtime_client import langgraph_client

logger = logging.getLogger(__name__)


class ApprovePlanState(TypedDict, total=False):
    """approve_plan 需要的 state 字段。"""

    # plan mode 开关
    plan_mode: bool


async def approve_plan(
    state: Annotated[ApprovePlanState | None, InjectedState] = None,
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
) -> Command | dict[str, Any]:
    """批准当前计划并退出 plan mode。

    计划就绪且用户批准时调用。翻 plan store 状态为 ``approved``、
    置线程 ``plan_mode=False``,并附用户评审意见(若有)供实现参考。

    参数:
        state: 注入的 agent state(取 plan_mode)。
        tool_call_id: 工具调用 id(回 ToolMessage 用)。
    返回:
        成功 → ``Command``(更新 plan_mode + 附加批准消息);
        失败 → ``{"success": False, "error"}``。
    """
    config = _safe_config()
    configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
    if not isinstance(configurable, dict):
        configurable = {}
    thread_id = configurable.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        return {"success": False, "error": "run config 缺少 thread_id"}
    try:
        if not _active_plan_mode(state, configurable):
            return {"success": False, "error": "当前线程未处于 plan mode"}
        plan = await get_plan(thread_id)
        if plan is None:
            return {"success": False, "error": "线程无已保存的计划,无法批准"}
        if plan.get("status") in ("approved", "rejected"):
            return {"success": False, "error": f"计划已处于终态 {plan.get('status')}"}
        comments = await list_plan_comments(thread_id)
        # 翻状态(幂等:并发下第二个调用方拿到 ConflictError → 409 语义)
        await set_plan_status(thread_id, "approved")
        # 线程 metadata:plan_mode=False + plan_status=approved(best-effort)
        await _update_thread_metadata(thread_id, configurable)
        message = _approved_message(comments)
    except ConflictError as exc:
        return {"success": False, "error": f"计划已终态: {exc}"}
    except Exception as exc:  # noqa: BLE001
        logger.exception("approve_plan 失败 thread=%s", thread_id)
        return {"success": False, "error": f"批准计划失败: {exc}"}
    return Command(
        update={
            "plan_mode": False,
            "messages": [ToolMessage(content=message, tool_call_id=tool_call_id)],
        }
    )


def _safe_config() -> dict[str, Any]:
    """安全取 run config。"""
    try:
        config = get_config()
        return config if isinstance(config, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _active_plan_mode(
    state: Any, configurable: dict[str, Any]
) -> bool:
    """plan mode 是否激活(state 或 configurable 任一为 True)。"""
    if isinstance(state, dict) and state.get("plan_mode") is True:
        return True
    return configurable.get("plan_mode") is True


async def _update_thread_metadata(
    thread_id: str, configurable: dict[str, Any]
) -> None:
    """线程 metadata 落盘:plan_mode=False + plan_status=approved。

    经运行时客户端(HTTP loopback,同进程内走 loopback transport)。
    best-effort:失败只告警,不阻断批准(状态以 plan store 为准)。
    """
    user = configurable.get("local_user")
    try:
        client = langgraph_client(str(user) if user else None)
        await client.threads.update(
            thread_id,
            metadata={"plan_mode": False, "plan_status": "approved"},
        )
    except Exception:  # noqa: BLE001
        logger.warning("更新线程 plan 元数据失败(best-effort)", exc_info=True)


def _format_comments(comments: list[dict[str, Any]]) -> str:
    """评论 → 文本(供批准消息附带的实现参考)。"""
    if not comments:
        return ""
    lines = []
    for comment in comments[-20:]:
        author = comment.get("author_login") or "unknown"
        content = str(comment.get("content") or "").strip()
        if content:
            lines.append(f"- {author}: {content}")
    if not lines:
        return ""
    return "\n\n用户评审意见(实现时参考):\n" + "\n".join(lines)


def _approved_message(comments: list[dict[str, Any]]) -> str:
    """批准成功后的工具消息正文。"""
    feedback = _format_comments(comments)
    base = "计划已批准,plan mode 已退出。现在开始按计划实现;完成后做必要的本地提交(本地分支,不推送)。"
    return base + feedback
