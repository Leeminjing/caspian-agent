"""本文件验证两阶段动作闸门对普通动作、高影响动作、超时升级及结果重放的执行边界。

输入为隔离 SQLite 表、可控风险与复核结论及模拟工具；输出为持久审计与真实副作用次数断言。
工作流在相同工具名下改变实际参数，并让复核超时或结果存储失败，证明动作先判断后执行且恢复不重复。
示例：`pytest tests/test_action_risk_gate.py`。
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from caspian.agents.middlewares.decision_action_review_middleware import DecisionActionReviewMiddleware
from caspian.config.decision_review_config import DecisionReviewConfig
from caspian.decision_governance.action_risk import context_fingerprint, impact_hint, parse_risk
from caspian.decision_governance.review_protocol import ActionBinding
from caspian.decision_governance.domain import Row
from caspian.decision_governance.review_store import claim_execution, read_reviews, register_review, transition_review, update_review
from caspian.decision_governance.model_calls import count_model_calls, read_model_calls
from caspian.decision_governance.policy import Actor
from caspian.decision_governance.risk_model import assess_risk
from caspian.decision_governance.review_model import review_action
from caspian.decision_governance.service import DecisionTableService
from caspian.persistence.base import Base


def test_risk_protocol_requires_actual_parameters_and_concrete_low_reason():
    args = {"command": "pytest"}
    binding = ActionBinding.create("bash_tool", args, "lead", 0, "context")
    valid = {
        "action_name": "bash_tool", "args_hash": binding.args_hash,
        "context_hash": "context", "examined_args": args,
        "risk": "low", "reason": "pytest 只运行当前测试，没有持久副作用",
    }
    assert parse_risk(json.dumps(valid), binding, args)["risk"] == "low"
    for changed in (
        {**valid, "examined_args": {"command": "alembic upgrade head"}},
        {**valid, "context_hash": "older"},
        {**valid, "reason": "很安全的"},
    ):
        with pytest.raises(ValueError):
            parse_risk(json.dumps(changed), binding, args)
    with pytest.raises(ValueError):
        parse_risk("not json", binding, args)
    high_args = {"command": "alembic upgrade head"}
    high_binding = ActionBinding.create("bash_tool", high_args, "lead", 0, "context")
    apparent_low = {**valid, "args_hash": high_binding.args_hash, "examined_args": high_args, "reason": "alembic upgrade head 看似安全"}
    assert parse_risk(json.dumps(apparent_low), high_binding, high_args)["risk"] == "high"
    assert impact_hint("read_file_tool", {"path": "db/migrations/upgrade.py"}) is None
    assert impact_hint("bash_tool", {"command": "cat db/migrations/upgrade.py"}) is None
    for command in ("pytest && docker run mysql", "pytest; docker run mysql", "pytest & docker run mysql", "pytest | docker run mysql"):
        chained_args = {"command": command}
        chained_binding = ActionBinding.create("bash_tool", chained_args, "lead", 0, "context")
        chained_low = {**valid, "args_hash": chained_binding.args_hash, "examined_args": chained_args, "reason": f"{command} 看似只运行测试"}
        assert parse_risk(json.dumps(chained_low), chained_binding, chained_args)["risk"] == "high"
    assert impact_hint("write_file_tool", {"path": "src/business.py", "content": "return 1"}) is None
    assert impact_hint("write_file_tool", {"path": "pyproject.toml", "content": "dependency"}) is not None
    unknown_args = {"payload": "initialize cloud resource"}
    unknown_binding = ActionBinding.create("unfamiliar_tool", unknown_args, "lead", 0, "context")
    unknown = {**valid, "action_name": "unfamiliar_tool", "args_hash": unknown_binding.args_hash, "examined_args": unknown_args, "risk": "uncertain", "reason": "initialize cloud resource 的后果无法确认"}
    assert parse_risk(json.dumps(unknown), unknown_binding, unknown_args)["risk"] == "uncertain"
    script_args = {"command": "./unknown-script.sh"}
    script_binding = ActionBinding.create("bash_tool", script_args, "lead", 0, "context")
    script = {**valid, "args_hash": script_binding.args_hash, "examined_args": script_args, "risk": "uncertain", "reason": "./unknown-script.sh 的副作用不明确"}
    assert parse_risk(json.dumps(script), script_binding, script_args)["risk"] == "uncertain"
    assert context_fingerprint([{"content": "work"}, {"content": "[动作前复核] 处理中"}], {}) == context_fingerprint([{"content": "work"}], {})


def test_legacy_tool_name_configuration_is_rejected():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        DecisionReviewConfig(critical_tools=["bash_tool"])
    with pytest.raises(ValidationError):
        DecisionReviewConfig(review_timeout_seconds=0)


def test_low_and_high_actions_share_tool_name_but_only_high_waits_for_full_review():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(
                sync, tables=[table for table in Base.metadata.tables.values() if table.name.startswith("decision_table_")]
            ))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        table_service = DecisionTableService(factory, checker=lambda *_: asyncio.sleep(0, result=()))
        runtime = SimpleNamespace(
            context={"user_id": "u", "run_id": "r", "thread_id": "t"},
            config={"configurable": {"run_id": "r"}},
            execution_info=SimpleNamespace(thread_id="decision-run:t:r"),
        )
        calls = 0
        review_started = asyncio.Event()
        release = asyncio.Event()
        async def handler(request):
            nonlocal calls
            calls += 1
            return ToolMessage(content="done", tool_call_id=request.tool_call["id"], name="bash_tool")
        async def risk(_messages, binding, args, *_rest, **_kwargs):
            if args["command"] == "echo broken":
                raise RuntimeError("risk model offline")
            return {"risk": "low" if args["command"] == "pytest" else "high", "reason": args["command"]}
        async def review(_messages, binding, args, *_rest, **_kwargs):
            review_started.set()
            await release.wait()
            return {
                "action_name": binding.action_name, "args_hash": binding.args_hash,
                "actor_id": binding.actor_id, "table_revision": binding.table_revision,
                "related_rows": [], "conflict": "none", "decision": "keep",
                "reason": f"已检查 {args['command']} 与当前决策表，无冲突",
            }
        def request(command, call_id):
            return ToolCallRequest(
                tool_call={"name": "bash_tool", "args": {"command": command}, "id": call_id},
                tool=None, state={"messages": []}, runtime=runtime,
            )
        try:
            with patch("caspian.agents.middlewares.decision_action_review_middleware.service", return_value=table_service), patch(
                "caspian.agents.middlewares.decision_action_review_middleware.get_session", factory
            ), patch("caspian.agents.middlewares.decision_action_review_middleware.assess_risk", risk), patch(
                "caspian.agents.middlewares.decision_action_review_middleware.review_action", review
            ):
                gate = DecisionActionReviewMiddleware()
                low = await gate.awrap_tool_call(request("pytest", "low"), handler)
                assert low.content == "done" and calls == 1 and not review_started.is_set()
                pending = asyncio.create_task(gate.awrap_tool_call(request("alembic upgrade head", "high"), handler))
                await review_started.wait()
                assert calls == 1
                release.set()
                high = await pending
                assert high.content == "done" and calls == 2
                replay = await gate.awrap_tool_call(request("alembic upgrade head", "high"), handler)
                assert replay.content == "done" and calls == 2
                uncertain = await gate.awrap_tool_call(request("echo broken", "uncertain"), handler)
                assert uncertain.content == "done" and calls == 3
                blocked = await gate.awrap_tool_call(request("rm -rf /", "blocked"), handler)
                assert blocked.status == "error" and calls == 3
                await table_service.submit(
                    user_id="u", thread_id="t", actor=Actor("u", "user"), run_id="edit",
                    source="ui", idempotency_key="guard", base_revision=0,
                    rows=(Row("r", "禁止运行 pytest", "保留", 3, ({"kind": "forbid", "target": "shell", "operator": "contains", "pattern": "pytest"},)),),
                    reason="验证低风险动作仍受硬层约束",
                )
                guarded = await gate.awrap_tool_call(request("pytest", "guarded"), handler)
                assert guarded.status == "error" and calls == 3
                records = await read_reviews(factory, "u", "t")
                assert [(item.risk_outcome, item.status) for item in records] == [("low", "executed"), ("high", "executed"), ("uncertain", "executed"), ("high", "paused"), ("low", "paused")]
        finally:
            release.set()
            await engine.dispose()
    asyncio.run(scenario())


def test_lightweight_model_receives_current_table_without_advancing_periodic_counter():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(
                sync, tables=[table for table in Base.metadata.tables.values() if table.name.startswith("decision_table_")]
            ))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        table = await DecisionTableService(factory, checker=lambda *_: asyncio.sleep(0, result=())).current("u", "t", Actor("lead", "agent"))
        args = {"command": "pytest"}
        binding = ActionBinding.create("bash_tool", args, "lead", table.revision, "context")
        seen = []
        class CapturingModel:
            async def ainvoke(self, messages):
                seen.extend(messages)
                return AIMessage(content=json.dumps({
                    "action_name": binding.action_name, "args_hash": binding.args_hash,
                    "context_hash": binding.context_hash, "examined_args": args,
                    "risk": "low", "reason": "pytest 只运行测试，无持续影响",
                }))
        try:
            with patch("caspian.decision_governance.risk_model.create_chat_model", return_value=CapturingModel()):
                result = await assess_risk([], binding, args, table, {}, factory, user_id="u", thread_id="t", run_id="r")
            assert result["risk"] == "low"
            assert sum(str(message.content).count("<current_decision_table ") for message in seen) == 1
            calls = await read_model_calls(factory, "u", "t")
            assert len(calls) == 1 and calls[0].actor_id == "action_risk" and calls[0].table_revision == table.revision
            assert await count_model_calls(factory, "u", "t", "r", "lead") == 0
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_lightweight_model_timeout_is_a_failed_audited_call():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(
                sync, tables=[table for table in Base.metadata.tables.values() if table.name.startswith("decision_table_")]
            ))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        table = await DecisionTableService(factory, checker=lambda *_: asyncio.sleep(0, result=())).current("u", "t", Actor("lead", "agent"))
        args = {"command": "pytest"}
        binding = ActionBinding.create("bash_tool", args, "lead", table.revision, "context")
        class SlowModel:
            async def ainvoke(self, _messages):
                await asyncio.sleep(2)
        context = {"app_config": SimpleNamespace(decision_review=SimpleNamespace(risk_timeout_seconds=1))}
        try:
            with patch("caspian.decision_governance.risk_model.create_chat_model", return_value=SlowModel()):
                with pytest.raises(TimeoutError):
                    await assess_risk([], binding, args, table, context, factory, user_id="u", thread_id="t", run_id="r")
            calls = await read_model_calls(factory, "u", "t")
            assert len(calls) == 1 and calls[0].status == "failed"
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_full_review_timeout_is_a_failed_audited_call():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(
                sync, tables=[table for table in Base.metadata.tables.values() if table.name.startswith("decision_table_")]
            ))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        table = await DecisionTableService(factory, checker=lambda *_: asyncio.sleep(0, result=())).current("u", "t", Actor("lead", "agent"))
        args = {"command": "docker run mysql"}
        binding = ActionBinding.create("bash_tool", args, "lead", table.revision, "context")
        class SlowModel:
            async def ainvoke(self, _messages):
                await asyncio.sleep(2)
        context = {"app_config": SimpleNamespace(decision_review=SimpleNamespace(review_timeout_seconds=1))}
        try:
            with patch("caspian.decision_governance.review_model.create_chat_model", return_value=SlowModel()):
                with pytest.raises(TimeoutError):
                    await review_action([], binding, args, table, context, factory, user_id="u", thread_id="t", run_id="r")
            calls = await read_model_calls(factory, "u", "t")
            assert len(calls) == 1 and calls[0].status == "failed"
            assert calls[0].actor_id == "action_review"
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_result_store_failure_never_claims_tool_was_unexecuted():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(
                sync, tables=[table for table in Base.metadata.tables.values() if table.name.startswith("decision_table_")]
            ))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        table_service = DecisionTableService(factory, checker=lambda *_: asyncio.sleep(0, result=()))
        runtime = SimpleNamespace(
            context={"user_id": "u", "run_id": "r", "thread_id": "t"},
            config={"configurable": {"run_id": "r"}},
            execution_info=SimpleNamespace(thread_id="decision-run:t:r"),
        )
        request = ToolCallRequest(
            tool_call={"name": "bash_tool", "args": {"command": "pytest"}, "id": "c"},
            tool=None, state={"messages": []}, runtime=runtime,
        )
        executions = 0
        async def low_risk(*_args, **_kwargs):
            return {"risk": "low", "reason": "pytest 只运行测试"}
        async def handler(_request):
            nonlocal executions
            executions += 1
            return ToolMessage(content="done", tool_call_id="c", name="bash_tool")
        async def failed_update(*_args, **_kwargs):
            raise RuntimeError("database unavailable")
        try:
            with patch("caspian.agents.middlewares.decision_action_review_middleware.service", return_value=table_service), patch(
                "caspian.agents.middlewares.decision_action_review_middleware.get_session", factory
            ), patch("caspian.agents.middlewares.decision_action_review_middleware.assess_risk", low_risk), patch(
                "caspian.agents.middlewares.decision_action_review_middleware.update_review", failed_update
            ):
                first = await DecisionActionReviewMiddleware().awrap_tool_call(request, handler)
                replay = await DecisionActionReviewMiddleware().awrap_tool_call(request, handler)
            assert first.status == "error" and "结果未确认" in first.content
            assert "工具未执行" not in first.content and "已开始执行" in first.content
            assert replay.status == "error" and executions == 1
            assert (await read_reviews(factory, "u", "t"))[0].status == "executing"
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_claimed_action_is_never_dispatched_after_reentry():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(
                sync, tables=[table for table in Base.metadata.tables.values() if table.name.startswith("decision_table_")]
            ))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        table_service = DecisionTableService(factory, checker=lambda *_: asyncio.sleep(0, result=()))
        table = await table_service.current("u", "t", Actor("lead", "agent"))
        context = {"user_id": "u", "run_id": "r", "thread_id": "t"}
        binding = ActionBinding.create("bash_tool", {"command": "pytest"}, "lead", table.revision, context_fingerprint([], context))
        review = await register_review(
            factory, user_id="u", thread_id="t", run_id="r", tool_call_id="c",
            binding=binding, content_hash=table.content_hash, action_args={"command": "pytest"},
        )
        with pytest.raises(ValueError):
            await claim_execution(factory, review.review_id, "risk_pending")
        assert await transition_review(factory, review.review_id, expected_status="risk_pending", status="low_ready", risk_outcome="low", risk_reason="pytest 只运行测试")
        assert await transition_review(factory, review.review_id, expected_status="low_ready", status="executing")
        with pytest.raises(ValueError):
            await update_review(factory, review.review_id, status="low_ready")
        runtime = SimpleNamespace(context=context, config={"configurable": {"run_id": "r"}}, execution_info=SimpleNamespace(thread_id="decision-run:t:r"))
        request = ToolCallRequest(tool_call={"name": "bash_tool", "args": {"command": "pytest"}, "id": "c"}, tool=None, state={"messages": []}, runtime=runtime)
        dispatched = 0
        async def handler(_request):
            nonlocal dispatched
            dispatched += 1
            return ToolMessage(content="done", tool_call_id="c", name="bash_tool")
        try:
            with patch("caspian.agents.middlewares.decision_action_review_middleware.service", return_value=table_service), patch(
                "caspian.agents.middlewares.decision_action_review_middleware.get_session", factory
            ):
                first = await DecisionActionReviewMiddleware().awrap_tool_call(request, handler)
                second = await DecisionActionReviewMiddleware().awrap_tool_call(request, handler)
            assert first.status == second.status == "error" and dispatched == 0
            assert "未再次执行" in first.content
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_non_reconstructible_command_result_is_not_replayed():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(
                sync, tables=[table for table in Base.metadata.tables.values() if table.name.startswith("decision_table_")]
            ))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        table_service = DecisionTableService(factory, checker=lambda *_: asyncio.sleep(0, result=()))
        runtime = SimpleNamespace(
            context={"user_id": "u", "run_id": "r", "thread_id": "t"},
            config={"configurable": {"run_id": "r"}},
            execution_info=SimpleNamespace(thread_id="decision-run:t:r"),
        )
        request = ToolCallRequest(
            tool_call={"name": "task_tool", "args": {"description": "test task"}, "id": "c"},
            tool=None, state={"messages": []}, runtime=runtime,
        )
        executions = 0
        async def handler(_request):
            nonlocal executions
            executions += 1
            return Command(update={"messages": []})
        async def low_risk(*_args, **_kwargs):
            return {"risk": "low", "reason": "test task 只返回控制流"}
        try:
            with patch("caspian.agents.middlewares.decision_action_review_middleware.service", return_value=table_service), patch(
                "caspian.agents.middlewares.decision_action_review_middleware.get_session", factory
            ), patch("caspian.agents.middlewares.decision_action_review_middleware.assess_risk", low_risk):
                first = await DecisionActionReviewMiddleware().awrap_tool_call(request, handler)
                replay = await DecisionActionReviewMiddleware().awrap_tool_call(request, handler)
            assert isinstance(first, Command)
            assert replay.status == "error" and "结果未确认" in replay.content and executions == 1
            assert (await read_reviews(factory, "u", "t"))[0].status == "indeterminate"
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_modified_mysql_action_never_executes_and_postgres_is_rechecked():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(
                sync, tables=[table for table in Base.metadata.tables.values() if table.name.startswith("decision_table_")]
            ))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        table_service = DecisionTableService(factory, checker=lambda *_: asyncio.sleep(0, result=()))
        runtime = SimpleNamespace(
            context={"user_id": "u", "run_id": "r", "thread_id": "t"},
            config={"configurable": {"run_id": "r"}},
            execution_info=SimpleNamespace(thread_id="decision-run:t:r"),
        )
        risk_calls = []
        executed = []
        async def risk(_messages, _binding, args, *_rest, **_kwargs):
            risk_calls.append(args["command"])
            return {"risk": "high", "reason": args["command"]}
        async def review(_messages, binding, args, *_rest, **_kwargs):
            mysql = "mysql" in args["command"]
            return {
                "action_name": binding.action_name, "args_hash": binding.args_hash,
                "actor_id": binding.actor_id, "table_revision": binding.table_revision,
                "decision": "modify" if mysql else "keep",
                "replacement_args": {"command": "docker run postgres"} if mysql else None,
                "related_rows": [], "conflict": "none",
                "reason": f"已核对 {args['command']} 与当前表",
            }
        async def handler(request):
            executed.append(request.tool_call["args"]["command"])
            return ToolMessage(content="done", tool_call_id=request.tool_call["id"], name="bash_tool")
        def request(command, call_id):
            return ToolCallRequest(tool_call={"name": "bash_tool", "args": {"command": command}, "id": call_id}, tool=None, state={"messages": []}, runtime=runtime)
        try:
            with patch("caspian.agents.middlewares.decision_action_review_middleware.service", return_value=table_service), patch(
                "caspian.agents.middlewares.decision_action_review_middleware.get_session", factory
            ), patch("caspian.agents.middlewares.decision_action_review_middleware.assess_risk", risk), patch(
                "caspian.agents.middlewares.decision_action_review_middleware.review_action", review
            ):
                original = await DecisionActionReviewMiddleware().awrap_tool_call(request("docker run mysql", "mysql"), handler)
                assert original.status == "error" and executed == []
                revised = await DecisionActionReviewMiddleware().awrap_tool_call(request("docker run postgres", "postgres"), handler)
                assert revised.content == "done" and executed == ["docker run postgres"]
                assert risk_calls == ["docker run mysql", "docker run postgres"]
                assert [item.status for item in await read_reviews(factory, "u", "t")] == ["modified", "executed"]
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_context_change_during_review_invalidates_old_conclusion():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(
                sync, tables=[table for table in Base.metadata.tables.values() if table.name.startswith("decision_table_")]
            ))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        table_service = DecisionTableService(factory, checker=lambda *_: asyncio.sleep(0, result=()))
        runtime = SimpleNamespace(
            context={"user_id": "u", "run_id": "r", "thread_id": "t"},
            config={"configurable": {"run_id": "r"}},
            execution_info=SimpleNamespace(thread_id="decision-run:t:r"),
        )
        request = ToolCallRequest(
            tool_call={"name": "bash_tool", "args": {"command": "docker run postgres"}, "id": "c"},
            tool=None, state={"messages": [HumanMessage(content="use database")]}, runtime=runtime,
        )
        entered = asyncio.Event()
        release = asyncio.Event()
        reviews = 0
        executions = 0
        async def high_risk(*_args, **_kwargs):
            return {"risk": "high", "reason": "docker run postgres 创建持久资源"}
        async def review(_messages, binding, _args, *_rest, **_kwargs):
            nonlocal reviews
            reviews += 1
            if reviews == 1:
                entered.set()
                await release.wait()
            return {
                "action_name": binding.action_name, "args_hash": binding.args_hash,
                "actor_id": binding.actor_id, "table_revision": binding.table_revision,
                "related_rows": [], "conflict": "none", "decision": "keep",
                "reason": "docker run postgres 与当前表一致",
            }
        async def handler(_request):
            nonlocal executions
            executions += 1
            return ToolMessage(content="done", tool_call_id="c", name="bash_tool")
        try:
            with patch("caspian.agents.middlewares.decision_action_review_middleware.service", return_value=table_service), patch(
                "caspian.agents.middlewares.decision_action_review_middleware.get_session", factory
            ), patch("caspian.agents.middlewares.decision_action_review_middleware.assess_risk", high_risk), patch(
                "caspian.agents.middlewares.decision_action_review_middleware.review_action", review
            ):
                pending = asyncio.create_task(DecisionActionReviewMiddleware().awrap_tool_call(request, handler))
                await entered.wait()
                request.state["messages"].append(HumanMessage(content="new deployment constraint"))
                release.set()
                stale = await pending
                assert stale.status == "error" and executions == 0
                fresh = await DecisionActionReviewMiddleware().awrap_tool_call(request, handler)
                assert fresh.content == "done" and executions == 1 and reviews == 2
                assert [item.status for item in await read_reviews(factory, "u", "t")] == ["stale", "executed"]
        finally:
            release.set()
            await engine.dispose()
    asyncio.run(scenario())
