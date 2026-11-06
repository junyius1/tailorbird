"""工具:``save_plan`` — 把沙箱内的 HTML 计划工件发布为可评审计划。

本地变体:存储走 Tailorbird plan store(store 命名空间 ``plans``);
状态模型为 ``pending``(保存即进入待审批)。
"""

import logging
from collections.abc import Mapping
from typing import Annotated, Any

from langgraph.config import get_config
from langgraph.prebuilt import InjectedState

from ..dashboard.plan_store import save_plan as save_plan_to_store
from ..sandbox_state import get_sandbox_backend

logger = logging.getLogger(__name__)

# 计划文件最大行数
_MAX_PLAN_LINES = 20_000
# 计划文件所在目录(沙箱内)
PLAN_FILE_DIRECTORY = "/workspace/plans"
# 保存计划作者标识(取 run 的 local_user)
_AUTHOR_FALLBACK = "local-user"


async def save_plan(
    plan_file_path: str,
    state: Annotated[dict[str, Any] | None, InjectedState] = None,
) -> dict[str, Any]:
    """从沙箱发布自包含的 HTML 计划工件。

    plan mode 下工件就绪后调用;plan mode 外用于分享长回复。
    在 ``/workspace/plans/`` 下创建一个完整 ``.html`` 文档并传其路径。
    文档要求:描述性 ``<title>``、内联 CSS、显式浅色/深色 token、
    响应式布局、无 script/form/embed;Google Fonts 是唯一外部资源。

    参数:
        plan_file_path: 沙箱内 HTML 工件路径。
        state: 注入的 agent state(用于取 plan_mode 上下文)。
    返回:
        ``{success: True, path}`` 或 ``{success: False, error}``。
    """
    if not isinstance(plan_file_path, str):
        return {"success": False, "error": "plan_file_path 必须是字符串"}
    path = plan_file_path.strip()
    if not path:
        return {"success": False, "error": "plan_file_path 不能为空"}
    if not _is_html_path(path):
        return {
            "success": False,
            "error": f"plan_file_path 必须指向 {PLAN_FILE_DIRECTORY} 下的 HTML 文件",
        }
    # 取线程 id 与作者
    config = _safe_config()
    thread_id, author = _thread_and_author(config)
    if not thread_id:
        return {"success": False, "error": "run config 缺少 thread_id"}
    try:
        content = (await _read_plan_file(str(thread_id), path)).strip()
        if not content:
            return {"success": False, "error": "计划文件不能为空"}
        error = _validate_html(content)
        if error:
            return {"success": False, "error": error}
        await save_plan_to_store(str(thread_id), content, "html", author)
    except Exception as exc:  # noqa: BLE001
        logger.exception("save_plan 失败 thread=%s", thread_id)
        return {"success": False, "error": f"保存计划失败: {exc}"}
    return {"success": True, "path": path}


def _safe_config() -> dict[str, Any]:
    """安全取 run config(无则空 dict)。"""
    try:
        config = get_config()
        return config if isinstance(config, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _thread_and_author(config: dict[str, Any]) -> tuple[str | None, str]:
    """从 config 取 (thread_id, 作者 username)。"""
    configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
    if not isinstance(configurable, dict):
        configurable = {}
    thread_id = configurable.get("thread_id")
    author = configurable.get("local_user") or _AUTHOR_FALLBACK
    return (
        str(thread_id) if isinstance(thread_id, str) and thread_id else None,
        str(author),
    )


async def _read_plan_file(thread_id: str, path: str) -> str:
    """经沙箱后端读计划文件(沙箱内路径)。"""
    backend = await get_sandbox_backend(thread_id)
    result = await backend.aread(path, offset=0, limit=_MAX_PLAN_LINES)
    error = _value(result, "error")
    if error:
        raise ValueError(error)
    file_data = _value(result, "file_data")
    if file_data is None:
        raise ValueError("计划文件读取不到")
    encoding = _value(file_data, "encoding")
    if encoding is not None and encoding != "utf-8":
        raise ValueError("计划文件必须是 UTF-8 文本")
    content = _value(file_data, "content")
    if not isinstance(content, str):
        raise ValueError("计划文件内容不是文本")
    if content.count("\n") + 1 >= _MAX_PLAN_LINES:
        raise ValueError("计划文件过大")
    return content


def _value(value: Any, key: str) -> Any:
    """从 dict 或对象取字段。"""
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _is_html_path(path: str) -> bool:
    """路径是否为 plans 目录下的单个 .html 文件。"""
    if "\x00" in path or not path.startswith(f"{PLAN_FILE_DIRECTORY}/"):
        return False
    filename = path.removeprefix(f"{PLAN_FILE_DIRECTORY}/")
    return bool(filename and "/" not in filename and filename.lower().endswith(".html"))


def _validate_html(content: str) -> str | None:
    """校验计划 HTML(完整性 + 无脚本/内嵌/表单)。"""
    lowered = content.lower()
    if "<html" not in lowered or "<head" not in lowered or "<body" not in lowered:
        return "计划必须是完整 HTML 文档(含 html/head/body)"
    if "<title" not in lowered:
        return "计划必须含描述性 title"
    if "<script" in lowered:
        return "计划 HTML 不得含 script"
    if "<iframe" in lowered or "<object" in lowered or "<embed" in lowered:
        return "计划 HTML 不得含嵌套 embed"
    if "<form" in lowered:
        return "计划 HTML 不得含 form"
    return None
