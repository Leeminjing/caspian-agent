"""
本文件对外提供 `AuthMiddleware` FastAPI 中间件,作为本地单用户身份注入入口。

对外提供:
    AuthMiddleware(BaseHTTPMiddleware) — 把本地单用户注入 request.state.current_user
    _AUTH_WHITELIST_PATHS / _AUTH_WHITELIST_PREFIXES — 公开/静态路径集合(信息性,供测试

输入:
    __init__: auth_config — 兼容既有构造签名;本地单用户模型下不再需要

输出:
    dispatch 中将本地用户注入 request.state.current_user 后放行;不再返回 401

具体工作流:
    (1) 本地单用户:从 auth.local.get_local_user() 取稳定身份
    (2) 注入 request.state.current_user
    (3) 放行

为什么这样设计:
    所有子系统都从 request.state.current_user.id 取 user_id。去掉登录后,让中间件恒定注入
    同一个本地用户,user_id 即稳定,持久化零改动。公开/静态路径集合仍保留作为「无需身份
    的公开资源」的契约,供测试断言。
"""

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from backend.app.gateway.auth.local import get_local_user

logger = logging.getLogger(__name__)

# 公开/静态路径集合:本地单用户模型下无登录,这里仅作为「公开资源」的契约保留。
# /api/ 下的业务路径一律需要身份(由本中间件恒定注入本地用户)。
_AUTH_WHITELIST_PATHS = {
    "/",
    "/healthz",
    "/readyz",
}
_AUTH_WHITELIST_PREFIXES = [
    "/assets/",
    "/docs",
    "/openapi.json",
    "/redoc",
]


class AuthMiddleware(BaseHTTPMiddleware):
    """本地单用户身份注入中间件。

    输入:
        app — ASGI application
        auth_config — 兼容既有构造签名;本地单用户模型下未使用

    输出:
        dispatch(request, call_next) 将本地用户注入 request.state.current_user
    """

    def __init__(self, app, auth_config=None):
        super().__init__(app)
        self._auth_config = auth_config

    async def dispatch(self, request: Request, call_next):
        request.state.current_user = get_local_user()
        return await call_next(request)
