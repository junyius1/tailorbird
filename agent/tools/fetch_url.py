"""工具:``fetch_url`` — 抓取 URL 并把 HTML 转成 markdown。

本地变体保留(经 URL 安全校验,阻断回环/内网)。
"""

from typing import Any

import httpx
from markdownify import markdownify

from ..utils.url_safety import request_with_safe_redirects

# 抓取正文截断上限(字符)
FETCH_URL_MAX_CHARS = 100_000
# 抓取超时(秒)
_DEFAULT_TIMEOUT = 30


async def fetch_url(url: str, timeout: int = _DEFAULT_TIMEOUT) -> dict[str, Any]:
    """抓取 URL 内容并转 markdown(HTML → 纯文本,便于阅读)。

    参数:
        url: 要抓取的 URL(必须为合法 HTTP/HTTPS)。
        timeout: 超时秒数(默认 30)。
    返回:
        ``{"success", "url", "markdown_content", "status_code", "content_length"}``;
        被阻断/失败时含 ``error``。
    说明:
        拿到 markdown 后,应从中提取与问题相关的信息,
        用自然、有帮助的语言回答;除非用户明确要求,不要直接贴原始 markdown。
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response, blocked = await request_with_safe_redirects(
                client,
                "GET",
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; Tailorbird/1.0)"},
            )
            if blocked:
                return {"success": False, "error": blocked["content"], "url": url}
            if response is None:
                return {"success": False, "error": "抓取失败:无响应", "url": url}
            response.raise_for_status()
            # HTML → markdown
            markdown_content = markdownify(response.text)
        # 截断超大内容
        if len(markdown_content) > FETCH_URL_MAX_CHARS:
            markdown_content = (
                markdown_content[:FETCH_URL_MAX_CHARS]
                + f"\n... [内容已截断:{len(markdown_content)} 字符]"
            )
        return {
            "success": True,
            "url": str(response.url),
            "markdown_content": markdown_content,
            "status_code": response.status_code,
            "content_length": len(markdown_content),
        }
    except httpx.HTTPStatusError as exc:
        return {"success": False, "error": f"HTTP 状态错误: {exc.response.status_code}", "url": url}
    except httpx.HTTPError as exc:
        return {"success": False, "error": f"HTTP 错误: {exc}", "url": url}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"抓取失败: {exc}", "url": url}
