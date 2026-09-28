"""本文件对外提供决策表 Run 审计及动作参数的增量迁移。

输入为已完成 d8e9f0a1b2c3 的数据库；输出为持久 Run 状态表及动作参数列。
工作流增量添加历史兼容的可空参数列和 Run 审计表，回退时按相反顺序移除。
示例：`alembic upgrade e9f0a1b2c3d4`。
"""

from alembic import op
import sqlalchemy as sa

revision = "e9f0a1b2c3d4"
down_revision = "d8e9f0a1b2c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("decision_table_action_reviews", sa.Column("action_args", sa.JSON, nullable=True))
    op.create_table(
        "decision_table_run_audits",
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("thread_id", sa.String(64), nullable=False),
        sa.Column("origin_run_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_decision_table_run_audits_thread_id", "decision_table_run_audits", ["thread_id"])


def downgrade() -> None:
    op.drop_index("ix_decision_table_run_audits_thread_id", table_name="decision_table_run_audits")
    op.drop_table("decision_table_run_audits")
    op.drop_column("decision_table_action_reviews", "action_args")
