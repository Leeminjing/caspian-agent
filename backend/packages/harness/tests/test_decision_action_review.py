"""本文件验证关键工具动作在复核结论前保持未执行，且同一绑定不会重复执行。

输入为隔离数据库、模拟工具和可控复核模型；输出为副作用次数与持久动作状态断言。
工作流让复核停在异步门上，确认下游未调用，再验证 keep 放行和参数变化失效。
示例：`pytest tests/test_decision_action_review.py`。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from caspian.agents.middlewares.decision_action_review_middleware import DecisionActionReviewMiddleware
from caspian.decision_governance.review_store import read_reviews, register_review, update_review
from caspian.decision_governance.review_protocol import ActionBinding
from caspian.decision_governance.domain import Row
from caspian.decision_governance.policy import Actor
from caspian.decision_governance.service import DecisionTableService
from caspian.persistence.base import Base


def test_critical_tool_waits_for_review_and_binding_deduplicates():
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
        runtime = SimpleNamespace(
            context={"user_id": "u", "run_id": "run", "thread_id": "t"},
            config={"configurable": {"run_id": "run", "thread_id": "t"}},
            execution_info=SimpleNamespace(thread_id="t"),
        )
        request = ToolCallRequest(
            tool_call={"name": "bash_tool", "args": {"command": "echo hi"}, "id": "call-1"},
            tool=None, state={"messages": []}, runtime=runtime,
        )
        entered = asyncio.Event()
        release = asyncio.Event()
        review_count = 0
        async def fake_review(_messages, binding, _args, _table, _context, _factory, **_kwargs):
            nonlocal review_count
            review_count += 1
            entered.set()
            await release.wait()
            return {
                "action_name": binding.action_name, "args_hash": binding.args_hash,
                "actor_id": binding.actor_id, "table_revision": binding.table_revision,
                "related_rows": [], "conflict": "none", "decision": "keep",
                "reason": "已检查当前空表与具体命令，无冲突",
            }
        executions = 0
        async def handler(value):
            nonlocal executions
            executions += 1
            return ToolMessage(content="done", tool_call_id=value.tool_call["id"], name="bash_tool")
        middleware = DecisionActionReviewMiddleware()
        try:
            with patch("caspian.agents.middlewares.decision_action_review_middleware.service", return_value=table_service), patch(
                "caspian.agents.middlewares.decision_action_review_middleware.get_session", factory
            ), patch("caspian.agents.middlewares.decision_action_review_middleware.review_action", fake_review):
                running = asyncio.create_task(middleware.awrap_tool_call(request, handler))
                await entered.wait()
                assert executions == 0
                assert (await read_reviews(factory, "u", "t"))[0].status == "pending_review"
                release.set()
                result = await running
                assert result.content == "done"
                assert executions == 1
                again = await middleware.awrap_tool_call(request, handler)
                assert again.content == "done" and executions == 1 and review_count == 1
                await table_service.submit(
                    user_id="u", thread_id="t", actor=Actor("u", "user"),
                    run_id="edit", source="ui", idempotency_key="edit",
                    base_revision=0, rows=(Row("r", "new decision", "保留", 3),), reason="table changed",
                )
                replay_after_table_change = await middleware.awrap_tool_call(request, handler)
                assert replay_after_table_change.content == "done"
                assert executions == 1 and review_count == 1
                changed = ToolCallRequest(
                    tool_call={"name": "bash_tool", "args": {"command": "echo changed"}, "id": "call-2"},
                    tool=None, state={"messages": []}, runtime=runtime,
                )
                await middleware.awrap_tool_call(changed, handler)
                assert executions == 2 and review_count == 2
                assert [item.status for item in await read_reviews(factory, "u", "t")] == ["executed", "executed"]
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_unexecuted_review_becomes_stale_when_table_revision_changes():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(
                sync, tables=[table for table in Base.metadata.tables.values() if table.name.startswith("decision_table_")]
            ))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            old = await register_review(
                factory, user_id="u", thread_id="t", run_id="run",
                tool_call_id="call", binding=ActionBinding.create("bash_tool", {"command": "echo"}, "lead", 0),
                content_hash="old", action_args={"command": "echo"},
            )
            await update_review(factory, old.review_id, status="awaiting_human")
            new = await register_review(
                factory, user_id="u", thread_id="t", run_id="run",
                tool_call_id="call", binding=ActionBinding.create("bash_tool", {"command": "echo"}, "lead", 1),
                content_hash="new", action_args={"command": "echo"},
            )
            assert new.review_id != old.review_id
            reviews = await read_reviews(factory, "u", "t")
            assert {item.table_revision: item.status for item in reviews} == {0: "stale", 1: "pending_review"}
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_table_change_during_review_blocks_tool_until_new_review():
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
        runtime = SimpleNamespace(
            context={"user_id": "u", "run_id": "run", "thread_id": "t"},
            config={"configurable": {"run_id": "run", "thread_id": "decision-run:t:run"}},
            execution_info=SimpleNamespace(thread_id="decision-run:t:run"),
        )
        request = ToolCallRequest(
            tool_call={"name": "bash_tool", "args": {"command": "echo hi"}, "id": "call"},
            tool=None, state={"messages": []}, runtime=runtime,
        )
        entered = asyncio.Event()
        release = asyncio.Event()
        review_count = 0
        executions = 0
        async def fake_review(_messages, binding, _args, _table, _context, _factory, **_kwargs):
            nonlocal review_count
            review_count += 1
            entered.set()
            if review_count == 1:
                await release.wait()
            return {
                "action_name": binding.action_name, "args_hash": binding.args_hash,
                "actor_id": binding.actor_id, "table_revision": binding.table_revision,
                "related_rows": [], "conflict": "none", "decision": "keep",
                "reason": "已核对当前表修订与具体命令参数，允许执行",
            }
        async def handler(value):
            nonlocal executions
            executions += 1
            return ToolMessage(content="done", tool_call_id=value.tool_call["id"], name="bash_tool")
        try:
            with patch("caspian.agents.middlewares.decision_action_review_middleware.service", return_value=table_service), patch(
                "caspian.agents.middlewares.decision_action_review_middleware.get_session", factory
            ), patch("caspian.agents.middlewares.decision_action_review_middleware.review_action", fake_review):
                first = asyncio.create_task(DecisionActionReviewMiddleware().awrap_tool_call(request, handler))
                await entered.wait()
                await table_service.submit(
                    user_id="u", thread_id="t", actor=Actor("u", "user"),
                    run_id="edit", source="ui", idempotency_key="edit",
                    base_revision=0, rows=(Row("r", "new decision", "保留", 3),), reason="changed during review",
                )
                release.set()
                blocked = await first
                assert blocked.status == "error"
                assert executions == 0
                result = await DecisionActionReviewMiddleware().awrap_tool_call(request, handler)
                assert result.content == "done"
                assert executions == 1 and review_count == 2
                assert [item.status for item in await read_reviews(factory, "u", "t")] == ["stale", "executed"]
        finally:
            release.set()
            await engine.dispose()
    asyncio.run(scenario())


def test_modify_and_cancel_leave_tool_unexecuted():
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
        runtime = SimpleNamespace(
            context={"user_id": "u", "run_id": "run", "thread_id": "t"},
            config={"configurable": {"run_id": "run"}},
            execution_info=SimpleNamespace(thread_id="decision-run:t:run"),
        )
        executions = 0
        async def handler(value):
            nonlocal executions
            executions += 1
            return ToolMessage(content="done", tool_call_id=value.tool_call["id"], name="bash_tool")
        async def fake_review(_messages, binding, args, _table, _context, _factory, **_kwargs):
            decision = "modify" if args["command"] == "change me" else "cancel"
            return {
                "action_name": binding.action_name, "args_hash": binding.args_hash,
                "actor_id": binding.actor_id, "table_revision": binding.table_revision,
                "related_rows": [], "conflict": "none", "decision": decision,
                "reason": "已核对当前命令并决定不执行原动作",
                "replacement_args": {"command": "echo revised"},
            }
        try:
            with patch("caspian.agents.middlewares.decision_action_review_middleware.service", return_value=table_service), patch(
                "caspian.agents.middlewares.decision_action_review_middleware.get_session", factory
            ), patch("caspian.agents.middlewares.decision_action_review_middleware.review_action", fake_review):
                for call_id, command in (("modify", "change me"), ("cancel", "cancel me")):
                    request = ToolCallRequest(
                        tool_call={"name": "bash_tool", "args": {"command": command}, "id": call_id},
                        tool=None, state={"messages": []}, runtime=runtime,
                    )
                    result = await DecisionActionReviewMiddleware().awrap_tool_call(request, handler)
                    assert result.status == "error" and result.tool_call_id == call_id
                assert executions == 0
                assert [item.status for item in await read_reviews(factory, "u", "t")] == ["modified", "cancelled"]
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_review_failure_keeps_critical_tool_paused_with_visible_reason():
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
        runtime = SimpleNamespace(
            context={"user_id": "u", "run_id": "run", "thread_id": "t"},
            config={"configurable": {"run_id": "run"}},
            execution_info=SimpleNamespace(thread_id="decision-run:t:run"),
        )
        request = ToolCallRequest(
            tool_call={"name": "bash_tool", "args": {"command": "echo"}, "id": "call"},
            tool=None, state={"messages": []}, runtime=runtime,
        )
        executions = 0
        async def broken_review(*_args, **_kwargs):
            raise RuntimeError("review model unavailable")
        async def handler(_request):
            nonlocal executions
            executions += 1
            return ToolMessage(content="done", tool_call_id="call", name="bash_tool")
        try:
            with patch("caspian.agents.middlewares.decision_action_review_middleware.service", return_value=table_service), patch(
                "caspian.agents.middlewares.decision_action_review_middleware.get_session", factory
            ), patch("caspian.agents.middlewares.decision_action_review_middleware.review_action", broken_review):
                result = await DecisionActionReviewMiddleware().awrap_tool_call(request, handler)
                assert result.status == "error" and "review model unavailable" in result.content
                assert executions == 0
                review = (await read_reviews(factory, "u", "t"))[0]
                assert review.status == "pending_review"
                assert "review model unavailable" in review.error
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_two_branches_cannot_claim_and_execute_same_review(tmp_path):
    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'reviews.sqlite'}")
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(
                sync, tables=[table for table in Base.metadata.tables.values() if table.name.startswith("decision_table_")]
            ))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async def no_findings(_candidate, _current):
            return ()
        table_service = DecisionTableService(factory, checker=no_findings)
        table = await table_service.current("u", "t", Actor("u", "user"))
        binding = ActionBinding.create("bash_tool", {"command": "echo"}, "lead", table.revision)
        review = await register_review(
            factory, user_id="u", thread_id="t", run_id="run", tool_call_id="call",
            binding=binding, content_hash=table.content_hash,
            action_args={"command": "echo"},
        )
        await update_review(factory, review.review_id, status="reviewed", conclusion={
            "action_name": binding.action_name, "args_hash": binding.args_hash,
            "actor_id": binding.actor_id, "table_revision": binding.table_revision,
            "related_rows": [], "conflict": "none", "decision": "keep",
            "reason": "已核对 echo 参数和当前空表，无冲突",
        })
        runtime = SimpleNamespace(
            context={"user_id": "u", "run_id": "run", "thread_id": "t"},
            config={"configurable": {"run_id": "run"}},
            execution_info=SimpleNamespace(thread_id="t"),
        )
        request = ToolCallRequest(
            tool_call={"name": "bash_tool", "args": {"command": "echo"}, "id": "call"},
            tool=None, state={"messages": []}, runtime=runtime,
        )
        prepared = 0
        both_prepared = asyncio.Event()
        executions = 0
        original_prepare = DecisionActionReviewMiddleware._prepare
        async def gated_prepare(self, value):
            nonlocal prepared
            result = await original_prepare(self, value)
            prepared += 1
            if prepared == 2:
                both_prepared.set()
            await both_prepared.wait()
            return result
        async def handler(_value):
            nonlocal executions
            executions += 1
            await asyncio.sleep(0.05)
            return ToolMessage(content="done", tool_call_id="call", name="bash_tool")
        try:
            with patch("caspian.agents.middlewares.decision_action_review_middleware.service", return_value=table_service), patch(
                "caspian.agents.middlewares.decision_action_review_middleware.get_session", factory
            ), patch.object(DecisionActionReviewMiddleware, "_prepare", gated_prepare):
                results = await asyncio.gather(*[
                    DecisionActionReviewMiddleware().awrap_tool_call(request, handler)
                    for _ in range(2)
                ])
            assert executions == 1
            assert sorted(result.status for result in results) == ["error", "success"]
            assert (await read_reviews(factory, "u", "t"))[0].status == "executed"
        finally:
            await engine.dispose()
    asyncio.run(scenario())
