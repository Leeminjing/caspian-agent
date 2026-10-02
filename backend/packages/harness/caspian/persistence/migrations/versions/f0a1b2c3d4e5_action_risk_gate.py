"""本文件对外提供动作风险闸门的增量 Alembic 迁移。

输入为 e9f0a1b2c3d4 数据库；输出为含上下文绑定和两阶段判断字段的动作记录。
工作流保留历史终态，未完成旧复核重置为风险待判，再重建唯一绑定约束；回退仅移除新增字段。
示例：`alembic upgrade f0a1b2c3d4e5`。
"""

from alembic import op
import sqlalchemy as sa

revision = "f0a1b2c3d4e5"
down_revision = "e9f0a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("decision_table_action_reviews") as batch:
        batch.add_column(sa.Column("context_hash", sa.String(64), nullable=False, server_default=""))
        batch.add_column(sa.Column("risk_outcome", sa.String(16), nullable=True))
        batch.add_column(sa.Column("risk_reason", sa.Text, nullable=True))
        batch.add_column(sa.Column("risk_checked_at", sa.DateTime(timezone=True), nullable=True))
        batch.drop_constraint("uq_decision_action_binding", type_="unique")
        batch.create_unique_constraint("uq_decision_action_binding", ["run_id", "tool_call_id", "action_name", "args_hash", "actor_id", "table_revision", "context_hash"])
    op.execute(sa.text("UPDATE decision_table_action_reviews SET trigger_reason = 'legacy_tool_list', status = 'risk_pending', conclusion = NULL WHERE status IN ('pending_review', 'reviewed', 'awaiting_human')"))
    op.execute(sa.text("UPDATE decision_table_action_reviews SET trigger_reason = 'legacy_tool_list' WHERE trigger_reason = 'critical_tool'"))


def downgrade() -> None:
    with op.batch_alter_table("decision_table_action_reviews") as batch:
        batch.drop_constraint("uq_decision_action_binding", type_="unique")
        batch.create_unique_constraint("uq_decision_action_binding", ["run_id", "tool_call_id", "action_name", "args_hash", "actor_id", "table_revision"])
        batch.drop_column("risk_checked_at")
        batch.drop_column("risk_reason")
        batch.drop_column("risk_outcome")
        batch.drop_column("context_hash")
