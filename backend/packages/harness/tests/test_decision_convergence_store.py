"""本文件验证并行 Run 的规范事件进入共享投影且旧分支不会覆盖另一方进展。

输入为 A 的工作消息、B 的改表操作和后续 A checkpoint 消息；输出为合并后的共享历史与单调游标。
工作流分别登记分支、投递 outbox 和推进共享投影，再重放旧消息确认幂等。
示例：`pytest tests/test_decision_convergence_store.py`。
"""

import asyncio

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from caspian.decision_governance.convergence_store import append_work_messages, project_thread, shared_messages
from caspian.decision_governance.convergence_runtime import hash_messages, missing_shared_messages
from caspian.decision_governance.domain import Row
from caspian.decision_governance.events import deliver_pending
from caspian.decision_governance.policy import Actor
from caspian.decision_governance.service import DecisionTableService
from caspian.decision_governance.shared_checkpoint import SharedCheckpointWriter
from caspian.persistence.base import Base


def test_a_work_and_b_operation_survive_later_a_save():
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
        writer = SharedCheckpointWriter(InMemorySaver())
        async def checkpoint(messages, revision, cursors):
            return await writer.write("u", "t", messages, revision, cursors)
        try:
            await service.current("u", "t", Actor("u", "user"))
            a_messages = [HumanMessage(id="a1", content="A start"), AIMessage(id="a2", content="A progress")]
            await append_work_messages(factory, "u", "t", "run-a", a_messages)
            await project_thread(factory, "u", "t", checkpoint)
            operation = await service.submit(
                user_id="u", thread_id="t", actor=Actor("u", "user"),
                run_id="run-b", source="ui", idempotency_key="b",
                base_revision=0, rows=(Row("r", "B decision", "保留", 3),), reason="B changed direction",
            )
            assert operation.status == "committed"
            await deliver_pending(factory)
            await project_thread(factory, "u", "t", checkpoint)
            shared = await shared_messages(factory, "u", "t")
            assert [message.id for message in shared[:2]] == ["a1", "a2"]
            assert any("B changed direction" in str(message.content) for message in shared)
            checkpoint_state = await writer.read("u", "t")
            assert checkpoint_state.values["table_revision"] == 1
            assert [message.id for message in checkpoint_state.values["messages"]] == [message.id for message in shared]
            assert checkpoint_state.values["run_cursors"]["run-a"] > 0
            assert await append_work_messages(factory, "u", "t", "run-a", a_messages) == 0
            await project_thread(factory, "u", "t")
            assert [message.id for message in await shared_messages(factory, "u", "t")] == [message.id for message in shared]
            compressed = [HumanMessage(id="summary", content="A 的旧历史已压缩")]
            assert await missing_shared_messages(factory, "u", "t", compressed, writer, hash_messages(shared)) == []
            await service.submit(
                user_id="u", thread_id="t", actor=Actor("u", "user"),
                run_id="run-b2", source="ui", idempotency_key="b2",
                base_revision=1,
                rows=(Row("r", "B decision", "保留", 3), Row("r2", "newer decision", "保留", 2)),
                reason="post-compression change",
            )
            new_messages = await missing_shared_messages(factory, "u", "t", compressed, writer, hash_messages(shared))
            assert new_messages and all(item.id not in {"a1", "a2"} for item in new_messages)
            assert any("post-compression change" in str(item.content) for item in new_messages)
        finally:
            await engine.dispose()
    asyncio.run(scenario())
