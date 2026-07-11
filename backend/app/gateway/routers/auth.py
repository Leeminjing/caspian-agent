"""
本文件对外提供 `router`（APIRouter 实例），定义认证相关路由：登录、登出、当前用户。

对外提供:
    router: APIRouter — 已注册 auth 相关路由的 FastAPI Router，前缀 /api/auth
    LoginRequest: BaseModel — 登录请求体 Pydantic 模型

输入:
    login:
        body: LoginRequest — {email, password}
        request: Request — FastAPI Request 对象（用于获取 IP 和 app.state）
    logout:
        request: Request
    me:
        request: Request

输出:
    POST /api/auth/login  → {user: {id, email, display_name}} + Set-Cookie
    POST /api/auth/logout → {ok: true} + Clear-Cookie
    GET  /api/auth/me     → {user: {id, email, display_name}}

具体工作流:
    login:
    (1) 获取客户端 IP
    (2) check_rate_limit(ip, config) → 拒绝则 429
    (3) 查 DB 验证 email + password → 失败则 401，记录限流
    (4) 成功: create_access_token + generate_csrf_token
    (5) 设置 Set-Cookie（access_token HttpOnly, csrf_token 普通）
    (6) 返回 user 对象和 CSRF token

    logout:
    (1) 清除两个 Cookie（max-age=0）
    (2) 返回 {ok: true}

    me:
    (1) 从 request.state.current_user 读取用户
    (2) 返回 {user: {id, email, display_name}}（不含敏感字段）

示例:
    from backend.app.gateway.routers.auth import router
    app.include_router(router)
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select

from backend.app.gateway.auth.security import (
    check_login_attempt_limit,
    create_access_token,
    generate_csrf_token,
    verify_password,
)
from lead_agent.persistence.engine import get_session
from backend.app.gateway.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth")


class LoginRequest(BaseModel):
    """POST /api/auth/login 的请求体。"""
    email: str
    password: str


def _is_secure(request: Request) -> bool:
    """判断当前请求是否 HTTPS。

    输入:
        request: Request — FastAPI Request 对象

    输出:
        bool — True 表示 HTTPS（或经反向代理的 TLS）
    """
    return request.url.scheme == "https" or request.headers.get("X-Forwarded-Proto") == "https"


def _set_access_cookie(response: JSONResponse, token: str, config, secure: bool) -> None:
    """设置 access_token HttpOnly Cookie。

    输入:
        response: JSONResponse — 响应对象
        token: str — JWT 字符串
        config: AuthConfig — 认证配置
        secure: bool — 是否为 HTTPS 连接
    """
    response.set_cookie(
        key=config.cookie_name,
        value=token,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def _set_csrf_cookie(response: JSONResponse, token: str, config, secure: bool) -> None:
    """设置 csrf_token 可读 Cookie（非 HttpOnly，JS 需要读取）。

    输入:
        response: JSONResponse — 响应对象
        token: str — CSRF token 随机字符串
        config: AuthConfig — 认证配置
        secure: bool — 是否为 HTTPS 连接
    """
    response.set_cookie(
        key=config.csrf_cookie_name,
        value=token,
        samesite="lax",
        secure=secure,
        path="/",
    )


def _clear_cookies(response: JSONResponse, config) -> None:
    """清除 access_token 和 csrf_token Cookie。

    输入:
        response: JSONResponse — 响应对象
        config: AuthConfig — 认证配置
    """
    response.set_cookie(
        key=config.cookie_name,
        value="",
        max_age=0,
        path="/",
    )
    response.set_cookie(
        key=config.csrf_cookie_name,
        value="",
        max_age=0,
        path="/",
    )


def _get_client_ip(request: Request) -> str:
    """从请求中提取客户端 IP。

    输入:
        request: Request — FastAPI Request 对象

    输出:
        str — 客户端 IP 地址
    """
    # 优先取反向代理设置的真实 IP
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "127.0.0.1"


def _user_to_dict(user) -> dict:
    """将 User ORM 实例转为前端安全的 dict（不含密码哈希和 token_version）。

    输入:
        user: User — 用户 ORM 实例

    输出:
        dict — {id, email, display_name}
    """
    return {
        "id": str(user.id),
        "email": user.email,
        "display_name": user.display_name,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/login")
async def login(body: LoginRequest, request: Request):
    """POST /api/auth/login — 验证凭据，签发 JWT 和 CSRF token。

    输入:
        body: LoginRequest — {email, password}
        request: Request — 用于获取 IP、auth_config 和 HTTPS 检测
    """
    from backend.app.gateway.app import auth_config

    config = auth_config
    client_ip = _get_client_ip(request)
    secure = _is_secure(request)

    # 登录尝试限流
    if not check_login_attempt_limit(client_ip, config):
        return JSONResponse(
            status_code=429,
            content={"detail": "登录尝试过于频繁，请稍后再试"},
        )

    # 查 DB 验证凭据
    async with get_session() as session:
        result = await session.execute(
            select(User).where(User.email == body.email)
        )
        user = result.scalar_one_or_none()

        if user is None or user.password_hash is None:
            return JSONResponse(
                status_code=401,
                content={"detail": "邮箱或密码错误"},
            )

        if not verify_password(body.password, user.password_hash):
            return JSONResponse(
                status_code=401,
                content={"detail": "邮箱或密码错误"},
            )

        # 签发 JWT 和 CSRF token
        access_token = create_access_token(user, config)
        csrf_token = generate_csrf_token()

        response_data = {
            "user": _user_to_dict(user),
            "csrf_token": csrf_token,
        }
        response = JSONResponse(content=response_data)

        _set_access_cookie(response, access_token, config, secure)
        _set_csrf_cookie(response, csrf_token, config, secure)

        logger.info("用户登录成功: user_id=%s, email=%s", user.id, user.email)
        return response


@router.post("/logout")
async def logout(request: Request):
    """POST /api/auth/logout — 清除 Cookie 实现登出。

    输入:
        request: Request — 用于获取 auth_config
    """
    from backend.app.gateway.app import auth_config

    config = auth_config
    response = JSONResponse(content={"ok": True})
    _clear_cookies(response, config)
    return response


@router.get("/me")
async def me(request: Request):
    """GET /api/auth/me — 返回当前登录用户信息。

    输入:
        request: Request — 从中读取 request.state.current_user
    """
    user = getattr(request.state, "current_user", None)
    if user is None:
        return JSONResponse(
            status_code=401,
            content={"detail": "未登录"},
        )
    return {"user": _user_to_dict(user)}
