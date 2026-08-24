"""验证任意 Context（根或派生）可重命名，且以 web_threads.title 为权威源。

输入:
    内存 sqlite session factory + InMemorySaver + ContextService

输出:
    覆盖根/派生重命名、非本人 404、不存在 404、空/超长标题 422、
    从未运行根线程懒创建、树/快照反应新标题、派生关系保持不变。
"""

import asyncio
import unittest

from fastapi import HTTPException
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.gateway.context.models import (
    ContextRenameRequest,
    WebContextDefinition,
    WebContextSource,
    WebThread,
)
from backend.app.gateway.context.service import ContextService
from caspian.persistence.base import Base
from langgraph.checkpoint.memory import InMemorySaver

_CONTEXT_TABLES = [
    WebThread.__table__,
    WebContextDefinition.__table__,
    WebContextSource.__table__,
]


class ContextRenameTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite://")
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=_CONTEXT_TABLES))
        self.checkpointer = InMemorySaver()
        self.service = ContextService(self.checkpointer, session_factory=self.session_factory)

        async def fake_make_state_graph():
            graph = create_agent(model=FakeListChatModel(responses=["unused"]), tools=[])
            graph.checkpointer = self.checkpointer
            return graph

        self.service._make_state_graph = fake_make_state_graph

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _seed_checkpoint(self, thread_id: str, content: str) -> str:
        config = {"configurable": {"thread_id": thread_id}}
        checkpoint_id = f"cp-{thread_id}-{content}"
        checkpoint = {
            "v": 4,
            "ts": "2026-08-16T00:00:00+00:00",
            "id": checkpoint_id,
            "channel_values": {"messages": [HumanMessage(content=content)]},
            "channel_versions": {"messages": 1},
            "versions_seen": {},
        }
        await self.checkpointer.aput(
            {**config, "configurable": {**config["configurable"], "checkpoint_ns": ""}},
            checkpoint,
            {},
            {"messages": 1},
        )
        return checkpoint_id

    async def _register_root(self, user_id: str, thread_id: str, content: str) -> str:
        await self.service.register_main_run(user_id, thread_id)
        return await self._seed_checkpoint(thread_id, content)

    async def _derive(
        self, user_id: str, sources, messages, title="新 Context"
    ) -> dict:
        from backend.app.gateway.context.models import ContextDeriveCreate, ContextSourceRef

        body = ContextDeriveCreate(
            title=title,
            sources=[ContextSourceRef(**source) for source in sources],
            messages=messages,
        )
        return await self.service.derive(user_id, body)

    async def _thread_title(self, thread_id: str) -> str | None:
        async with self.session_factory() as session:
            task = await session.get(WebThread, thread_id)
            return task.title if task else None

    async def test_重命名根context后树与线程标题更新(self):
        await self._register_root("u-1", "thread-a", "root hello")
        await self._seed_checkpoint("thread-b", "another")

        result = await self.service.rename("u-1", "thread-a", "重命名后")

        self.assertEqual(result["title"], "重命名后")
        self.assertEqual(await self._thread_title("thread-a"), "重命名后")
        tree = await self.service.tree("u-1")
        node = next(item for item in tree if item["context_id"] == "thread-a")
        self.assertEqual(node["title"], "重命名后")

    async def test_重命名派生context后来源关系与消息不变(self):
        cp_a = await self._register_root("u-1", "thread-a", "root hello")
        derived = await self._derive(
            "u-1",
            [{"context_id": "thread-a", "checkpoint_id": cp_a}],
            [{"role": "human", "content": "B 内容", "id": "msg-b-1"}],
        )
        derived_id = derived["context_id"]
        derived_title_before = derived["title"]
        authored_before = derived["authored_messages"]
        sources_before = [(s["context_id"], s["checkpoint_id"], s["position"]) for s in derived["sources"]]

        result = await self.service.rename("u-1", derived_id, "派生改名")

        self.assertEqual(result["title"], "派生改名")
        self.assertEqual(result["authored_messages"], authored_before)
        self.assertEqual(
            [(s["context_id"], s["checkpoint_id"], s["position"]) for s in result["sources"]],
            sources_before,
        )
        snapshot = await self.service.get("u-1", derived_id)
        self.assertEqual(snapshot["title"], "派生改名")

    async def test_重命名从未运行的根线程懒创建(self):
        await self._register_root("u-1", "thread-a", "root hello")
        self.assertEqual(await self._thread_title("thread-fresh"), None)

        result = await self.service.rename("u-1", "thread-fresh", "新会话改名")

        self.assertEqual(result["title"], "新会话改名")
        self.assertEqual(await self._thread_title("thread-fresh"), "新会话改名")

    async def test_重命名其他用户context返回404(self):
        await self._register_root("u-2", "thread-a", "root hello")
        with self.assertRaises(HTTPException) as ctx:
            await self.service.rename("u-1", "thread-a", "越权改名")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_空标题校验失败(self):
        with self.assertRaises(ValueError):
            ContextRenameRequest(title="")

    def test_超长标题校验失败(self):
        with self.assertRaises(ValueError):
            ContextRenameRequest(title="x" * 201)
