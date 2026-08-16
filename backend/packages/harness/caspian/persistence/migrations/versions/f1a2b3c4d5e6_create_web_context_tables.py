"""create web context tables

Revision ID: f1a2b3c4d5e6
Revises: c1d2e3f4a5b6
Create Date: 2026-08-16

新增 Recursive Context Forking 的三张表：
    web_threads — 线程注册表（thread_id 即 context_id，含主运行锁定与 usage 聚合列）
    web_context_definitions — 派生 Context 的用户定义与执行投影记录
    web_context_sources — 派生 Context 的有序父来源记录
不改动既有表与数据。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "c1d2e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "web_threads",
        sa.Column("thread_id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False, index=True),
        sa.Column("title", sa.String(200), nullable=True),
        sa.Column("main_run_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("prompt_input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("prompt_cache_hit_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "web_context_definitions",
        sa.Column("context_id", sa.String(36), primary_key=True),
        sa.Column("authored_messages", JSONB(), nullable=False),
        sa.Column("execution_messages", JSONB(), nullable=False),
        sa.Column("repair_manifest", JSONB(), nullable=False),
        sa.Column("issues", JSONB(), nullable=False),
        sa.Column("definition_hash", sa.String(64), nullable=False),
        sa.Column("projection_hash", sa.String(64), nullable=False),
        sa.Column("projection_status", sa.String(32), nullable=False),
        sa.Column("initial_message_ids", JSONB(), nullable=False),
        sa.Column("initial_checkpoint_id", sa.Text(), nullable=True),
        sa.Column("decision", sa.String(16), nullable=True),
        sa.Column("decided_definition_hash", sa.String(64), nullable=True),
        sa.Column("decided_projection_hash", sa.String(64), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["context_id"], ["web_threads.thread_id"], ondelete="CASCADE"
        ),
    )

    op.create_table(
        "web_context_sources",
        sa.Column("source_id", sa.String(36), primary_key=True),
        sa.Column("context_id", sa.String(36), nullable=False, index=True),
        sa.Column("parent_context_id", sa.String(36), nullable=False, index=True),
        sa.Column("source_checkpoint_id", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["context_id"], ["web_threads.thread_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["parent_context_id"], ["web_threads.thread_id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "context_id", "position", name="uq_web_context_source_position"
        ),
    )


def downgrade() -> None:
    op.drop_table("web_context_sources")
    op.drop_table("web_context_definitions")
    op.drop_table("web_threads")
