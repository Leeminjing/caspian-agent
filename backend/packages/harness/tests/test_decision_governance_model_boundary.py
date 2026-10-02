"""本文件验证主模型边界实际收到唯一有效表及每次调用的修订记录。

输入为旧 system 消息、空表和运行中更新；输出为捕获的最终 ModelRequest 与持久调用记录。
工作流在隔离数据库调用中间件，确保更新后下一轮换新表，读取失败则不调用模型。
示例：`pytest tests/test_decision_governance_model_boundary.py`。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from langchain.agents.middleware import ModelRequest
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.runtime import ExecutionInfo, Runtime
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from caspian.agents.middlewares.decision_table_middleware import DecisionTableMiddleware
from caspian.agents.middlewares.decision_action_review_middleware import DecisionActionReviewMiddleware
from caspian.agents.middlewares.builder import build_subagent_middlewares
from caspian.config.decision_review_config import DecisionReviewConfig
from caspian.decision_governance.domain import Row
from caspian.decision_governance.model_calls import read_model_calls
from caspian.decision_governance.review_store import read_reviews
from caspian.decision_governance.policy import Actor
from caspian.decision_governance.service import DecisionTableService
from caspian.persistence.base import Base


def test_empty_then_updated_table_in_actual_model_request():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(
                sync, tables=[table for table in Base.metadata.tables.values() if table.name.startswith("decision_table_")]
            ))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async def no_findings(_candidate, _current):
            return ()
        service = DecisionTableService(factory, checker=no_findings)
        middleware = DecisionTableMiddleware()
        runtime = Runtime(
            context={"user_id": "u", "run_id": "run", "thread_id": "t"},
            execution_info=ExecutionInfo("ck", "", "task", "t"),
        )
        request = ModelRequest(
            model=FakeListChatModel(responses=["ok"]),
            messages=[SystemMessage(content='<decision_table version="old">stale</decision_table>', id="decision-table"), HumanMessage(content='<current_decision_table revision="old">stale</current_decision_table>')],
            system_message=SystemMessage(content='base instructions <decision_table version="old">stale</decision_table>'),
            runtime=runtime,
        )
        seen = []
        async def handler(bound):
            seen.append(bound)
            return AIMessage(content="ok")
        try:
            with patch("caspian.agents.middlewares.decision_table_middleware.service", return_value=service), patch(
                "caspian.agents.middlewares.decision_table_middleware.get_session", factory
            ):
                await middleware.awrap_model_call(request, handler)
                first = seen[-1]
                assert 'status="verified_empty"' in first.system_message.text
                assert len(first.messages) == 1
                assert "historical_decision_table" in first.messages[0].content
                assert "stale" not in first.system_message.text
                assert first.system_message.text.count("<current_decision_table ") == 1
                await service.submit(
                    user_id="u", thread_id="t", actor=Actor("u", "user"),
                    run_id="edit", source="ui", idempotency_key="edit-1",
                    base_revision=0, rows=(Row("r", "必须加密", "保留", 3),), reason="新增要求",
                )
                await middleware.awrap_model_call(request, handler)
                assert 'revision="1"' in seen[-1].system_message.text
                assert "必须加密" in seen[-1].system_message.text
                calls = await read_model_calls(factory, "u", "t")
                assert [call.table_revision for call in calls] == [0, 1]
                assert all(call.status == "completed" for call in calls)
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_bad_view_stops_model_call():
    async def scenario():
        middleware = DecisionTableMiddleware()
        runtime = Runtime(
            context={"user_id": "u", "run_id": "run", "thread_id": "t"},
            execution_info=ExecutionInfo("ck", "", "task", "t"),
        )
        request = ModelRequest(model=FakeListChatModel(responses=["ok"]), messages=[], runtime=runtime)
        called = 0
        async def handler(_bound):
            nonlocal called
            called += 1
            return AIMessage(content="ok")
        class BrokenService:
            async def current(self, *_args):
                raise RuntimeError("snapshot corrupt")
        with patch("caspian.agents.middlewares.decision_table_middleware.service", return_value=BrokenService()):
            with pytest.raises(RuntimeError, match="snapshot corrupt"):
                await middleware.awrap_model_call(request, handler)
        assert called == 0
    asyncio.run(scenario())


def test_periodic_reminder_counts_model_calls_not_messages():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(
                sync, tables=[table for table in Base.metadata.tables.values() if table.name.startswith("decision_table_")]
            ))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async def no_findings(_candidate, _current):
            return ()
        service = DecisionTableService(factory, checker=no_findings)
        config = type("Config", (), {"decision_review": DecisionReviewConfig(reminder_interval=2)})()
        runtime = Runtime(
            context={"user_id": "u", "run_id": "run", "thread_id": "t", "app_config": config},
            execution_info=ExecutionInfo("ck", "", "task", "t"),
        )
        request = ModelRequest(
            model=FakeListChatModel(responses=["ok"]),
            messages=[HumanMessage(content=f"message {n}") for n in range(8)],
            runtime=runtime,
        )
        seen = []
        async def handler(bound):
            seen.append(bound.system_message.text)
            return AIMessage(content="ok")
        try:
            with patch("caspian.agents.middlewares.decision_table_middleware.service", return_value=service), patch(
                "caspian.agents.middlewares.decision_table_middleware.get_session", factory
            ):
                for _ in range(3):
                    await DecisionTableMiddleware().awrap_model_call(request, handler)
            assert "<decision_review_reminder" not in seen[0]
            assert "<decision_review_reminder" not in seen[1]
            assert 'round="3"' in seen[2]
            assert [call.reminder_reason for call in await read_model_calls(factory, "u", "t")] == [None, None, "periodic"]
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_physical_run_checkpoint_uses_logical_decision_table_thread():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(
                sync, tables=[table for table in Base.metadata.tables.values() if table.name.startswith("decision_table_")]
            ))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async def no_findings(_candidate, _current):
            return ()
        service = DecisionTableService(factory, checker=no_findings)
        await service.submit(
            user_id="u", thread_id="t", actor=Actor("u", "user"),
            run_id="edit", source="ui", idempotency_key="edit",
            base_revision=0, rows=(Row("r", "logical table", "保留", 3),), reason="test",
        )
        runtime = Runtime(
            context={"user_id": "u", "run_id": "run-a", "thread_id": "t"},
            execution_info=ExecutionInfo("ck", "", "task", "decision-run:t:run-a"),
        )
        request = ModelRequest(model=FakeListChatModel(responses=["ok"]), messages=[], runtime=runtime)
        seen = []
        try:
            with patch("caspian.agents.middlewares.decision_table_middleware.service", return_value=service), patch(
                "caspian.agents.middlewares.decision_table_middleware.get_session", factory
            ):
                await DecisionTableMiddleware().awrap_model_call(request, lambda bound: seen.append(bound) or asyncio.sleep(0, result=AIMessage(content="ok")))
                await DecisionTableMiddleware(actor_id="subagent").awrap_model_call(
                    request, lambda bound: seen.append(bound) or asyncio.sleep(0, result=AIMessage(content="ok")),
                )
            assert "logical table" in seen[0].system_message.text
            assert [call.thread_id for call in await read_model_calls(factory, "u", "t")] == ["t", "t"]
            assert [call.actor_id for call in await read_model_calls(factory, "u", "t")] == ["lead", "subagent"]
            assert any(isinstance(item, DecisionTableMiddleware) and item._actor_id == "subagent" for item in build_subagent_middlewares())
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_sent_model_call_keeps_old_revision_while_new_table_governs_tool():
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
        runtime = Runtime(
            context={"user_id": "u", "run_id": "work", "thread_id": "t"},
            execution_info=ExecutionInfo("ck", "", "task", "decision-run:t:work"),
        )
        model_request = ModelRequest(
            model=FakeListChatModel(responses=["ok"]), messages=[HumanMessage(content="执行命令")],
            runtime=runtime,
        )
        seen_model = []
        seen_review = []
        executions = 0
        async def answer(bound):
            seen_model.append(bound)
            return AIMessage(content="ok")
        async def review(_messages, binding, _args, table, _context, _factory, **_kwargs):
            seen_review.append((binding, table))
            return {
                "action_name": binding.action_name, "args_hash": binding.args_hash,
                "actor_id": binding.actor_id, "table_revision": binding.table_revision,
                "related_rows": ["r"], "conflict": "none", "decision": "keep",
                "reason": "已按新修订核对具体动作和必须加密条目",
            }
        async def high_risk(*_args, **_kwargs):
            return {"risk": "high", "reason": "当前命令可能固化执行决策"}
        async def execute(request):
            nonlocal executions
            executions += 1
            return ToolMessage(content="done", tool_call_id=request.tool_call["id"], name="bash_tool")
        try:
            with patch("caspian.agents.middlewares.decision_table_middleware.service", return_value=table_service), patch(
                "caspian.agents.middlewares.decision_table_middleware.get_session", factory
            ), patch("caspian.agents.middlewares.decision_action_review_middleware.service", return_value=table_service), patch(
                "caspian.agents.middlewares.decision_action_review_middleware.get_session", factory
            ), patch("caspian.agents.middlewares.decision_action_review_middleware.review_action", review), patch(
                "caspian.agents.middlewares.decision_action_review_middleware.assess_risk", high_risk):
                await DecisionTableMiddleware().awrap_model_call(model_request, answer)
                await table_service.submit(
                    user_id="u", thread_id="t", actor=Actor("u", "user"),
                    run_id="edit", source="ui", idempotency_key="edit",
                    base_revision=0, rows=(Row("r", "必须加密", "保留", 3),), reason="添加当前决策",
                )
                tool_runtime = SimpleNamespace(
                    context={"user_id": "u", "run_id": "work", "thread_id": "t"},
                    config={"configurable": {"run_id": "work", "thread_id": "decision-run:t:work"}},
                    execution_info=SimpleNamespace(thread_id="decision-run:t:work"),
                )
                tool_request = ToolCallRequest(
                    tool_call={"name": "bash_tool", "args": {"command": "echo hi"}, "id": "call"},
                    tool=None, state={"messages": [HumanMessage(content="执行命令")]}, runtime=tool_runtime,
                )
                result = await DecisionActionReviewMiddleware().awrap_tool_call(tool_request, execute)
            calls = await read_model_calls(factory, "u", "t")
            reviews = await read_reviews(factory, "u", "t")
            assert 'revision="0"' in seen_model[0].system_message.text
            assert len(calls) == 1 and calls[0].table_revision == 0
            assert calls[0].status == "completed"
            assert len(reviews) == 1 and reviews[0].table_revision == 1
            assert seen_review[0][0].table_revision == 1
            assert seen_review[0][1].rows[0].requirement == "必须加密"
            assert result.content == "done" and executions == 1
        finally:
            await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("history", [
    [HumanMessage(content="压缩摘要：旧决策表为空")],
    [HumanMessage(content="Run 恢复后继续工作")],
    [HumanMessage(content="上下文调整后的任务摘要")],
    [SystemMessage(id="decision-table", content='<current_decision_table revision="0">旧快照</current_decision_table>'),
     HumanMessage(content="从较早 checkpoint 继续")],
], ids=["compression", "run_recovery", "context_adjustment", "old_checkpoint"])
def test_all_context_rebuild_paths_bind_only_authoritative_table(history):
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
        await table_service.submit(
            user_id="u", thread_id="t", actor=Actor("u", "user"),
            run_id="edit", source="ui", idempotency_key="edit",
            base_revision=0, rows=(Row("r", "最新有效要求", "保留", 3),), reason="更新决策",
        )
        runtime = Runtime(
            context={"user_id": "u", "run_id": "restored", "thread_id": "t"},
            execution_info=ExecutionInfo("older-ck", "", "task", "decision-run:t:restored"),
        )
        request = ModelRequest(
            model=FakeListChatModel(responses=["ok"]), messages=history,
            system_message=SystemMessage(content='<current_decision_table revision="0">旧指令</current_decision_table>'),
            runtime=runtime,
        )
        seen = []
        async def handler(bound):
            seen.append(bound)
            return AIMessage(content="ok")
        try:
            with patch("caspian.agents.middlewares.decision_table_middleware.service", return_value=table_service), patch(
                "caspian.agents.middlewares.decision_table_middleware.get_session", factory
            ):
                await DecisionTableMiddleware().awrap_model_call(request, handler)
            sent = seen[0]
            assert sent.system_message.text.count('<current_decision_table ') == 1
            assert 'revision="1"' in sent.system_message.text
            assert "最新有效要求" in sent.system_message.text
            assert "旧指令" not in sent.system_message.text
            assert all("<current_decision_table " not in str(message.content) for message in sent.messages)
        finally:
            await engine.dispose()
    asyncio.run(scenario())
