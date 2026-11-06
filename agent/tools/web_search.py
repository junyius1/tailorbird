"""工具:``web_search`` — 用 Exa 检索网络信息。

本地变体保留:需 ``EXA_API_KEY``;未配置时返回明确错误(不静默)。
"""

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# 默认结果数
_DEFAULT_RESULTS = 5
# 结果正文截断上限(字符)
_MAX_CONTENT_CHARS = 20_000


async def web_search(
    query: str,
    num_results: int = _DEFAULT_RESULTS,
    include_contents: bool = True,
) -> dict[str, Any]:
    """用 Exa 检索网络信息(文档/代码/仓库/新闻/论文)。

    参数:
        query: 检索查询。
        num_results: 返回结果数(默认 5)。
        include_contents: 是否包含全文(默认 True)。
    返回:
        ``{"success", "results": [...], "count"}``;
        缺 API key 或失败时含 ``error``。
    """
    api_key = os.environ.get("EXA_API_KEY")
    if not api_key:
        return {
            "success": False,
            "error": "EXA_API_KEY 未配置。请在环境变量中设置。",
            "results": [],
            "count": 0,
        }
    try:
        from exa_py import Exa  # 延迟导入(重依赖)

        client = Exa(api_key=api_key)
        # include_contents → Exa 的 content 参数
        content_type = "text" if include_contents else None
        response = client.search(
            query,
            num_results=num_results,
            content=content_type,
        )
        results: list[dict[str, Any]] = []
        for item in (response.results if hasattr(response, "results") else []):
            entry: dict[str, Any] = {
                "title": getattr(item, "title", None),
                "url": getattr(item, "url", None),
                "published_date": getattr(item, "published_date", None),
                "text": getattr(item, "text", None) or getattr(item, "content", None),
            }
            # 截断超长正文
            if entry["text"] and len(entry["text"]) > _MAX_CONTENT_CHARS:
                entry["text"] = entry["text"][:_MAX_CONTENT_CHARS] + " ...[截断]"
            results.append(entry)
        return {"success": True, "results": results, "count": len(results)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("web_search 失败: %s", exc)
        return {"success": False, "error": f"检索失败: {exc}", "results": [], "count": 0}
