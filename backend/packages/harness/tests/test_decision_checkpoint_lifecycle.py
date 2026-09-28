"""本文件验证决策表在真实 LangGraph checkpoint 压缩、恢复和旧分支中的模型输入。

输入为已提交的决策表和持久 checkpoint；输出为每轮模型实际接收的有效表修订。
工作流运行真实 Agent 图，以 RemoveMessage 改写状态，再重新实例化 Agent 并从旧 checkpoint 分支运行。
示例：`pytest tests/test_decision_checkpoint_lifecycle.py`。
"""

import asyncio
from typing import ClassVar
from unittest.mock import patch

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from caspian.agents.middlewares.decision_table_middleware import DecisionTableMiddleware
from caspian.decision_governance.domain import Row
from caspian.decision_governance.model_calls import read_model_calls
from caspian.decision_governance.policy import Actor
from caspian.decision_governance.service import DecisionTableService
from caspian.persistence.base import Base


class RecordingModel(BaseChatModel):
    seen: ClassVar[list[list]] = []

    @property
    def _llm_type(self):
        return "decision-checkpoint-test"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="已处理"))])


def test_compression_recovery_and_old_checkpoint_send_current_table():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(
                sync, tables=[table for table in Base.metadata.tables.values() if table.name.startswith("decision_table_")]
            ))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        service = DecisionTableService(factory, checker=lambda *_: asyncio.sleep(0, result=()))
        saver = InMemorySaver()
        RecordingModel.seen = []
        config = {"configurable": {"thread_id": "t", "run_id": "work"}}
        context = {"user_id": "u", "thread_id": "t", "run_id": "work"}
        try:
            with patch("caspian.agents.middlewares.decision_table_middleware.service", return_value=service), patch(
                "caspian.agents.middlewares.decision_table_middleware.get_session", factory
            ):
                agent = create_agent(RecordingModel(), tools=[], middleware=[DecisionTableMiddleware()], checkpointer=saver)
                await agent.ainvoke({"messages": [HumanMessage(content="最初任务", id="start")]}, config=config, context=context)
                assert "checkpoint_id" not in config["configurable"], config
                old_checkpoint = (await agent.aget_state(config)).config
                await service.submit(
                    user_id="u", thread_id="t", actor=Actor("u", "user"),
                    run_id="edit", source="ui", idempotency_key="edit", base_revision=0,
                    rows=(Row("r", "始终加密", "保留", 3),), reason="新增决策",
                )
                compressed = await agent.ainvoke({"messages": [
                    RemoveMessage(id=REMOVE_ALL_MESSAGES),
                    HumanMessage(content="压缩摘要：继续工作", id="summary"),
                    HumanMessage(content="压缩后继续"),
                ]}, config=old_checkpoint, context=context)
                recovered_agent = create_agent(RecordingModel(), tools=[], middleware=[DecisionTableMiddleware()], checkpointer=saver)
                compressed_checkpoint = (await agent.aget_state(config)).config
                recovered = await recovered_agent.ainvoke({"messages": [HumanMessage(content="恢复后继续")]}, config=compressed_checkpoint, context=context)
                await recovered_agent.ainvoke(
                    {"messages": [HumanMessage(content="从旧点继续")]}, config=old_checkpoint, context=context,
                )
            calls = await read_model_calls(factory, "u", "t")
            assert [call.table_revision for call in calls] == [0, 1, 1, 1], (
                len(RecordingModel.seen), [str(turn[-1].content) for turn in RecordingModel.seen],
                [str(message.content) for message in compressed["messages"]],
                [str(message.content) for message in recovered["messages"]],
            )
            sent = ["\n".join(str(message.content) for message in turn) for turn in RecordingModel.seen]
            assert "压缩摘要" in sent[1] and "最初任务" not in sent[1]
            assert all(turn.count("<current_decision_table ") == 1 for turn in sent)
            assert all("始终加密" in turn for turn in sent[1:])
        finally:
            await engine.dispose()
    asyncio.run(scenario())
