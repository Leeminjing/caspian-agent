"""验证会话生命周期：级联删除/归档/恢复与归档列表。

输入:
    内存 sqlite session factory（PRAGMA foreign_keys=ON）+ InMemorySaver + InMemoryStore
    + ThreadLifecycleService

输出:
    覆盖整棵子树闭包、级联删除不触发 ON DELETE RESTRICT、删除后 web_threads/来源/checkpoint/goal
    被清且知识保留、归档后默认 tree/list 隐藏但数据保留、归档列表、恢复回默认树、非本人 404。
"""

import asyncio
import unittest

from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.gateway.context.models import (
    WebContextSource,
    WebThread,
)
from backend.app.gateway.context.lifecycle import ThreadLifecycleService
from backend.app.gateway.context.service import ContextService
from caspian.persistence.base import Base
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore


class ConversationLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite://")

        @event.listens_for(self.engine.sync_engine, "connect")
        def _fk_on(dbapi_conn, _record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        from backend.app.gateway.context.models import (
            WebContextDefinition,
            WebContextSource,
            WebThread,
        )

        tables = [WebThread.__table__, WebContextDefinition.__table__, WebContextSource.__table__]
        async with self.engine.begin() as conn:
            await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
        self.checkpointer = InMemorySaver()
        self.store = InMemoryStore()
        self.service = ThreadLifecycleService(
            self.checkpointer, self.store, session_factory=self.session_factory
        )
        self.context_service = ContextService(
            self.checkpointer, session_factory=self.session_factory
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _seed_thread(self, user_id: str, thread_id: str, title: str | None = None) -> None:
        async with self.session_factory() as session:
            session.add(WebThread(thread_id=thread_id, user_id=user_id, title=title))
            await session.commit()
        await self.checkpointer.adelete_thread(thread_id)

    async def _seed_source(self, child: str, parent: str, position: int = 0) -> None:
        async with self.session_factory() as session:
            session.add(
                WebContextSource(
                    source_id=f"src-{child}-{parent}-{position}",
                    context_id=child,
                    parent_context_id=parent,
                    source_checkpoint_id=f"cp-{child}",
                    position=position,
                )
            )
            await session.commit()

    async def _thread_exists(self, thread_id: str) -> bool:
        async with self.session_factory() as session:
            return (await session.get(WebThread, thread_id)) is not None

    async def _archived(self, thread_id: str):
        async with self.session_factory() as session:
            row = await session.get(WebThread, thread_id)
            return row.archived_at

    async def test_级联删除整棵子树且不触发restrict(self):
        # 用户 u-1 建根 A，派生出 B（A→B）、C（B→C），另有独立 X、其他用户 Y
        for tid in ("thread-a", "thread-b", "thread-c", "thread-x"):
            await self._seed_thread("u-1", tid, f"title-{tid}")
        await self._seed_thread("u-2", "thread-y", "title-y")
        await self._seed_source("thread-b", "thread-a")
        await self._seed_source("thread-c", "thread-b")

        result = await self.service.delete("u-1", "thread-a")

        self.assertIn("thread-a", result["deleted"])
        self.assertIn("thread-b", result["deleted"])
        self.assertIn("thread-c", result["deleted"])
        # 根与后裔被删，独立线程与他用户线程保留
        self.assertFalse(await self._thread_exists("thread-a"))
        self.assertFalse(await self._thread_exists("thread-b"))
        self.assertFalse(await self._thread_exists("thread-c"))
        self.assertTrue(await self._thread_exists("thread-x"))
        self.assertTrue(await self._thread_exists("thread-y"))

        # 来源行全部清除（父/子引用都不再残留）
        async with self.session_factory() as session:
            remaining = (await session.scalars(select(WebContextSource))).all()
        self.assertEqual(remaining, [])

    async def test_归档后默认tree隐藏且数据保留(self):
        for tid in ("thread-a", "thread-b", "thread-x"):
            await self._seed_thread("u-1", tid, f"title-{tid}")
        await self._seed_source("thread-b", "thread-a")

        await self.service.archive("u-1", "thread-a")

        # 默认 tree 隐藏归档（A、B 不在，X 在）
        tree = await self.context_service.tree("u-1")
        tree_ids = {node["context_id"] for node in tree}
        self.assertNotIn("thread-a", tree_ids)
        self.assertNotIn("thread-b", tree_ids)
        self.assertIn("thread-x", tree_ids)

        # 数据保留：行仍在且 archived_at 非空
        self.assertTrue(await self._thread_exists("thread-a"))
        self.assertIsNotNone(await self._archived("thread-a"))
        self.assertIsNotNone(await self._archived("thread-b"))

        # 归档列表包含 A、B
        archived = await self.service.list_archived("u-1")
        archived_ids = {item["thread_id"] for item in archived}
        self.assertIn("thread-a", archived_ids)
        self.assertIn("thread-b", archived_ids)
        self.assertNotIn("thread-x", archived_ids)

    async def test_恢复归档会话回默认树(self):
        for tid in ("thread-a", "thread-b"):
            await self._seed_thread("u-1", tid)
        await self._seed_source("thread-b", "thread-a")

        await self.service.archive("u-1", "thread-a")
        self.assertIsNotNone(await self._archived("thread-a"))

        await self.service.restore("u-1", "thread-a")

        self.assertIsNone(await self._archived("thread-a"))
        self.assertIsNone(await self._archived("thread-b"))
        tree_ids = {node["context_id"] for node in await self.context_service.tree("u-1")}
        self.assertIn("thread-a", tree_ids)
        self.assertIn("thread-b", tree_ids)

    async def test_非本人操作返回404(self):
        await self._seed_thread("u-2", "thread-a", "title-a")
        for op in (self.service.delete, self.service.archive, self.service.restore):
            with self.assertRaises(HTTPException) as ctx:
                await op("u-1", "thread-a")
            self.assertEqual(ctx.exception.status_code, 404)

    async def test_删除不删除共享知识库(self):
        await self._seed_thread("u-1", "thread-a", "title-a")
        await self.store.aput(("knowledge", "u-1"), "k1", {"content": "共享知识", "level": 3})
        await self.store.aput(("goal", "u-1", "thread-a"), "goal", {"phase": "active"})

        await self.service.delete("u-1", "thread-a")

        # 知识条目保留；goal 条目被清
        self.assertIsNotNone(await self.store.aget(("knowledge", "u-1"), "k1"))
        self.assertIsNone(await self.store.aget(("goal", "u-1", "thread-a"), "goal"))

    async def test_多父来源子会话也被级联闭包覆盖(self):
        # A 与 B 都作为 C 的父来源；删除 A 不应因 C 的 parent RESTRICT 失败
        for tid in ("thread-a", "thread-b", "thread-c"):
            await self._seed_thread("u-1", tid)
        await self._seed_source("thread-c", "thread-a", position=0)
        await self._seed_source("thread-c", "thread-b", position=1)

        result = await self.service.delete("u-1", "thread-a")

        self.assertIn("thread-c", result["deleted"])
        self.assertFalse(await self._thread_exists("thread-a"))
        self.assertFalse(await self._thread_exists("thread-c"))
        self.assertTrue(await self._thread_exists("thread-b"))


if __name__ == "__main__":
    unittest.main()
