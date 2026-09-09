"""
本文件对外提供 Caspian 网页层的「本地单用户身份」:无注册/登录下的稳定 user_id 来源。

对外提供:
    LOCAL_USER_EMAIL — 自动创建本地用户时的默认邮箱(常量)
    ensure_local_user — 采纳已存在的唯一用户,否则自动创建;解析为稳定身份
    get_local_user — 返回当前本地身份;未初始化时回退到稳定常量
    set_local_user — 注入已解析的本地身份(供启动引导调用)

输入: 无(ensure_local_user 内部读 users 表)
输出: SimpleNamespace — 含 id / email / display_name / token_version 的轻量身份对象

具体工作流:
    (1) adopt: 若 users 表已有账号,取最早一条作为本地用户,复用其 id
    (2) create: 若 users 表为空,插入唯一一条本地用户并提交
    (3) fallback: 数据库不可用(测试/未迁移)时,回退到一个确定性常量身份
    (4) 无论何种路径,均把解析结果 set_local_user 缓存,供中间件与 /me 复用

为什么这样设计:
    持久化以 (user_id, thread_id) 为键;user_id 唯一来源是 request.state.current_user.id。
    去掉登录后,只要维持一个稳定且确定性的本地用户身份,全部数据原样保留。
    复用已存在账号的 uuid,使存量数据零迁移落在同一 user_id 下。
"""

import uuid
from types import SimpleNamespace

LOCAL_USER_EMAIL = "local@caspian.local"
# 数据库不可用(测试/未迁移)时的确定性回退身份,保证 user_id 不漂移。
_FALLBACK_USER_ID = "00000000-0000-0000-0000-000000000001"

_local_user: SimpleNamespace | None = None


def set_local_user(user) -> None:
    """注入已解析的本地身份(受保护 helper,供启动引导与测试使用)。"""
    global _local_user
    _local_user = user


def get_local_user() -> SimpleNamespace:
    """返回当前本地身份;未初始化时回退到稳定常量。

    输出:
        SimpleNamespace — {id, email, display_name, token_version}
    """
    if _local_user is not None:
        return _local_user
    return SimpleNamespace(
        id=_FALLBACK_USER_ID,
        email=LOCAL_USER_EMAIL,
        display_name="Local User",
        token_version=0,
    )


def _identity(user) -> SimpleNamespace:
    """把 User ORM 实例(或任意含字段对象)转为轻量身份(受保护 helper)。"""
    return SimpleNamespace(
        id=user.id,
        email=getattr(user, "email", None),
        display_name=getattr(user, "display_name", None),
        token_version=getattr(user, "token_version", 0),
    )


async def ensure_local_user() -> SimpleNamespace:
    """采纳已存在的唯一用户,否则自动创建;解析为稳定本地身份。

    输出:
        SimpleNamespace — 当前本地的稳定身份对象,已缓存
    """
    user = None
    try:
        from backend.app.gateway.models.user import User
        from caspian.persistence.engine import get_session
        from sqlalchemy import select

        async with get_session() as session:
            result = await session.execute(
                select(User).order_by(User.created_at).limit(1)
            )
            user = result.scalar_one_or_none()
            if user is None:
                user = User(
                    id=uuid.uuid4(),
                    email=LOCAL_USER_EMAIL,
                    password_hash=None,
                    token_version=0,
                    display_name="Local User",
                )
                session.add(user)
                await session.commit()
    except Exception:
        user = None

    identity = _identity(user) if user is not None else get_local_user()
    set_local_user(identity)
    return identity
