"""
本文件对外提供 `User` SQLAlchemy ORM 模型，映射到 `users` 表，用于存储用户认证数据。属于网关层业务模型，不归 agent 的 persistence 模型。

对外提供:
    User(Base) — 用户 ORM 模型类

输入: 无 — 本文件为纯定义文件
输出: User 类

字段:
    id: UUID             — 用户唯一标识，默认 uuid4
    email: str(320)      — 登录凭据，唯一，非空
    password_hash: str|null — bcrypt 密码哈希（$dfv2$ 前缀），OAuth 用户为 null
    token_version: int   — JWT token 版本号，改密码 +1 使旧 token 全部失效
    display_name: str|null — 可选显示名
    oauth_provider: str|null — OAuth 提供商（预留），如 "github"
    oauth_id: str|null   — OAuth 端用户唯一 ID（预留）
    created_at: datetime(tz) — 创建时间，默认 now()
    updated_at: datetime(tz) — 更新时间，默认 now()，自动更新

索引:
    email UNIQUE

示例:
    from backend.app.gateway.models.user import User

    user = User(
        id=uuid.uuid4(),
        email="alice@example.com",
        password_hash="$dfv2$...",
    )
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from lead_agent.persistence.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(
        String(320), unique=True, nullable=False
    )
    password_hash: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    token_version: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    display_name: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    oauth_provider: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    oauth_id: Mapped[str | None] = mapped_column(
        String(256), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
