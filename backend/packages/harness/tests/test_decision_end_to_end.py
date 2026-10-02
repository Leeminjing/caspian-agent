"""本文件验证并行改表与工作 Run 的表、历史、checkpoint、模型输入和工具副作用一致。

输入为暂停中的 A 模型调用、B 改表请求及旧共享 checkpoint；输出为五方一致的修订与一次工具执行。
工作流在 A 调用期间提交并重试 B，恢复 A 后按新表复核，再检查旧恢复点补齐和持久结果。
示例：`pytest tests/test_decision_end_to_end.py`。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from langchain.agents.middleware import ModelRequest
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.runtime import ExecutionInfo, Runtime
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from caspian.agents.middlewares.decision_action_review_middleware import DecisionActionReviewMiddleware
from caspian.agents.middlewares.decision_table_middleware import DecisionTableMiddleware
from caspian.decision_governance.convergence_runtime import hash_messages, missing_shared_messages, record_work_progress, synchronize
from caspian.decision_governance.domain import Row
from caspian.decision_governance.model_calls import read_model_calls
from caspian.decision_governance.policy import Actor
from caspian.decision_governance.review_store import read_reviews
from caspian.decision_governance.service import DecisionTableService
from caspian.decision_governance.shared_checkpoint import SharedCheckpointWriter
from caspian.persistence.base import Base


def test_parallel_edit_recovery_and_action_review_agree_across_five_records():
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
        writer = SharedCheckpointWriter(InMemorySaver())
        user = Actor("u", "user")
        model_entered = asyncio.Event()
        model_release = asyncio.Event()
        seen_request = []
        reviewed_revisions = []
        executions = 0
        async def model_handler(request):
            seen_request.append(request)
            model_entered.set()
            await model_release.wait()
            return AIMessage(
                id="a-model", content="准备执行",
                tool_calls=[{"name": "bash_tool", "args": {"command": "encrypt"}, "id": "tool-a"}],
            )
        async def reviewer(_messages, binding, _args, table, _context, _factory, **_kwargs):
            reviewed_revisions.append((binding.table_revision, table.revision))
            return {
                "action_name": binding.action_name, "args_hash": binding.args_hash,
                "actor_id": binding.actor_id, "table_revision": binding.table_revision,
                "related_rows": ["r"], "conflict": "none", "decision": "keep",
                "reason": "命令已按当前加密决策复核，可以继续",
            }
        async def tool_handler(request):
            nonlocal executions
            executions += 1
            return ToolMessage(id="a-tool-result", content="executed", name="bash_tool", tool_call_id=request.tool_call["id"])
        try:
            await table_service.current("u", "t", user)
            work = HumanMessage(id="a-start", content="A 开始工作")
            await record_work_progress(factory, "u", "t", "run-a", [work], writer)
            old_shared = await writer.read("u", "t")
            old_messages = list(old_shared.values["messages"])
            model_runtime = Runtime(
                context={"user_id": "u", "thread_id": "t", "run_id": "run-a"},
                execution_info=ExecutionInfo("old-ck", "", "task", "decision-run:t:run-a"),
            )
            model_request = ModelRequest(
                model=FakeListChatModel(responses=["unused"]), messages=[work], runtime=model_runtime,
            )
            with patch("caspian.agents.middlewares.decision_table_middleware.service", return_value=table_service), patch(
                "caspian.agents.middlewares.decision_table_middleware.get_session", factory
            ), patch("caspian.agents.middlewares.decision_action_review_middleware.service", return_value=table_service), patch(
                "caspian.agents.middlewares.decision_action_review_middleware.get_session", factory
            ), patch("caspian.agents.middlewares.decision_action_review_middleware.review_action", reviewer), patch(
                "caspian.agents.middlewares.decision_action_review_middleware.assess_risk",
                new=lambda *_args, **_kwargs: asyncio.sleep(0, result={"risk": "high", "reason": "当前命令可能固化执行决策"}),
            ):
                model_task = asyncio.create_task(DecisionTableMiddleware().awrap_model_call(model_request, model_handler))
                await model_entered.wait()
                committed = await table_service.submit(
                    user_id="u", thread_id="t", actor=user,
                    run_id="run-b", source="ui", idempotency_key="b-request",
                    base_revision=0, rows=(Row("r", "必须加密", "保留", 3),), reason="B 要求加密",
                )
                repeated = await table_service.submit(
                    user_id="u", thread_id="t", actor=user,
                    run_id="run-b", source="ui", idempotency_key="b-request",
                    base_revision=0, rows=(Row("r", "必须加密", "保留", 3),), reason="B 要求加密",
                )
                await synchronize(factory, "u", "t", writer)
                model_release.set()
                model_response = await model_task
                tool_runtime = SimpleNamespace(
                    context={"user_id": "u", "thread_id": "t", "run_id": "run-a"},
                    config={"configurable": {"run_id": "run-a", "thread_id": "decision-run:t:run-a"}},
                    execution_info=SimpleNamespace(thread_id="decision-run:t:run-a"),
                )
                tool_request = ToolCallRequest(
                    tool_call={"name": "bash_tool", "args": {"command": "encrypt"}, "id": "tool-a"},
                    tool=None, state={"messages": [work, model_response]}, runtime=tool_runtime,
                )
                tool_response = await DecisionActionReviewMiddleware().awrap_tool_call(tool_request, tool_handler)
                await record_work_progress(factory, "u", "t", "run-a", [work, model_response, tool_response], writer)
                restored_delta = await missing_shared_messages(
                    factory, "u", "t", old_messages, writer, hash_messages(old_messages),
                )
            current = await table_service.current("u", "t", user)
            history = await table_service.history("u", "t")
            shared = await writer.read("u", "t")
            calls = await read_model_calls(factory, "u", "t")
            reviews = await read_reviews(factory, "u", "t")
            assert current.revision == 1 and current.rows[0].requirement == "必须加密"
            assert committed.operation_id == repeated.operation_id
            assert len(history) == 1 and history[0].status == "committed" and history[0].run_id == "run-b"
            assert shared.values["table_revision"] == 1
            assert any("B 要求加密" in str(message.content) for message in shared.values["messages"])
            assert any(message.id == "a-tool-result" for message in shared.values["messages"])
            assert any("B 要求加密" in str(message.content) for message in restored_delta)
            assert 'revision="0"' in seen_request[0].system_message.text
            assert len(calls) == 1 and calls[0].table_revision == 0
            assert reviewed_revisions == [(1, 1)]
            assert len(reviews) == 1 and reviews[0].status == "executed" and reviews[0].table_revision == 1
            assert tool_response.content == "executed" and executions == 1
        finally:
            model_release.set()
            await engine.dispose()
    asyncio.run(scenario())
