"""本文件对外提供决策表治理表的 Alembic upgrade 与 downgrade。

输入为已迁移到 b7e8f9a0c1d2 的数据库；输出为表头、修订、操作、事件、审批、权限及 outbox。
工作流先建权威表头和快照，再建审计与投递表；回退按反序删除。
示例：`alembic upgrade d8e9f0a1b2c3`。
"""

from alembic import op
import sqlalchemy as sa

revision = "d8e9f0a1b2c3"
down_revision = "b7e8f9a0c1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "decision_table_heads",
        sa.Column("user_id", sa.String(64), primary_key=True),
        sa.Column("thread_id", sa.String(64), primary_key=True),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("permission_revision", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "decision_table_revisions",
        sa.Column("user_id", sa.String(64), primary_key=True),
        sa.Column("thread_id", sa.String(64), primary_key=True),
        sa.Column("revision", sa.Integer, primary_key=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("rows", sa.JSON, nullable=False),
        sa.Column("operation_id", sa.String(36)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id", "thread_id"], ["decision_table_heads.user_id", "decision_table_heads.thread_id"], ondelete="CASCADE"),
    )
    op.create_table(
        "decision_table_operations",
        sa.Column("operation_id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("thread_id", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("actor_kind", sa.String(16), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("base_revision", sa.Integer, nullable=False),
        sa.Column("permission_revision", sa.Integer, nullable=False),
        sa.Column("candidate_hash", sa.String(64), nullable=False),
        sa.Column("candidate_rows", sa.JSON, nullable=False),
        sa.Column("changes", sa.JSON, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("checks", sa.JSON, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("result_revision", sa.Integer),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "thread_id", "idempotency_key", name="uq_decision_operation_idempotency"),
    )
    op.create_index("ix_decision_table_operations_user_id", "decision_table_operations", ["user_id"])
    op.create_index("ix_decision_table_operations_thread_id", "decision_table_operations", ["thread_id"])
    op.create_table(
        "decision_table_operation_events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("operation_id", sa.String(36), nullable=False),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("operation_id", "ordinal", name="uq_decision_operation_event_order"),
    )
    op.create_index("ix_decision_table_operation_events_operation_id", "decision_table_operation_events", ["operation_id"])
    op.create_table(
        "decision_table_approvals",
        sa.Column("approval_id", sa.String(36), primary_key=True),
        sa.Column("operation_id", sa.String(36), nullable=False),
        sa.Column("approver_id", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("candidate_hash", sa.String(64), nullable=False),
        sa.Column("base_revision", sa.Integer, nullable=False),
        sa.Column("permission_revision", sa.Integer, nullable=False),
        sa.Column("allowed_ops", sa.JSON, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_decision_table_approvals_operation_id", "decision_table_approvals", ["operation_id"])
    op.create_table(
        "decision_table_permissions",
        sa.Column("user_id", sa.String(64), primary_key=True),
        sa.Column("thread_id", sa.String(64), primary_key=True),
        sa.Column("revision", sa.Integer, primary_key=True),
        sa.Column("policy", sa.JSON, nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "decision_table_outbox",
        sa.Column("sequence", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(36), nullable=False, unique=True),
        sa.Column("operation_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("thread_id", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("operation_id", "event_type", name="uq_decision_outbox_operation_event"),
    )
    op.create_index("ix_decision_table_outbox_thread_id", "decision_table_outbox", ["thread_id"])
    op.create_table(
        "decision_table_session_events",
        sa.Column("sequence", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(36), nullable=False, unique=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("thread_id", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("operation_id", sa.String(36)),
        sa.Column("parent_event_id", sa.String(36)),
        sa.Column("tool_call_id", sa.String(64)),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_decision_table_session_events_thread_id", "decision_table_session_events", ["thread_id"])
    op.create_table(
        "decision_table_run_cursors",
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("thread_id", sa.String(64), nullable=False),
        sa.Column("last_event_id", sa.String(36)),
        sa.Column("last_sequence", sa.Integer, nullable=False),
        sa.Column("message_fingerprints", sa.JSON, nullable=False),
        sa.Column("branch_checkpoint_id", sa.Text),
    )
    op.create_index("ix_decision_table_run_cursors_thread_id", "decision_table_run_cursors", ["thread_id"])
    op.create_table(
        "decision_table_shared_projections",
        sa.Column("user_id", sa.String(64), primary_key=True),
        sa.Column("thread_id", sa.String(64), primary_key=True),
        sa.Column("last_sequence", sa.Integer, nullable=False),
        sa.Column("table_revision", sa.Integer, nullable=False),
        sa.Column("messages", sa.JSON, nullable=False),
        sa.Column("pending_messages", sa.JSON, nullable=False),
        sa.Column("checkpoint_id", sa.Text),
    )
    op.create_table(
        "decision_table_model_calls",
        sa.Column("sequence", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("call_id", sa.String(36), nullable=False, unique=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("thread_id", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("table_revision", sa.Integer, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("reminder_reason", sa.String(64)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("checked_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_decision_table_model_calls_thread_id", "decision_table_model_calls", ["thread_id"])
    op.create_table(
        "decision_table_action_reviews",
        sa.Column("review_id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("thread_id", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("tool_call_id", sa.String(64), nullable=False),
        sa.Column("action_name", sa.String(128), nullable=False),
        sa.Column("args_hash", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("table_revision", sa.Integer, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("trigger_reason", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("conclusion", sa.JSON),
        sa.Column("result", sa.JSON),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("run_id", "tool_call_id", "action_name", "args_hash", "actor_id", "table_revision", name="uq_decision_action_binding"),
    )
    op.create_index("ix_decision_table_action_reviews_thread_id", "decision_table_action_reviews", ["thread_id"])


def downgrade() -> None:
    op.drop_table("decision_table_shared_projections")
    op.drop_table("decision_table_run_cursors")
    op.drop_table("decision_table_action_reviews")
    op.drop_table("decision_table_model_calls")
    op.drop_table("decision_table_session_events")
    op.drop_table("decision_table_outbox")
    op.drop_table("decision_table_permissions")
    op.drop_table("decision_table_approvals")
    op.drop_table("decision_table_operation_events")
    op.drop_table("decision_table_operations")
    op.drop_table("decision_table_revisions")
    op.drop_table("decision_table_heads")
