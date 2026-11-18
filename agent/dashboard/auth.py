"""本地静态 token 多用户认证(设计书 §3):登录/注册/登出/会话依赖。

认证凭据是**一个共享的服务端 token**(``OPEN_SWE_LOCAL_AUTH_TOKEN``,
证明"能访问本机"),**身份由登录时的 username 决定**,会话按用户隔离:
- ``POST /auth/login {username, token}``:token 与共享 token 经
  ``hmac.compare_digest`` 比对(防时序,不区分"缺/错"防探测);
  注册表开启且用户不存在 → 401 ``unknown user``;
  成功 → 200 + ``Set-Cookie: tb_session=<按用户签名的会话值>``。
  (cookie 名独立于 open-swe 的 ``osw_session``:同域名不同端口的两个
  dashboard 各持各的会话,互不影响。)
- 会话 cookie 值**按用户签名**(含 sub)——两个浏览器各拿各的 cookie,
  服务端互不串号;**不用进程内存存会话**(双进程部署不共享内存,
  签名 cookie 天然跨进程,只要两进程共享 ``DASHBOARD_JWT_SECRET``)。
- ``require_session``:解出 ``{sub, name, email:None, is_admin}``,无效/过期 → 401。
"""

import hmac
import logging
import os
import re
import time
from typing import Any

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .user_registry import count_users, list_user_records, registry_enabled, upsert_user_record, user_exists

logger = logging.getLogger(__name__)

# 会话 cookie 名(独立于 open-swe 的 osw_session,避免同域跨端会话互踢)
_COOKIE_NAME = "tb_session"
# 会话有效期(秒):7 天
_SESSION_TTL_SECONDS = 7 * 24 * 3600
# username 允许字符(1-64 位)
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# 认证路由(挂 /dashboard/api 下)
router = APIRouter(tags=["auth"])


def _auth_token() -> str:
    """共享服务端 token(未设返回空串 → fail-closed)。"""
    return (os.environ.get("OPEN_SWE_LOCAL_AUTH_TOKEN") or "").strip()


def _jwt_secret() -> str:
    """会话签名密钥(缺省回退到共享 token;生产必须显式设 DASHBOARD_JWT_SECRET)。"""
    return (
        os.environ.get("DASHBOARD_JWT_SECRET")
        or os.environ.get("OPEN_SWE_LOCAL_AUTH_TOKEN")
        or ""
    ).strip()


def configured_admins() -> set[str]:
    """admin 用户集合(CONFIGURED_ADMINS,逗号分隔 username)。"""
    return {
        name.strip()
        for name in os.environ.get("CONFIGURED_ADMINS", "").split(",")
        if name.strip()
    }


def _check_token(supplied: str) -> bool:
    """共享 token 校验(hmac.compare_digest 防时序)。"""
    expected = _auth_token()
    if not expected:
        return False
    return hmac.compare_digest(supplied or "", expected)


def _is_https(request: Request) -> bool:
    """是否 https(决定 cookie SameSite/Secure)。"""
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "") == "https"


def issue_session_cookie(response: Response, username: str, request: Request) -> None:
    """按用户签发会话 cookie 并写 Set-Cookie。

    值 = HS256 JWT ``{sub, name, is_admin, iat, exp}``,按用户签名。
    """
    secret = _jwt_secret()
    if not secret:
        raise HTTPException(status_code=503, detail="auth not configured; set OPEN_SWE_LOCAL_AUTH_TOKEN")
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": username,
        "name": username,
        "is_admin": username in configured_admins(),
        "iat": now,
        "exp": now + _SESSION_TTL_SECONDS,
    }
    token = jwt.encode(payload, secret, algorithm="HS256")
    kwargs: dict[str, Any] = {
        "max_age": _SESSION_TTL_SECONDS,
        "path": "/",
        "httponly": True,
        "samesite": "none" if _is_https(request) else "lax",
    }
    if _is_https(request):
        kwargs["secure"] = True
    response.set_cookie(_COOKIE_NAME, token, **kwargs)


def decode_session_cookie(request: Request) -> dict[str, Any] | None:
    """解会话 cookie;无效/过期返回 None。"""
    raw = request.cookies.get(_COOKIE_NAME)
    if not raw:
        return None
    secret = _jwt_secret()
    if not secret:
        return None
    try:
        payload = jwt.decode(raw, secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    sub = payload.get("sub")
    if not isinstance(sub, str) or not _USERNAME_RE.match(sub):
        return None
    return {
        "sub": sub,
        "name": payload.get("name") or sub,
        "is_admin": bool(payload.get("is_admin")) or sub in configured_admins(),
    }


def require_session(request: Request) -> dict[str, Any]:
    """会话依赖:解 cookie → 用户字典;无效/过期 → 401。

    返回:
        ``{"sub", "name", "email": None, "is_admin"}``(前端 ``me()`` 把 ``sub``
        映射到 ``login`` 字段,全站 ``user.login`` 引用面不变)。
    """
    session = decode_session_cookie(request)
    if session is None:
        raise HTTPException(status_code=401, detail="unauthenticated")
    session["email"] = None
    return session


def require_admin(user: dict[str, Any] = Depends(require_session)) -> dict[str, Any]:
    """admin 依赖:非 admin → 403。"""
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="forbidden")
    return user


def _allowed_origins() -> list[str]:
    """Origin 白名单(DASHBOARD_BASE_URL + DASHBOARD_ALLOWED_ORIGINS)。"""
    origins: list[str] = []
    for raw in (
        os.environ.get("DASHBOARD_BASE_URL", ""),
        os.environ.get("DASHBOARD_ALLOWED_ORIGINS", ""),
    ):
        for origin in raw.split(","):
            origin = origin.strip().rstrip("/")
            if origin:
                origins.append(origin)
    return origins


async def require_same_origin_for_mutations(request: Request) -> None:
    """非 GET/HEAD/OPTIONS 请求的 Origin 白名单校验(防 CSRF)。

    本地开发未配任何 origin 时 no-op;同 origin 请求(无 Origin 头)放行。
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    origins = _allowed_origins()
    if not origins:
        return
    origin = request.headers.get("origin", "")
    if not origin:
        return  # 同 origin 导航无 Origin 头
    if origin.rstrip("/") in origins:
        return
    raise HTTPException(status_code=403, detail="origin not allowed")


class LoginBody(BaseModel):
    """登录请求体。"""

    # 登录名(1-64 位字母/数字/._-)
    username: str = Field(min_length=1, max_length=64)
    # 共享服务端 token
    token: str = Field(min_length=1)


@router.post("/auth/login")
async def auth_login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
    """静态 token 换 per-user 会话 cookie(唯一登录入口)。

    成功:200 ``{sub, name, is_admin}`` + Set-Cookie;
    token 错:401 ``invalid token``;注册表开启且用户不存在:401 ``unknown user``;
    auth 未配置:503 fail-closed。
    """
    if not _auth_token():
        raise HTTPException(status_code=503, detail="auth not configured; set OPEN_SWE_LOCAL_AUTH_TOKEN")
    if not _check_token(body.token):
        raise HTTPException(status_code=401, detail="invalid token")
    username = body.username.strip()
    if not _USERNAME_RE.match(username):
        raise HTTPException(status_code=400, detail="username: invalid characters")
    # 注册表开启 → 要求用户已存在;关闭(裸开发)→ 首登自动建
    if not await user_exists(username):
        if registry_enabled():
            raise HTTPException(status_code=401, detail="unknown user")
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        await upsert_user_record(
            {
                "username": username,
                "created_at": now_iso,
                "admin": username in configured_admins(),
            }
        )
    is_admin = username in configured_admins()
    issue_session_cookie(response, username, request)
    return {"sub": username, "name": username, "is_admin": is_admin}


class RegisterBody(BaseModel):
    """注册请求体(admin 或首用户可建账号)。"""

    # 新用户名
    username: str = Field(min_length=1, max_length=64)
    # 共享服务端 token(证明"能访问本机")
    token: str = Field(min_length=1)


@router.post("/auth/register", status_code=201)
async def auth_register(body: RegisterBody, request: Request) -> dict[str, Any]:
    """创建本地账号(仅 admin 或首用户可建;§3.1.1)。

    成功:201 用户对象;重名:409;token 错:401;非 admin 且非首用户:403。
    """
    if not _auth_token() or not _check_token(body.token):
        raise HTTPException(status_code=401, detail="invalid token")
    username = body.username.strip()
    if not _USERNAME_RE.match(username):
        raise HTTPException(status_code=400, detail="username: invalid characters")
    # 权限:当前会话是 admin,或尚无任何用户(首用户)
    current = decode_session_cookie(request)
    is_first = await count_users() == 0
    if not (current and current.get("is_admin")) and not is_first:
        raise HTTPException(status_code=403, detail="forbidden")
    if await user_exists(username):
        raise HTTPException(status_code=409, detail="user already exists")
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    record = {
        "username": username,
        "created_at": now_iso,
        "admin": username in configured_admins(),
    }
    await upsert_user_record(record)
    return record


@router.post("/auth/logout")
async def auth_logout(response: Response) -> dict[str, Any]:
    """作废当前会话 cookie(幂等:无 cookie 也 200)。"""
    response.set_cookie(_COOKIE_NAME, "", max_age=0, path="/")
    return {}


@router.get("/me")
async def me(user: dict[str, Any] = Depends(require_session)) -> dict[str, Any]:
    """读当前身份(会话无效/过期 → 401,前端据此跳登录)。"""
    return {
        "sub": user["sub"],
        "name": user["name"],
        "email": None,
        "is_admin": user["is_admin"],
    }


@router.get("/users")
async def list_users(user: dict[str, Any] = Depends(require_admin)) -> list[dict[str, Any]]:
    """列全部用户(admin only;非 admin → 403)。"""
    records = await list_user_records()
    return [
        {
            "username": str(r.get("username") or ""),
            "created_at": str(r.get("created_at") or ""),
            "admin": bool(r.get("admin")),
        }
        for r in records
    ]
