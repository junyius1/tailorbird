"""工具:``http_request`` — 向 API / 网络服务发起 HTTP 请求。

本地变体保留:agent 需要出站调 API,但经 URL 安全校验
(阻断回环/内网,防 SSRF 打本地 vLLM/Postgres)。
"""

from typing import Any

import httpx

from ..utils.url_safety import request_with_safe_redirects

# 默认超时(秒)
_DEFAULT_TIMEOUT = 30
# 响应正文截断上限(字符)
_MAX_CONTENT_CHARS = 100_000


async def http_request(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: str | dict | None = None,
    params: dict[str, str] | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """向 API 或网络服务发起 HTTP 请求(带 SSRF 防护)。

    参数:
        url: 目标 URL。
        method: HTTP 方法(GET/POST/PUT/DELETE 等)。
        headers: 请求头。
        data: 请求体(字符串或 dict)。
        params: URL 查询参数。
        timeout: 超时秒数(默认 30)。
    返回:
        ``{"success", "status_code", "headers", "content", "url"}``;
        被阻断时 ``content`` 含原因。
    """
    # 规范化:dict 请求体转 JSON + content-type
    json_body: dict | None = None
    raw_body: str | None = None
    final_headers = dict(headers or {})
    if isinstance(data, dict):
        json_body = data
        final_headers.setdefault("Content-Type", "application/json")
    elif isinstance(data, str):
        raw_body = data
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response, blocked = await request_with_safe_redirects(
                client,
                method.upper(),
                url,
                headers=final_headers,
                params=params,
                json=json_body,
                content=raw_body,
            )
        if blocked:
            return blocked
        if response is None:
            return {"success": False, "status_code": 0, "headers": {}, "content": "无响应", "url": url}
        # 截断超大正文
        content = response.text
        truncated = False
        if len(content) > _MAX_CONTENT_CHARS:
            content = content[:_MAX_CONTENT_CHARS] + f"\n... [内容已截断:{len(response.text)} 字符]"
            truncated = True
        return {
            "success": response.status_code < 400,
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "content": content,
            "url": str(response.url),
            "truncated": truncated,
        }
    except httpx.HTTPError as exc:
        return {"success": False, "status_code": 0, "headers": {}, "content": f"HTTP 错误: {exc}", "url": url}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "status_code": 0, "headers": {}, "content": f"请求失败: {exc}", "url": url}
