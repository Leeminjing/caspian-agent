"""
本文件对外提供 Store 和 StoreVectors 两个 ORM 模型，表结构照抄 LangGraph 官方 AsyncPostgresStore 的 MIGRATIONS 和 VECTOR_MIGRATIONS。

对外提供:
    Store        — store 主表 (prefix + key → JSONB value)
    StoreVectors — store 向量索引表 (prefix + key + field_name → embedding)

输入: 无 — 本文件为纯定义文件
输出: 两个 ORM 模型类，继承自 Base

示例:
    from caspian.persistence.store_models import Store, StoreVectors

    async with get_session() as session:
        result = await session.execute(select(Store).where(Store.prefix == "users/123"))
"""

from sqlalchemy import Column, DateTime, ForeignKeyConstraint, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from caspian.persistence.base import Base


class Store(Base):
    __tablename__ = "store"

    prefix: Mapped[str] = mapped_column(Text, primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=None)
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=None)
    expires_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    ttl_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)


class StoreVectors(Base):
    __tablename__ = "store_vectors"

    prefix: Mapped[str] = mapped_column(Text, primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    field_name: Mapped[str] = mapped_column(Text, primary_key=True)
    # embedding 列的 DDL 类型由 Alembic 迁移中的纯 SQL 管理（pgvector.vector(1024)），
    # ORM 层面仅声明列存在即可，不对其 SQL 类型做映射
    embedding = Column("embedding")
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=None)
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=None)

    __table_args__ = (
        ForeignKeyConstraint(
            ["prefix", "key"],
            ["store.prefix", "store.key"],
            ondelete="CASCADE",
        ),
    )
