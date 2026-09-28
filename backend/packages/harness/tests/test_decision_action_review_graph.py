"""本文件验证关键工具复核的 LangGraph 中断、恢复和副作用顺序。

输入为模型发出的工具调用、待人工确认的复核结论和恢复选择；输出为未执行到恰好执行一次的工具记录。
工作流使用真实 create_agent 与 checkpoint，第一次停在工具处理器之前，恢复后配对工具结果。
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
        agent = create_agent(
            ToolCallingModel(), tools=[bash_tool],
            middleware=[DecisionActionReviewMiddleware()], checkpointer=InMemorySaver(),
        )
        config = {"configurable": {"thread_id": "decision-run:t:run", "run_id": "run"}}
        context = {"user_id": "u", "thread_id": "t", "run_id": "run"}
        try:
            with patch("caspian.agents.middlewares.decision_action_review_middleware.service", return_value=table_service), patch(
                "caspian.agents.middlewares.decision_action_review_middleware.get_session", factory
            ), patch("caspian.agents.middlewares.decision_action_review_middleware.review_action", fake_review):
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
