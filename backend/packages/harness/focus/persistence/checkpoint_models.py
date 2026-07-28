"""
本文件对外提供四张 checkpointer 表的 ORM 模型，字段完全模仿 LangGraph 官方 checkpoint-postgres schema。

对外提供:
    CheckpointMigrations — checkpoint 迁移版本记录表
    Checkpoints        — checkpoint 主体记录表 (JSONB)
    CheckpointBlobs    — channel 序列化值存储表 (bytea)
    CheckpointWrites   — task/node 中间写入记录表 (bytea)

输入: 无 — 本文件为纯定义文件
输出: 四个 ORM 模型类，继承自 Base

示例:
    from focus.persistence.checkpoint_models import Checkpoints

    async with get_session() as session:
        result = await session.execute(select(Checkpoints).where(...))
"""

from sqlalchemy import Integer, Text, String
from sqlalchemy.dialects.postgresql import JSONB, BYTEA
from sqlalchemy.orm import Mapped, mapped_column

from focus.persistence.base import Base


class CheckpointMigrations(Base):
    __tablename__ = "checkpoint_migrations"

    v: Mapped[int] = mapped_column(Integer, primary_key=True)


class Checkpoints(Base):
    __tablename__ = "checkpoints"

    thread_id: Mapped[str] = mapped_column(Text, primary_key=True)
    checkpoint_ns: Mapped[str] = mapped_column(Text, primary_key=True, default="", server_default="")
    checkpoint_id: Mapped[str] = mapped_column(Text, primary_key=True)
    parent_checkpoint_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    type: Mapped[str | None] = mapped_column(Text, nullable=True)
    checkpoint: Mapped[dict] = mapped_column(JSONB, nullable=False)
    checkpoint_metadata: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default={}, server_default="{}")


class CheckpointBlobs(Base):
    __tablename__ = "checkpoint_blobs"

    thread_id: Mapped[str] = mapped_column(Text, primary_key=True)
    checkpoint_ns: Mapped[str] = mapped_column(Text, primary_key=True, default="", server_default="")
    channel: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[str] = mapped_column(Text, primary_key=True)
    type: Mapped[str | None] = mapped_column(Text, nullable=True)
    blob: Mapped[bytes | None] = mapped_column(BYTEA, nullable=True)


class CheckpointWrites(Base):
    __tablename__ = "checkpoint_writes"

    thread_id: Mapped[str] = mapped_column(Text, primary_key=True)
    checkpoint_ns: Mapped[str] = mapped_column(Text, primary_key=True, default="", server_default="")
    checkpoint_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(Text, primary_key=True)
    idx: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str | None] = mapped_column(Text, nullable=True)
    blob: Mapped[bytes | None] = mapped_column(BYTEA, nullable=True)
    task_path: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
