"""add archived_at to web_threads

Revision ID: b7e8f9a0c1d2
Revises: f1a2b3c4d5e6
Create Date: 2026-09-10

为 web_threads 新增 archived_at（可空时间戳）列，标记会话是否已归档（软删除）。
既有行默认 NULL（未归档），不改变任何既有数据；纯增量列。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b7e8f9a0c1d2"
down_revision: Union[str, Sequence[str], None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "web_threads",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("web_threads", "archived_at")
