"""本文件验证并行执行分支与共享投影的合流边界。

输入为同一会话的工作 Run A、改表 Run B 和各自 checkpoint；输出为 A 下一轮收到双方进展且执行 checkpoint 互不覆盖。
工作流暂停 A、提交 B、推进共享 checkpoint、恢复 A 并检查模型边界输入。
示例：`pytest tests/test_decision_run_branch.py`。
"""

import asyncio
from typing import Annotated, TypedDict

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from caspian.decision_governance.convergence_runtime import branch_config, missing_shared_messages, record_work_progress, synchronize
from caspian.decision_governance.domain import Row
from caspian.decision_governance.policy import Actor
from caspian.decision_governance.service import DecisionTableService
from caspian.decision_governance.shared_checkpoint import SharedCheckpointWriter
from caspian.persistence.base import Base


class State(TypedDict):
    messages: Annotated[list, add_messages]


def test_b_change_reaches_a_before_next_model_without_overwriting_a_checkpoint():
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
        saver = InMemorySaver()
        writer = SharedCheckpointWriter(saver)
        entered = asyncio.Event()
        release = asyncio.Event()
        seen = []

        async def work(state):
            entered.set()
            await release.wait()
            return {"messages": [HumanMessage(id="a-progress", content="A completed work step")]}

        async def boundary(state):
            await record_work_progress(factory, "u", "t", "run-a", state["messages"], writer)
            missing = await missing_shared_messages(factory, "u", "t", state["messages"], writer)
            seen.extend([*state["messages"], *missing])
            return {"messages": missing}

        builder = StateGraph(State)
        builder.add_node("work", work)
        builder.add_node("boundary", boundary)
        builder.add_edge(START, "work")
        builder.add_edge("work", "boundary")
        builder.add_edge("boundary", END)
        graph = builder.compile(checkpointer=saver)
        try:
            await service.current("u", "t", Actor("u", "user"))
            config_a = branch_config("t", "run-a")
            config_b = branch_config("t", "run-b")
            task_a = asyncio.create_task(graph.ainvoke({"messages": [HumanMessage(id="a-start", content="A started")]}, config_a))
            await entered.wait()
            older = await saver.aget_tuple(config_a)
            assert older is not None
            operation = await service.submit(
                user_id="u", thread_id="t", actor=Actor("u", "user"),
                run_id="run-b", source="ui", idempotency_key="b",
                base_revision=0, rows=(Row("r", "B decision", "保留", 3),), reason="B changed direction",
            )
            assert operation.status == "committed"
            await synchronize(factory, "u", "t", writer)
            release.set()
            result_a = await task_a
            assert any("B changed direction" in str(item.content) for item in seen)
            assert any(item.id == "a-progress" for item in result_a["messages"])
            assert await saver.aget_tuple(config_b) is None
            assert (await writer.read("u", "t")).values["table_revision"] == 1
            saved_a = await saver.aget_tuple(config_a)
            assert any("B changed direction" in str(item.content) for item in saved_a.checkpoint["channel_values"]["messages"])
            await graph.aupdate_state(config_a, {"messages": [HumanMessage(id="restored", content="restored progress")]})
            assert any(item.id == "restored" for item in (await graph.aget_state(config_a)).values["messages"])
            assert (await writer.read("u", "t")).values["table_revision"] == 1
            restored_config = {"configurable": {
                **config_a["configurable"],
                "checkpoint_id": older.config["configurable"]["checkpoint_id"],
            }}
            restored = await graph.ainvoke({"messages": [HumanMessage(id="later", content="resume earlier work")]}, restored_config)
            assert any("B changed direction" in str(item.content) for item in restored["messages"])
            assert (await writer.read("u", "t")).values["table_revision"] == 1
        finally:
            release.set()
            await engine.dispose()
    asyncio.run(scenario())
