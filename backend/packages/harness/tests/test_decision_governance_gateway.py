"""本文件验证网关能从当前表追溯 Run、审批与操作前后差异。

输入为认证用户、会话和已提交操作；输出为当前快照、历史列表和操作详情 API 数据。
工作流在隔离数据库调用实际路由函数，核对空表、提交及双向历史字段。
示例：`pytest tests/test_decision_governance_gateway.py`。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.gateway.context.models import WebThread
from backend.app.gateway.routers.decision_table import get_decision_table, get_decision_table_operation, get_decision_table_operations
from backend.app.gateway.routers.thread_runs import get_run
from caspian.decision_governance.domain import Row
from caspian.decision_governance.policy import Actor
from caspian.decision_governance.service import DecisionTableService
from caspian.decision_governance.run_audit import create_run_audit, update_run_audit
from caspian.persistence.base import Base


def test_current_table_and_operation_detail_link_to_run_and_revisions():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(
                sync, tables=[WebThread.__table__, *(
                    table for table in Base.metadata.tables.values() if table.name.startswith("decision_table_")
                )]
            ))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async def no_findings(_candidate, _current):
            return ()
        service = DecisionTableService(factory, checker=no_findings)
        request = SimpleNamespace(state=SimpleNamespace(current_user=SimpleNamespace(id="u")))
        async with factory() as session, session.begin():
            session.add(WebThread(thread_id="t", user_id="u"))
        try:
            await create_run_audit(
                factory, user_id="u", thread_id="t", run_id="run-b",
                origin_run_id="run-b", kind="decision_edit",
            )
            await update_run_audit(
                factory, user_id="u", thread_id="t", run_id="run-b", status="success",
            )
            with patch("backend.app.gateway.routers.decision_table.get_session", factory), patch(
                "backend.app.gateway.routers.decision_table.service", return_value=service,
            ):
                empty = await get_decision_table("t", request)
                assert empty["read_status"] == "verified_empty" and empty["rows"] == []
                operation = await service.submit(
                    user_id="u", thread_id="t", actor=Actor("u", "user"),
                    run_id="run-b", source="ui", idempotency_key="edit",
                    base_revision=0, rows=(Row("r", "required", "保留", 3),), reason="specific reason",
                )
                current = await get_decision_table("t", request)
                history = await get_decision_table_operations("t", request)
                detail = await get_decision_table_operation("t", operation.operation_id, request)
                assert current["revision"] == 1 and current["read_status"] == "verified_current"
                assert history["operations"][0]["operation_id"] == operation.operation_id
                assert detail["operation"]["run_id"] == "run-b"
                assert detail["run"]["status"] == "success"
                assert detail["operation"]["changes"][0]["kind"] == "add"
                assert detail["before"] == []
                assert detail["after"][0]["requirement"] == "required"
                assert [event["event_type"] for event in detail["events"]] == ["processing", "committed"]
            with patch("backend.app.gateway.routers.thread_runs.get_session", factory):
                recovered = await get_run("t", "run-b", request)
                assert recovered["origin_run_id"] == "run-b"
                assert recovered["status"] == "success"
        finally:
            await engine.dispose()
    asyncio.run(scenario())
