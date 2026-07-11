"""create users table

Revision ID: c1d2e3f4a5b6
Revises: a2c3e4f5d6b7
Create Date: 2026-07-11

新增 users 表，存储用户认证数据，支持 UUID 主键、email 唯一登录标识、
OAuth 预留字段。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, Sequence[str], None] = "a2c3e4f5d6b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(320), unique=True, nullable=False),
        sa.Column("password_hash", sa.String(128), nullable=True),
        sa.Column("token_version", sa.Integer(), default=0, nullable=False),
        sa.Column("display_name", sa.String(100), nullable=True),
        sa.Column("oauth_provider", sa.String(32), nullable=True),
        sa.Column("oauth_id", sa.String(256), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )
    # email 唯一索引已通过 unique=True 在列定义中创建


def downgrade() -> None:
    op.drop_table("users")
