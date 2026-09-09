"""
本文件对外提供 `router`（APIRouter 实例），定义认证相关路由：当前用户。

对外提供:
    router: APIRouter — 已注册 auth 相关路由的 FastAPI Router，前缀 /api/auth
    _user_to_dict — 把用户身份对象转成前端安全的 dict

输入:
    me:
        request: Request

输出:
    GET /api/auth/me → {user: {id, email, display_name}}

具体工作流:
    me:
    (1) 从 auth.local.get_local_user() 取本地单用户身份
    (2) 返回 {user: {id, email, display_name}}（不含敏感字段）

为什么这样设计:
    本地单用户、无注册/登录。保留 /me 作为前端身份 bootstrap（提供 user.id / display_name /
    email 供 UI 使用），user_id 从服务端统一取得，前端不硬编码。
"""

import logging

from fastapi import APIRouter, Request

from backend.app.gateway.auth.local import get_local_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth")


def _user_to_dict(user) -> dict:
    """将身份对象转为前端安全的 dict（不含密码哈希和 token_version）。

    输入:
        user: 身份对象（含 id / email / display_name）

    输出:
        dict — {id, email, display_name}
    """
    return {
        "id": str(user.id),
        "email": getattr(user, "email", None),
        "display_name": getattr(user, "display_name", None),
    }


@router.get("/me")
async def me(request: Request):
    """GET /api/auth/me — 返回当前本地用户身份。恒成功。"""
    return {"user": _user_to_dict(get_local_user())}
