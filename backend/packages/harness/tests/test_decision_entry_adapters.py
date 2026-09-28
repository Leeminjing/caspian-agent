"""本文件验证界面、Agent 工具与承诺阶段都进入同一改表服务。

输入为三个入口的等价新增候选、Run 与认证身份；输出为同一状态机上的操作和权限结论。
工作流让界面所有者直接提交，两个未获授权的 Agent 被拒绝，并核对每条记录的来源与 Run。
示例：`pytest tests/test_decision_entry_adapters.py`。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import HumanMessage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from caspian.agents.middlewares.decision_table_edit_middleware import DecisionTableEditMiddleware
from caspian.decision_governance.commitment import propose_contract_table
from caspian.decision_governance.service import DecisionTableService
from caspian.persistence.base import Base
from caspian.tools.builtins.update_decision_table_tool import update_decision_table


def test_all_three_entry_paths_use_governed_operation_records():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(
                sync, tables=[table for table in Base.metadata.tables.values() if table.name.startswith("decision_table_")]
            ))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async def no_findings(_candidate, _current):
            return ()
        table_service = DecisionTableService(factory, checker=no_findings)
        try:
            with patch("caspian.agents.middlewares.decision_table_edit_middleware.service", return_value=table_service), patch(
                "caspian.tools.builtins.update_decision_table_tool.service", return_value=table_service
            ), patch("caspian.decision_governance.commitment.service", return_value=table_service):
                ui_runtime = SimpleNamespace(
                    context={"user_id": "u", "thread_id": "ui", "run_id": "run-ui"},
                    config={"configurable": {"run_id": "run-ui"}},
                    execution_info=SimpleNamespace(thread_id="decision-run:ui:run-ui"),
                )
                ui_request = HumanMessage(
                    id="request-ui", content="改表",
                    additional_kwargs={"decision_table_edit": {
                        "base_revision": 0, "reason": "用户提交等价新增",
                        "rows": [{"requirement": "必须加密", "decision": "保留", "priority": 3}],
                    }},
                )
                ui_result = await DecisionTableEditMiddleware()._run({"messages": [ui_request]}, ui_runtime)
                assert ui_result["messages"][0].additional_kwargs["decision_table_edit_ack"]["status"] == "committed"

                tool_runtime = SimpleNamespace(
                    context={"user_id": "u", "thread_id": "tool"},
                    config={"configurable": {"run_id": "run-tool"}},
                    execution_info=SimpleNamespace(thread_id="decision-run:tool:run-tool"),
                    tool_call_id="call-tool",
                )
                result = await update_decision_table.coroutine(
                    operation="add", requirement="必须加密", decision="保留", priority=3,
                    reason="Agent 提出等价新增", runtime=tool_runtime,
                )
                assert "权限不足" in result

                commitment = await propose_contract_table(
                    {"user_id": "u", "thread_id": "commitment", "run_id": "run-commitment"},
                    {"2": {"requirements": ["必须加密"], "discarded_requirements": []},
                     "3": {"requirements": [{"requirement": "必须加密", "priority": 3}]}},
                )
                assert commitment.status == "rejected"
                operations = [
                    (await table_service.history("u", thread))[0]
                    for thread in ("ui", "tool", "commitment")
                ]
                assert [item.source for item in operations] == ["ui_edit", "agent_tool", "commitment"]
                assert [item.run_id for item in operations] == ["run-ui", "run-tool", "run-commitment"]
                assert [item.status for item in operations] == ["committed", "rejected", "rejected"]
        finally:
            await engine.dispose()
    asyncio.run(scenario())
