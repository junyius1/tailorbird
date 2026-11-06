"""出站 URL 安全校验 + 重定向钉扎(DNS pinning)。

- ``resolve_and_validate``:解析 URL 主机名,要求**所有**解析出的地址都是
  公网 IP(阻断回环/内网/链路本地,防 SSRF 打本地 vLLM/Postgres);
- ``request_with_safe_redirects``:每跳重定向都重新校验 + DNS 钉扎,
  跨源跳转剥离敏感头(auth/cookie),303/302 降级 GET。
本地变体保留该防护:agent 的网络工具可出站,但不能打穿到本地服务。
"""

import ipaddress
import socket
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import httpx

# 最大重定向跳数
_MAX_REDIRECTS = 5
# 触发重定向的状态码
_REDIRECT_CODES = {301, 302, 303, 307, 308}
# 实体头(重定向改方法时需剥离,避免语义漂移)
_ENTITY_HEADERS = {"content-encoding", "content-language", "content-length", "content-type"}
# 敏感头(跨源跳转需剥离,防凭据泄漏到外部)
_SENSITIVE_HEADERS = {"authorization", "cookie", "proxy-authorization"}


def resolve_and_validate(
    url: str,
) -> tuple[bool, str, str | None, list | None]:
    """解析 URL 主机名,要求所有解析地址为公网。

    返回:
        ``(是否安全, 原因, 主机名, addr_info 列表)``。
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return False, f"不支持的 URL scheme: {parsed.scheme or '<缺失>'}", None, None
        hostname = parsed.hostname
        if not hostname:
            return False, "无法解析主机名", None, None
        try:
            addr_infos = socket.getaddrinfo(hostname, None)
        except socket.gaierror:
            return False, f"无法解析主机名: {hostname}", hostname, None
        if not addr_infos:
            return False, f"无法解析主机名: {hostname}", hostname, None
        for addr_info in addr_infos:
            ip_str = addr_info[4][0]
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                return False, f"无法解析地址: {ip_str}", hostname, None
            # 映射 IPv6 套接字 IPv4 地址
            if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
                ip = ip.ipv4_mapped
            # 只允许公网(排除回环/内网/链路本地/保留)
            if not ip.is_global:
                return False, f"URL 解析到被阻断的地址: {ip_str}", hostname, None
        return True, "", hostname, addr_infos
    except Exception as e:  # noqa: BLE001
        return False, f"URL 校验错误: {e}", None, None


def is_url_safe(url: str) -> tuple[bool, str]:
    """URL 是否安全可请求。"""
    is_safe, reason, _, _ = resolve_and_validate(url)
    return is_safe, reason


def _blocked_response(url: str, reason: str) -> dict[str, Any]:
    """被阻断请求的响应体。"""
    return {
        "success": False,
        "status_code": 0,
        "headers": {},
        "content": f"请求被阻断: {reason}",
        "url": url,
    }


def pinned_url(url: str, ip: str) -> str:
    """把 URL 的主机替换为钉扎 IP(IPv6 加方括号;保留端口)。"""
    parsed = urlparse(url)
    host_literal = f"[{ip}]" if ":" in ip else ip
    netloc = f"{host_literal}:{parsed.port}" if parsed.port else host_literal
    return urlunparse(parsed._replace(netloc=netloc))


def _origin(url: str) -> tuple[str, str, int]:
    """URL 的 (scheme, host, port) 三元组(小写归一)。"""
    parsed = urlparse(url)
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError:
        port = -1
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), port


def _is_sensitive_header(name: str) -> bool:
    """是否为敏感头(跨源跳转需剥离)。"""
    normalized = name.lower()
    return (
        normalized in _SENSITIVE_HEADERS
        or "api-key" in normalized
        or normalized.endswith("-token")
    )


def _redirect_method(method: str, status_code: int) -> str:
    """按状态码决定重定向后的方法(303→GET 等)。"""
    if status_code == 303 and method != "HEAD":
        return "GET"
    if status_code == 302 and method != "HEAD":
        return "GET"
    if status_code == 301 and method == "POST":
        return "GET"
    return method


async def request_with_safe_redirects(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers_for_url: Callable[[str, str], Mapping[str, str] | None] | None = None,
    **kwargs: Any,
) -> tuple[httpx.Response | None, dict[str, Any] | None]:
    """发起带 DNS 钉扎与逐跳重定向校验的请求。

    返回:
        ``(响应或 None, 阻断原因 dict 或 None)``。
    """
    current_method = method.upper()
    current_url = url
    request_kwargs = dict(kwargs)
    caller_headers = dict(request_kwargs.pop("headers", None) or {})
    caller_extensions = dict(request_kwargs.pop("extensions", None) or {})
    response: httpx.Response | None = None

    for redirect_count in range(_MAX_REDIRECTS + 1):
        is_safe, reason, hostname, addr_infos = resolve_and_validate(current_url)
        if not is_safe or hostname is None or addr_infos is None:
            return None, _blocked_response(current_url, reason)

        parsed = urlparse(current_url)
        per_hop_headers = dict(headers_for_url(url, current_url) or {}) if headers_for_url else {}
        headers = {**caller_headers, **per_hop_headers, "Host": parsed.netloc}
        extensions = {**caller_extensions, "sni_hostname": hostname}

        # 钉扎 IP(去重保序)
        pinned_ips = list(dict.fromkeys(addr_info[4][0] for addr_info in addr_infos))
        for address_index, pinned_ip in enumerate(pinned_ips):
            try:
                response = await client.request(
                    current_method,
                    pinned_url(current_url, pinned_ip),
                    follow_redirects=False,
                    headers=headers,
                    extensions=extensions,
                    **request_kwargs,
                )
                break
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if address_index == len(pinned_ips) - 1:
                    raise

        if response is None:
            raise httpx.ConnectError("钉扎地址无响应")
        if response.status_code not in _REDIRECT_CODES:
            return response, None

        location = response.headers.get("Location")
        if not location:
            return response, None

        if redirect_count == _MAX_REDIRECTS:
            return None, _blocked_response(current_url, "重定向过多")

        next_url = urljoin(current_url, location)
        request_kwargs.pop("params", None)
        # 跨源跳转:剥离敏感头 + 认证 + cookie
        if _origin(current_url) != _origin(next_url):
            caller_headers = {
                name: value
                for name, value in caller_headers.items()
                if not _is_sensitive_header(name)
            }
            request_kwargs.pop("auth", None)
            request_kwargs.pop("cookies", None)

        next_method = _redirect_method(current_method, response.status_code)
        if next_method != current_method:
            # 方法改变:剥离实体头 + 请求体
            caller_headers = {
                name: value
                for name, value in caller_headers.items()
                if name.lower() not in _ENTITY_HEADERS
            }
            request_kwargs.pop("data", None)
            request_kwargs.pop("content", None)
            request_kwargs.pop("json", None)

        current_method = next_method
        current_url = next_url

    return None, _blocked_response(current_url, "重定向过多")
