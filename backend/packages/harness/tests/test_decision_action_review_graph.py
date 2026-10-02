"""本文件验证关键工具复核的 LangGraph 中断、并行认领、恢复和副作用顺序。

输入为模型发出的工具调用、待人工确认的复核结论和恢复选择；输出为未执行到恰好执行一次的工具记录。
工作流使用真实 create_agent 与 checkpoint，分别验证人工恢复、并行分支认领及从工具节点前回放后的结果配对。
示例：`pytest tests/test_decision_action_review_graph.py`。
"""

import asyncio
from unittest.mock import patch

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from caspian.agents.middlewares.decision_action_review_middleware import DecisionActionReviewMiddleware
from caspian.decision_governance.review_store import read_reviews
from caspian.decision_governance.service import DecisionTableService
from caspian.persistence.base import Base


class ToolCallingModel(BaseChatModel):
    @property
    def _llm_type(self):
        return "decision-action-review-test"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if any(isinstance(message, ToolMessage) for message in messages):
            response = AIMessage(content="done")
        else:
            response = AIMessage(content="", tool_calls=[{
                "name": "bash_tool", "args": {"command": "echo safe"},
                "id": "call-1", "type": "tool_call",
            }])
        return ChatResult(generations=[ChatGeneration(message=response)])

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self


def test_human_review_interrupts_before_handler_and_resumes_once():
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
        executions = 0
        @tool
        async def bash_tool(command: str) -> str:
            """Execute a test command."""
            nonlocal executions
            executions += 1
            return f"executed {command}"
        async def fake_review(_messages, binding, _args, _table, _context, _factory, **_kwargs):
            return {
                "action_name": binding.action_name, "args_hash": binding.args_hash,
                "actor_id": binding.actor_id, "table_revision": binding.table_revision,
                "related_rows": [], "conflict": "none", "decision": "ask_human",
                "reason": "命令需要用户在执行前逐项确认",
            }
        async def high_risk(*_args, **_kwargs):
            return {"risk": "high", "reason": "当前命令可能产生持久影响"}
        agent = create_agent(
            ToolCallingModel(), tools=[bash_tool],
            middleware=[DecisionActionReviewMiddleware()], checkpointer=InMemorySaver(),
        )
        config = {"configurable": {"thread_id": "decision-run:t:run", "run_id": "run"}}
        context = {"user_id": "u", "thread_id": "t", "run_id": "run"}
        try:
            with patch("caspian.agents.middlewares.decision_action_review_middleware.service", return_value=table_service), patch(
                "caspian.agents.middlewares.decision_action_review_middleware.get_session", factory
            ), patch("caspian.agents.middlewares.decision_action_review_middleware.review_action", fake_review), patch(
                "caspian.agents.middlewares.decision_action_review_middleware.assess_risk", high_risk):
                paused = await agent.ainvoke({"messages": [{"role": "user", "content": "run"}]}, config=config, context=context)
                assert executions == 0
                assert paused["__interrupt__"][0].value["type"] == "decision_action_review"
                assert (await read_reviews(factory, "u", "t"))[0].status == "awaiting_human"
                resumed = await agent.ainvoke(Command(resume={"decision": "approve"}), config=config, context=context)
                assert executions == 1
                assert any(isinstance(item, ToolMessage) and item.tool_call_id == "call-1" for item in resumed["messages"])
                assert (await read_reviews(factory, "u", "t"))[0].status == "executed"
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_real_graph_low_risk_tool_runs_without_full_review():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(
                sync, tables=[table for table in Base.metadata.tables.values() if table.name.startswith("decision_table_")]
            ))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        table_service = DecisionTableService(factory, checker=lambda *_: asyncio.sleep(0, result=()))
        executions = 0
        @tool
        async def bash_tool(command: str) -> str:
            """Run a test-only command."""
            nonlocal executions
            executions += 1
            return f"executed {command}"
        async def low_risk(*_args, **_kwargs):
            return {"risk": "low", "reason": "echo safe 是可撤销的普通查询"}
        async def unexpected_review(*_args, **_kwargs):
            raise AssertionError("普通动作不应进入完整决策表复核")
        agent = create_agent(ToolCallingModel(), tools=[bash_tool], middleware=[DecisionActionReviewMiddleware()], checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": "decision-run:t:run", "run_id": "run"}}
        context = {"user_id": "u", "thread_id": "t", "run_id": "run"}
        try:
            with patch("caspian.agents.middlewares.decision_action_review_middleware.service", return_value=table_service), patch(
                "caspian.agents.middlewares.decision_action_review_middleware.get_session", factory
            ), patch("caspian.agents.middlewares.decision_action_review_middleware.assess_risk", low_risk), patch(
                "caspian.agents.middlewares.decision_action_review_middleware.review_action", unexpected_review
            ):
                result = await agent.ainvoke({"messages": [{"role": "user", "content": "run"}]}, config=config, context=context)
                assert executions == 1 and "__interrupt__" not in result
                assert (await read_reviews(factory, "u", "t"))[0].status == "executed"
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_conflicting_decision_cannot_be_overridden_by_human_resume():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(
                sync, tables=[table for table in Base.metadata.tables.values() if table.name.startswith("decision_table_")]
            ))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        table_service = DecisionTableService(factory, checker=lambda *_: asyncio.sleep(0, result=()))
        executions = 0
        @tool
        async def bash_tool(command: str) -> str:
            """Run a test-only command."""
            nonlocal executions
            executions += 1
            return command
        async def high_risk(*_args, **_kwargs):
            return {"risk": "high", "reason": "echo safe 可能固化重要决策"}
        async def conflict(_messages, binding, _args, _table, _context, _factory, **_kwargs):
            return {
                "action_name": binding.action_name, "args_hash": binding.args_hash,
                "actor_id": binding.actor_id, "table_revision": binding.table_revision,
                "related_rows": [], "conflict": "conflict", "decision": "ask_human",
                "reason": "echo safe 与当前决策冲突，须先正式改表",
            }
        agent = create_agent(ToolCallingModel(), tools=[bash_tool], middleware=[DecisionActionReviewMiddleware()], checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": "decision-run:t:run", "run_id": "run"}}
        context = {"user_id": "u", "thread_id": "t", "run_id": "run"}
        try:
            with patch("caspian.agents.middlewares.decision_action_review_middleware.service", return_value=table_service), patch(
                "caspian.agents.middlewares.decision_action_review_middleware.get_session", factory
            ), patch("caspian.agents.middlewares.decision_action_review_middleware.assess_risk", high_risk), patch(
                "caspian.agents.middlewares.decision_action_review_middleware.review_action", conflict
            ):
                paused = await agent.ainvoke({"messages": [{"role": "user", "content": "run"}]}, config=config, context=context)
                assert paused["__interrupt__"][0].value["allowed_decisions"] == ["cancel", "retry_after_table_change"]
                resumed = await agent.ainvoke(Command(resume={"decision": "approve"}), config=config, context=context)
                assert executions == 0
                assert any(isinstance(item, ToolMessage) and "冲突" in item.content for item in resumed["messages"])
                assert (await read_reviews(factory, "u", "t"))[0].status == "awaiting_human"
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_real_graph_parallel_claim_and_checkpoint_replay_do_not_repeat_tool(tmp_path):
    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'graph-replay.sqlite'}")
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(
                sync, tables=[table for table in Base.metadata.tables.values() if table.name.startswith("decision_table_")]
            ))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        table_service = DecisionTableService(factory, checker=lambda *_: asyncio.sleep(0, result=()))
        entered = asyncio.Event()
        release = asyncio.Event()
        executions = 0
        @tool
        async def bash_tool(command: str) -> str:
            """Run a test-only command."""
            nonlocal executions
            executions += 1
            entered.set()
            await release.wait()
            return f"executed {command}"
        async def low_risk(*_args, **_kwargs):
            return {"risk": "low", "reason": "echo safe 没有持久副作用"}
        agent = create_agent(ToolCallingModel(), tools=[bash_tool], middleware=[DecisionActionReviewMiddleware()], checkpointer=InMemorySaver())
        context = {"user_id": "u", "thread_id": "t", "run_id": "run"}
        first_config = {"configurable": {"thread_id": "decision-run:t:run:first", "run_id": "run"}}
        second_config = {"configurable": {"thread_id": "decision-run:t:run:second", "run_id": "run"}}
        try:
            with patch("caspian.agents.middlewares.decision_action_review_middleware.service", return_value=table_service), patch(
                "caspian.agents.middlewares.decision_action_review_middleware.get_session", factory
            ), patch("caspian.agents.middlewares.decision_action_review_middleware.assess_risk", low_risk):
                first = asyncio.create_task(agent.ainvoke({"messages": [{"role": "user", "content": "run"}]}, config=first_config, context=context))
                await asyncio.wait_for(entered.wait(), timeout=5)
                second = await agent.ainvoke({"messages": [{"role": "user", "content": "run"}]}, config=second_config, context=context)
                assert any(isinstance(item, ToolMessage) and item.status == "error" for item in second["messages"])
                release.set()
                completed = await first
                assert any(isinstance(item, ToolMessage) and item.content == "executed echo safe" for item in completed["messages"])
                assert executions == 1
                before_tool = None
                async for state in agent.aget_state_history(first_config):
                    if "tools" in state.next:
                        before_tool = state
                        break
                assert before_tool is not None
                replayed = await agent.ainvoke(None, config=before_tool.config, context=context)
                assert any(isinstance(item, ToolMessage) and item.content == "executed echo safe" for item in replayed["messages"])
                assert executions == 1
                assert (await read_reviews(factory, "u", "t"))[0].status == "executed"
        finally:
            release.set()
            await engine.dispose()
    asyncio.run(scenario())
