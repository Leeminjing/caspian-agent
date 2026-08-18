"""
本文件对外提供 Recursive Context Forking 的 PostgreSQL ORM 模型与 API 数据模型。

对外提供:
    WebThread(Base) — 线程注册表（thread_id 即 context_id，含主运行锁定与 usage 聚合字段）
    WebContextDefinition(Base) — 派生 Context 的用户定义与执行投影记录
    WebContextSource(Base) — 派生 Context 的有序父来源记录
    ContextSourceRef / ContextDeriveCreate / ContextDefinitionUpdate / ContextProjectionDecision — 请求模型

输入: Context 派生、快照、决断请求的结构化数据
输出: SQLAlchemy 表定义与 Pydantic 请求模型

具体工作流:
    (1) WebThread 按认证用户隔离，lazy upsert 于主运行启动与派生创建
    (2) WebContextDefinition 一对一保存 authored/execution messages、修补清单、问题、
        双哈希、投影状态与初始 checkpoint 信息
    (3) WebContextSource 保存有序父来源（parent_context_id + source_checkpoint_id + position）

示例:
    from backend.app.gateway.context.models import WebThread, ContextDeriveCreate
    body = ContextDeriveCreate(title="新 Context", sources=[{"context_id": "t-1", "checkpoint_id": "ck-1"}], messages=[...])
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from caspian.persistence.base import Base


class WebThread(Base):
    """线程注册表：thread_id 即 context_id。"""

    __tablename__ = "web_threads"

    thread_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # 首个主运行被接受时间；非空后派生 Context 定义永久锁定
    main_run_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # ponytail: 不建 runs 表，per-run usage 明细不需要，聚合足够展示命中率
    prompt_input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    prompt_cache_hit_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class WebContextDefinition(Base):
    """派生 Context 的用户定义与执行投影记录（与 web_threads 一对一）。"""

    __tablename__ = "web_context_definitions"

    context_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("web_threads.thread_id", ondelete="CASCADE"),
        primary_key=True,
    )
    # ponytail: 用通用 JSON 而非 dialect JSONB，兼容 sqlite 内存测试；迁移文件仍建 JSONB（postgres 生产库）
    authored_messages: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    execution_messages: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    repair_manifest: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    issues: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    definition_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    projection_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    projection_status: Mapped[str] = mapped_column(String(32), nullable=False)
    initial_message_ids: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    initial_checkpoint_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    decided_definition_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_projection_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class WebContextSource(Base):
    """派生 Context 的有序父来源记录。"""

    __tablename__ = "web_context_sources"
    __table_args__ = (
        UniqueConstraint("context_id", "position", name="uq_web_context_source_position"),
    )

    source_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    context_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("web_threads.thread_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parent_context_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("web_threads.thread_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source_checkpoint_id: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ContextSourceRef(BaseModel):
    context_id: str = Field(min_length=1)
    checkpoint_id: str = Field(min_length=1)


class ContextDeriveCreate(BaseModel):
    title: str = Field(default="新 Context", min_length=1, max_length=200)
    sources: list[ContextSourceRef] = Field(min_length=1)
    messages: list[dict[str, Any]] = Field(default_factory=list)


class ContextDefinitionUpdate(BaseModel):
    messages: list[dict[str, Any]] = Field(default_factory=list)


class ContextProjectionDecision(BaseModel):
    decision: Literal["accept", "reject"]
    definition_hash: str = Field(min_length=64, max_length=64)
    projection_hash: str = Field(min_length=64, max_length=64)
