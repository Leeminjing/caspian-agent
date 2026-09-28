"""本文件验证决策表治理迁移的建表、约束及回退。

输入为 SQLite 内存数据库；输出为建表、唯一约束与回退断言。
工作流在隔离数据库执行 Alembic upgrade/downgrade，不连接生产库。
示例：`pytest tests/test_decision_governance_migration.py`。
"""

import importlib
from unittest.mock import patch

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


def test_migration_roundtrip_and_operation_identity_constraint():
    migration = importlib.import_module(
        "caspian.persistence.migrations.versions.d8e9f0a1b2c3_decision_table_governance"
    )
    audit_migration = importlib.import_module(
        "caspian.persistence.migrations.versions.e9f0a1b2c3d4_decision_run_audit"
    )
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        with patch.object(migration, "op", operations):
            migration.upgrade()
            with patch.object(audit_migration, "op", operations):
                audit_migration.upgrade()
            assert "decision_table_heads" in inspect(connection).get_table_names()
            assert "decision_table_operations" in inspect(connection).get_table_names()
            assert "decision_table_run_audits" in inspect(connection).get_table_names()
            assert "action_args" in {column["name"] for column in inspect(connection).get_columns("decision_table_action_reviews")}
            values = {
                "operation_id": "op-1", "user_id": "u", "thread_id": "t", "run_id": "r",
                "actor_id": "u", "actor_kind": "user", "source": "ui", "idempotency_key": "k",
                "request_hash": "h", "base_revision": 0, "permission_revision": 0,
                "candidate_hash": "h", "candidate_rows": "[]", "changes": "[]",
                "reason": "test", "checks": "[]", "status": "processing",
            }
            fields = ", ".join(values)
            parameters = ", ".join(f":{name}" for name in values)
            connection.execute(text(f"INSERT INTO decision_table_operations ({fields}) VALUES ({parameters})"), values)
            with pytest.raises(IntegrityError):
                with connection.begin_nested():
                    connection.execute(
                        text(f"INSERT INTO decision_table_operations ({fields}) VALUES ({parameters})"),
                        {**values, "operation_id": "op-2"},
                    )
            with patch.object(audit_migration, "op", operations):
                audit_migration.downgrade()
            migration.downgrade()
            assert "decision_table_heads" not in inspect(connection).get_table_names()
