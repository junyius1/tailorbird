"""协议面(LangGraph REST/SSE)的认证钩子:多用户复合 bearer。

设计书 §3.1.2:
- 形态 A(内部服务间,默认):``Bearer <OPEN_SWE_LOCAL_AUTH_TOKEN>`` +
  ``X-OpenSWE-User: <username>`` 头(dashboard 进程转发请求时注入)。
  无该头 → 身份 ``local-service``(仅允许机器级操作)。
- 形态 B(浏览器/SDK 直连协议面):``Bearer <token>:<username>``(分隔符 ``:``)
  → 校验 token 后身份 = username。

校验必须 ``hmac.compare_digest``(防时序),整个协议面 + dashboard 面
共享同一 token,不引入第二份 secret。

langgraph_api 的 authenticate 钩子支持的注入参数:
``authorization: str | None``、``headers: dict[bytes, bytes] | None`` 等。
"""

import hmac
import os
import re

from langgraph_sdk import Auth

# 协议面认证实例(langgraph.json 的 auth.path 指向本模块的 ``auth``)
auth = Auth()

# username 允许字符:字母/数字/点/下划线/连字符,1-64 位
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# 形态 A 下没有用户头时的机器身份
_SERVICE_IDENTITY = "local-service"
# 形态 A 的用户身份头名(bytes 形态,与请求头大小写无关)
_USER_HEADER = b"x-openswe-user"


def _configured_token() -> str:
    """读取共享服务端 token(未设返回空串)。"""
    return (os.environ.get("OPEN_SWE_LOCAL_AUTH_TOKEN") or "").strip()


def _valid_username(value: object) -> bool:
    """校验 username 形态(限定字符集,防注入)。"""
    return isinstance(value, str) and bool(_USERNAME_RE.match(value))


def extract_identity(
    authorization: str | None, user_header: str | None = None
) -> str | None:
    """从认证头解析身份;token 不匹配或头非法返回 None。

    参数:
        authorization: ``Authorization`` 头的原始值。
        user_header: ``X-OpenSWE-User`` 头的值(形态 A 的用户身份)。

    返回:
        解析出的 username(或 ``local-service``);认证失败返回 None。
    """
    expected = _configured_token()
    if not authorization or not expected:
        return None
    scheme, _, supplied = authorization.partition(" ")
    if scheme.strip().lower() != "bearer":
        return None
    supplied = supplied.strip()
    # 形态 B:token 后带 ":username"
    if ":" in supplied:
        token_part, _, username = supplied.partition(":")
        if not hmac.compare_digest(token_part, expected):
            return None
        if not _valid_username(username):
            return None
        return username
    # 形态 A:纯 token,身份来自 X-OpenSWE-User 头
    if not hmac.compare_digest(supplied, expected):
        return None
    if user_header and _valid_username(user_header.strip()):
        return user_header.strip()
    return _SERVICE_IDENTITY


@auth.authenticate
async def authenticate(
    authorization: str | None = None,
    headers: dict[bytes, bytes] | None = None,
) -> Auth.types.MinimalUserDict:
    """langgraph_sdk.Auth 的 authenticate 钩子。

    成功 → 注入 ``{"identity": <username>}``;
    失败 → 401 ``{"detail": "Invalid bearer token"}``(不区分缺/错,防探测)。
    """
    # 从请求头里取 X-OpenSWE-User(大小写不敏感)
    user_header: str | None = None
    if headers:
        for key, value in headers.items():
            if key.lower() == _USER_HEADER:
                user_header = value.decode("utf-8", errors="ignore")
                break
    identity = extract_identity(authorization, user_header)
    if identity is None:
        raise Auth.exceptions.HTTPException(
            status_code=401, detail="Invalid bearer token"
        )
    return {"identity": identity}
