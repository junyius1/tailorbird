"""工具:``enter_plan_mode`` — 中途把 run 切到只读规划模式。"""

import logging
from typing import Annotated

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId
from langgraph.config import get_config
from langgraph.types import Command

logger = logging.getLogger(__name__)

# 进入 plan mode 后的提示(本地化:无 PR,审批后本地实现)
_ENTERED_MESSAGE = (
    "Plan mode 已激活。对目标仓库保持只读:研究代码库,"
    "在 `/workspace/plans/` 下创建或编辑一个带日期的自包含 HTML 工件,"
    "然后用 `save_plan` 工具发布,并在线程里告知用户等待审批。"
    "不要编辑仓库文件、不要提交、不要推送——等用户审批计划后再实现。"
)


async def enter_plan_mode(
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """中途激活 plan mode。

    当任务复杂、涉及多文件、有多种可行方案,认为需要先做结构化实现计划时调用。
    激活后对目标仓库保持只读;用 ``save_plan`` 发布计划工件;
    用户审批(前端批准按钮)后才开始实现。
    """
    thread_id = _thread_id_from_config()
    if thread_id:
        try:
            # 线程 metadata 落盘 plan_mode=True(best-effort;失败不阻断)
            from ..dashboard.runtime_client import langgraph_client

            client = langgraph_client(None)
            await client.threads.update(thread_id, metadata={"plan_mode": True})
        except Exception:  # noqa: BLE001
            logger.warning("持久化 plan mode 入口失败 thread=%s", thread_id, exc_info=True)
    return Command(
        update={
            "plan_mode": True,
            "messages": [ToolMessage(content=_ENTERED_MESSAGE, tool_call_id=tool_call_id)],
        }
    )


def _thread_id_from_config() -> str | None:
    """从 run config 取 thread_id(可空)。"""
    try:
        config = get_config()
    except Exception:  # noqa: BLE001
        return None
    configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
    thread_id = configurable.get("thread_id") if isinstance(configurable, dict) else None
    return str(thread_id) if isinstance(thread_id, str) and thread_id else None
