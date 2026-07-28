"""create store tables

Revision ID: a2c3e4f5d6b7
Revises: 41c1e13fc246
Create Date: 2026-07-09

本迁移照抄 LangGraph 官方 AsyncPostgresStore 的 MIGRATIONS 和 VECTOR_MIGRATIONS。
来源: langgraph/store/postgres/base.py
"""

from typing import Sequence, Union

from alembic import op

revision: str = "a2c3e4f5d6b7"
down_revision: Union[str, Sequence[str], None] = "41c1e13fc246"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # === 官方 MIGRATIONS（4 条）===

    # 1. 主表
    op.execute("""
        CREATE TABLE IF NOT EXISTS store (
            prefix text NOT NULL,
            key text NOT NULL,
            value jsonb NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (prefix, key)
        )
    """)

    # 2. prefix 查询索引
    # ponytail: 官方用 CONCURRENTLY，但 Alembic 事务内不支持。新表无并发写，用普通索引等价。
    op.execute("""
        CREATE INDEX IF NOT EXISTS store_prefix_idx ON store USING btree (prefix text_pattern_ops)
    """)

    # 3. TTL 列
    op.execute("""
        ALTER TABLE store
        ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP WITH TIME ZONE,
        ADD COLUMN IF NOT EXISTS ttl_minutes INT
    """)

    # 4. TTL 索引
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_store_expires_at ON store (expires_at)
        WHERE expires_at IS NOT NULL
    """)

    # === 官方 VECTOR_MIGRATIONS（3 条）===

    # 1. pgvector 扩展
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
                CREATE EXTENSION vector;
            END IF;
        END $$;
    """)

    # 2. 向量表（dims=1024 对应 text-embedding-v4）
    op.execute("""
        CREATE TABLE IF NOT EXISTS store_vectors (
            prefix text NOT NULL,
            key text NOT NULL,
            field_name text NOT NULL,
            embedding vector(1024),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (prefix, key, field_name),
            FOREIGN KEY (prefix, key) REFERENCES store(prefix, key) ON DELETE CASCADE
        )
    """)

    # 3. 向量索引
    # ponytail: 官方用 CONCURRENTLY，Alembic 事务内不支持，用 ivfflat 普通索引等价。
    op.execute("""
        CREATE INDEX IF NOT EXISTS store_vectors_embedding_idx ON store_vectors
        USING ivfflat (embedding vector_cosine_ops)
    """)


def downgrade() -> None:
    # 逆序 DROP：先删子表再删父表
    op.execute("DROP TABLE IF EXISTS store_vectors")
    op.drop_table("store")
    # 不删除 vector extension（共享扩展）
