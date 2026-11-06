"""模型选项端点(设计书 §5.6):前端下拉的模型/effort 列表。"""

import os
from typing import Any

from fastapi import APIRouter, Depends

from .auth import require_session

router = APIRouter(prefix="/dashboard/api", tags=["options"])

# 支持的模型(本地 vLLM + 可选云端);按已配置 provider 过滤
# local: 模型恒在(本地部署主路径)
_LOCAL_MODEL_ID = os.environ.get("LOCAL_LLM_MODEL_ID", "Qwen")


def _local_model_entry() -> dict[str, Any]:
    """本地模型条目。"""
    return {
        "id": f"local:{_LOCAL_MODEL_ID}",
        "label": f"本地 {_LOCAL_MODEL_ID}",
        "effort_options": ["low", "medium", "high"],
    }


def _cloud_model_entries() -> list[dict[str, Any]]:
    """云端模型条目(按已配置 key 过滤;未配则不出现)。"""
    entries: list[dict[str, Any]] = []
    if os.environ.get("ANTHROPIC_API_KEY"):
        entries.append(
            {
                "id": "anthropic:claude-sonnet-4-5",
                "label": "Claude Sonnet 4.5",
                "effort_options": ["low", "medium", "high"],
            }
        )
    if os.environ.get("OPENAI_API_KEY"):
        entries.append(
            {
                "id": "openai:gpt-5",
                "label": "GPT-5",
                "effort_options": ["low", "medium", "high"],
            }
        )
    return entries


@router.get("/options")
async def get_options(user: dict[str, Any] = Depends(require_session)) -> dict[str, Any]:
    """模型/effort 下拉列表。无可用模型 → 200 空数组(前端显示"未配置模型")。

    返回:
        ``{"models": [...], "default_agent_model", "default_agent_reasoning_effort"}``。
    """
    models = [_local_model_entry(), *_cloud_model_entries()]
    default = models[0]["id"] if models else None
    return {
        "models": models,
        "default_agent_model": default,
        "default_agent_reasoning_effort": "medium",
    }
