"""验证 Context 派生树、审批边界和独立 LangGraph checkpoint。

输入:
    内存 sqlite session factory + InMemorySaver + ContextService

输出:
    覆盖递归派生、多来源合并、跨用户拒绝、哈希绑定批准、首次运行前可更新、
    主运行锁定、展示投影、usage 聚合与旧线程兼容读取。
"""

import asyncio
import shutil
import tempfile
import unittest

from fastapi import HTTPException
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.gateway.context.models import (
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


class RecursiveContextForkingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="caspian-context-")
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{self.tmpdir}/context-test.db"
        )
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=_CONTEXT_TABLES))
        self.checkpointer = InMemorySaver()
        self.service = ContextService(self.checkpointer, session_factory=self.session_factory)

        async def fake_make_state_graph():
            graph = create_agent(model=FakeListChatModel(responses=["unused"]), tools=[])
            graph.checkpointer = self.checkpointer
            return graph

        # 测试内以 FakeListChatModel 最小图代替生产 _make_state_graph（避免加载 config.yaml）
        self.service._make_state_graph = fake_make_state_graph

    async def asyncTearDown(self):
        await self.engine.dispose()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def _seed_checkpoint(self, thread_id: str, content: str) -> str:
        """向 InMemorySaver 写入一条带 HumanMessage 的 checkpoint，返回 checkpoint_id。"""
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

    async def _append_checkpoint(self, thread_id: str, messages: list) -> None:
        """以新 checkpoint id 覆盖同 thread 的最新 checkpoint（模拟后续真实运行）。"""
        config = {"configurable": {"thread_id": thread_id}}
        checkpoint_id = f"cp-{thread_id}-append-{len(messages)}"
        checkpoint = {
            "v": 4,
            "ts": "2026-08-16T00:00:01+00:00",
            "id": checkpoint_id,
            "channel_values": {"messages": messages},
            "channel_versions": {"messages": 2},
            "versions_seen": {},
        }
        await self.checkpointer.aput(
            {**config, "configurable": {**config["configurable"], "checkpoint_ns": ""}},
            checkpoint,
            {},
            {"messages": 2},
        )

    async def _register_root(self, user_id: str, thread_id: str, content: str) -> str:
        await self.service.register_main_run(user_id, thread_id)
        return await self._seed_checkpoint(thread_id, content)

    async def _thread_count(self) -> int:
        async with self.session_factory() as session:
            return len((await session.scalars(select(WebThread))).all())

    async def _derive_valid(self, user_id: str, sources, messages, title="新 Context"):
        body = _derive_body(title, sources, messages)
        return await self.service.derive(user_id, body)

    async def test_递归派生链A到B到C与父后续变化隔离(self):
        cp_a = await self._register_root("u-1", "thread-a", "root hello")

        b = await self._derive_valid(
            "u-1",
            [{"context_id": "thread-a", "checkpoint_id": cp_a}],
            [{"role": "human", "content": "B 内容", "id": "msg-b-1"}],
            "B",
        )
        self.assertEqual(b["projection_status"], "valid")
        self.assertNotEqual(b["thread_id"], "thread-a")
        self.assertTrue(b["initial_checkpoint_id"])

        lineage_b = await self.service.lineage("u-1", b["context_id"])
        self.assertEqual(lineage_b["depth"], 1)

        c = await self._derive_valid(
            "u-1",
            [{"context_id": b["context_id"], "checkpoint_id": b["initial_checkpoint_id"]}],
            [{"role": "human", "content": "C 内容", "id": "msg-c-1"}],
            "C",
        )
        self.assertEqual(c["projection_status"], "valid")
        self.assertEqual((await self.service.lineage("u-1", c["context_id"]))["depth"], 2)

        # 父 A 后续变化不影响 B / C
        await self._append_checkpoint("thread-a", [HumanMessage(content="root hello"), AIMessage(content="A 新消息")])
        snapshot_b = await self.service.snapshot("u-1", b["context_id"])
        self.assertEqual(
            [message["content"] for message in snapshot_b["messages"]],
            ["B 内容"],
        )
        snapshot_c = await self.service.snapshot("u-1", c["context_id"])
        self.assertEqual([message["content"] for message in snapshot_c["messages"]], ["C 内容"])

    async def test_同源派生两个分支(self):
        cp_a = await self._register_root("u-1", "thread-a", "root hello")

        b = await self._derive_valid(
            "u-1",
            [{"context_id": "thread-a", "checkpoint_id": cp_a}],
            [{"role": "human", "content": "B", "id": "b-1"}],
            "B",
        )
        c = await self._derive_valid(
            "u-1",
            [{"context_id": "thread-a", "checkpoint_id": cp_a}],
            [{"role": "human", "content": "C", "id": "c-1"}],
            "C",
        )

        tree = await self.service.tree("u-1")
        by_id = {node["context_id"]: node for node in tree}
        self.assertEqual(by_id["thread-a"]["depth"], 0)
        self.assertEqual(by_id[b["context_id"]]["depth"], 1)
        self.assertEqual(by_id[c["context_id"]]["depth"], 1)
        self.assertEqual(by_id[b["context_id"]]["parents"][0]["context_id"], "thread-a")
        self.assertEqual(by_id[c["context_id"]]["parents"][0]["context_id"], "thread-a")

    async def test_多来源合并B加C派生D(self):
        cp_a = await self._register_root("u-1", "thread-a", "root hello")
        b = await self._derive_valid(
            "u-1",
            [{"context_id": "thread-a", "checkpoint_id": cp_a}],
            [{"role": "human", "content": "B", "id": "b-1"}],
            "B",
        )
        c = await self._derive_valid(
            "u-1",
            [{"context_id": "thread-a", "checkpoint_id": cp_a}],
            [{"role": "human", "content": "C", "id": "c-1"}],
            "C",
        )

        d = await self._derive_valid(
            "u-1",
            [
                {"context_id": b["context_id"], "checkpoint_id": b["initial_checkpoint_id"]},
                {"context_id": c["context_id"], "checkpoint_id": c["initial_checkpoint_id"]},
            ],
            [{"role": "human", "content": "D", "id": "d-1"}],
            "D",
        )
        self.assertEqual(len(d["sources"]), 2)
        self.assertEqual([source["context_id"] for source in d["sources"]],
                         [b["context_id"], c["context_id"]])
        self.assertEqual((await self.service.lineage("u-1", d["context_id"]))["depth"], 2)

    async def test_跨用户派生被拒绝且不创建子Context(self):
        cp_a = await self._register_root("u-2", "thread-a", "root hello")
        before = await self._thread_count()

        with self.assertRaises(HTTPException) as ctx:
            await self._derive_valid(
                "u-1",
                [{"context_id": "thread-a", "checkpoint_id": cp_a}],
                [{"role": "human", "content": "越权", "id": "x-1"}],
            )
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(await self._thread_count(), before)

    async def test_降级需批准拒绝不创建运行且批准哈希绑定(self):
        cp_a = await self._register_root("u-1", "thread-a", "root hello")

        d = await self._derive_valid(
            "u-1",
            [{"context_id": "thread-a", "checkpoint_id": cp_a}],
            [{"role": "tool", "content": "缺少协议字段"}],
            "受阻 Context",
        )
        self.assertEqual(d["projection_status"], "approval_required")
        self.assertIsNone(d["initial_checkpoint_id"])

        # 受阻 Context 禁止主运行
        with self.assertRaises(HTTPException) as ctx:
            await self.service.ensure_runnable("u-1", d["context_id"])
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail["code"], "context_projection_blocked")

        # 哈希不匹配的批准被拒绝
        with self.assertRaises(HTTPException) as ctx:
            await self.service.decide(
                "u-1",
                d["context_id"],
                _decision("accept", "0" * 64, "1" * 64),
            )
        self.assertEqual(ctx.exception.status_code, 409)

        # 拒绝保留 Context，仍不可运行
        rejected = await self.service.decide(
            "u-1",
            d["context_id"],
            _decision("reject", d["definition_hash"], d["projection_hash"]),
        )
        self.assertEqual(rejected["projection_status"], "rejected")
        with self.assertRaises(HTTPException):
            await self.service.ensure_runnable("u-1", d["context_id"])

        # 正确哈希批准后初始化 checkpoint，可运行
        approved = await self.service.decide(
            "u-1",
            d["context_id"],
            _decision("accept", d["definition_hash"], d["projection_hash"]),
        )
        self.assertEqual(approved["projection_status"], "approved")
        self.assertTrue(approved["initial_checkpoint_id"])
        await self.service.ensure_runnable("u-1", d["context_id"])

    async def test_首次主运行前可更新定义且运行后锁定(self):
        cp_a = await self._register_root("u-1", "thread-a", "root hello")
        b = await self._derive_valid(
            "u-1",
            [{"context_id": "thread-a", "checkpoint_id": cp_a}],
            [{"role": "human", "content": "B v1", "id": "b-1"}],
            "B",
        )
        old_hash = b["definition_hash"]

        updated = await self.service.update_definition(
            "u-1",
            b["context_id"],
            _definition_update([{"role": "human", "content": "B v2", "id": "b-2"}]),
        )
        self.assertEqual(updated["projection_status"], "valid")
        self.assertNotEqual(updated["definition_hash"], old_hash)
        snapshot = await self.service.snapshot("u-1", b["context_id"])
        self.assertEqual([message["content"] for message in snapshot["messages"]], ["B v2"])

        # 首个主运行被接受后锁定
        await self.service.register_main_run("u-1", b["context_id"])
        with self.assertRaises(HTTPException) as ctx:
            await self.service.update_definition(
                "u-1",
                b["context_id"],
                _definition_update([{"role": "human", "content": "B v3", "id": "b-3"}]),
            )
        self.assertEqual(ctx.exception.status_code, 409)

    async def test_展示投影为authored加后缀且过滤合成占位(self):
        cp_a = await self._register_root("u-1", "thread-a", "root hello")

        # 无损修补路径：孤立 ToolMessage → repaired，执行投影含合成 AI Tool Call
        orphan = {"role": "tool", "content": "只保留的结果", "tool_call_id": "call-kept", "name": "search", "id": "msg-tool"}
        b = await self._derive_valid(
            "u-1",
            [{"context_id": "thread-a", "checkpoint_id": cp_a}],
            [orphan],
            "B",
        )
        self.assertEqual(b["projection_status"], "repaired")
        self.assertTrue(any(item.get("kind") == "missing_tool_call" for item in b["repair_manifest"]))

        # 追加真实后续消息（模拟真实运行）
        async with self.session_factory() as session:
            task = await session.get(WebThread, b["context_id"])
        await self._append_checkpoint(
            task.thread_id,
            [
                AIMessage(content="", tool_calls=[{"id": "call-kept", "name": "search", "args": {}}]),
                HumanMessage(content="只保留的结果", id="msg-tool"),
                AIMessage(content="真实后续回复", id="msg-real"),
            ],
        )
        snapshot = await self.service.snapshot("u-1", b["context_id"])
        contents = [message["content"] for message in snapshot["messages"]]
        self.assertIn("只保留的结果", contents)
        self.assertIn("真实后续回复", contents)
        # authored 定义未被执行修补反写
        self.assertEqual(snapshot["messages"][0]["content"], "只保留的结果")
        # 合成占位不展示
        self.assertFalse(any(message.get("curation_synthetic") for message in snapshot["messages"]))

        async with self.session_factory() as session:
            definition = await session.get(WebContextDefinition, b["context_id"])
        self.assertEqual(definition.authored_messages, [orphan])

    async def test_usage聚合与命中率(self):
        await self._register_root("u-1", "thread-a", "root hello")
        await self.service.accumulate_usage("thread-a", 100, 35)
        await self.service.accumulate_usage("thread-a", 50, 10)
        # 零 usage 调用不落库不报错
        await self.service.accumulate_usage("thread-a", 0, 0)

        tree = await self.service.tree("u-1")
        node = next(item for item in tree if item["context_id"] == "thread-a")
        self.assertEqual(node["cache_input_tokens"], 150)
        self.assertEqual(node["cache_hit_tokens"], 45)
        self.assertAlmostEqual(node["cache_hit_rate"], 0.3)

        # 无 usage 的线程显示未知（None）
        await self.service.register_main_run("u-1", "thread-x")
        tree = await self.service.tree("u-1")
        node_x = next(item for item in tree if item["context_id"] == "thread-x")
        self.assertIsNone(node_x["cache_hit_rate"])
        self.assertEqual(node_x["projection_status"], "root")
        self.assertEqual(node_x["parents"], [])

    async def test_tree兼容无lineage旧线程(self):
        await self._register_root("u-1", "thread-a", "root hello")
        await self.service.register_main_run("u-1", "thread-old")

        tree = await self.service.tree("u-1")
        self.assertEqual(len(tree), 2)
        old = next(item for item in tree if item["context_id"] == "thread-old")
        self.assertEqual(old["depth"], 0)
        self.assertEqual(old["projection_status"], "root")
        self.assertFalse(old["editable"])

    async def test_跨用户查询与操作全部拒绝(self):
        await self._register_root("u-1", "thread-a", "root hello")
        for call in [
            lambda: self.service.snapshot("u-2", "thread-a"),
            lambda: self.service.lineage("u-2", "thread-a"),
            lambda: self.service.get("u-2", "thread-a"),
        ]:
            with self.assertRaises(HTTPException) as ctx:
                await call()
            self.assertEqual(ctx.exception.status_code, 404)


class ChatRecordsDerivedProjectionTests(unittest.IsolatedAsyncioTestCase):
    """验证 /messages 路由对派生 Context 返回展示投影（f46 增量）。"""

    async def asyncSetUp(self):
        from types import SimpleNamespace

        from backend.app.gateway.routers.chat_records import get_thread_messages
        from caspian.persistence.engine import dispose_engine, get_session, init_engine

        self.get_thread_messages = get_thread_messages
        self.get_session = get_session
        self.dispose_engine = dispose_engine
        self.tmpdir = tempfile.mkdtemp(prefix="caspian-chatrecords-")
        app_config = SimpleNamespace(
            database=SimpleNamespace(
                backend="sqlite",
                url=f"sqlite+aiosqlite:///{self.tmpdir}/chatrecords-test.db",
                echo=False,
                pool_size=1,
                max_overflow=0,
                pool_timeout=5,
                pool_pre_ping=False,
                pool_recycle=-1,
                isolation_level=None,
            )
        )
        self.engine = init_engine(app_config)
        async with self.engine.begin() as conn:
            await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=_CONTEXT_TABLES))
        self.checkpointer = InMemorySaver()
        service = ContextService(self.checkpointer, session_factory=get_session)
        self.request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(checkpointer=self.checkpointer, context_service=service)
            ),
            state=SimpleNamespace(current_user=SimpleNamespace(id="u-1")),
        )

    async def asyncTearDown(self):
        # ponytail: dispose_engine 的同步 dispose() 在 async 上下文产生未等待协程，
        # 测试内显式 await 后直接重置全局单例
        import warnings

        from caspian.persistence import engine as engine_module

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            self.dispose_engine()
        await self.engine.dispose()
        engine_module._engine = None
        engine_module._session_factory = None
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def test_派生线程返回展示投影且合成占位被过滤(self):
        async with self.get_session() as session:
            session.add(WebThread(thread_id="th-derived", user_id="u-1", title="B"))
            session.add(
                WebContextDefinition(
                    context_id="th-derived",
                    authored_messages=[
                        {"role": "human", "content": "authored-1", "id": "m1"},
                        {"role": "human", "content": "authored-2", "id": "m2"},
                    ],
                    execution_messages=[],
                    repair_manifest=[],
                    issues=[],
                    definition_hash="a" * 64,
                    projection_hash="b" * 64,
                    projection_status="valid",
                    initial_message_ids=["m1", "m2"],
                    initial_checkpoint_id="cp-derived",
                )
            )
            await session.commit()

        await _put_checkpoint(
            self.checkpointer,
            "th-derived",
            "cp-derived",
            [
                HumanMessage(content="authored-1", id="m1"),
                HumanMessage(content="authored-2", id="m2"),
                AIMessage(content="真实后续回复", id="r1"),
            ],
        )

        result = await self.get_thread_messages("th-derived", self.request)
        self.assertEqual(
            [message["content"] for message in result["messages"]],
            ["authored-1", "authored-2", "真实后续回复"],
        )

    async def test_非派生线程保持原始读取路径(self):
        await _put_checkpoint(
            self.checkpointer,
            "th-plain",
            "cp-plain",
            [HumanMessage(content="你好"), AIMessage(content="收到")],
        )
        result = await self.get_thread_messages("th-plain", self.request)
        self.assertEqual(
            [message["type"] for message in result["messages"]],
            ["human", "ai"],
        )


async def _put_checkpoint(checkpointer, thread_id: str, checkpoint_id: str, messages: list) -> None:
    config = {"configurable": {"thread_id": thread_id}}
    checkpoint = {
        "v": 4,
        "ts": "2026-08-16T00:00:00+00:00",
        "id": checkpoint_id,
        "channel_values": {"messages": messages},
        "channel_versions": {"messages": 1},
        "versions_seen": {},
    }
    await checkpointer.aput(
        {**config, "configurable": {**config["configurable"], "checkpoint_ns": ""}},
        checkpoint,
        {},
        {"messages": 1},
    )


def _derive_body(title, sources, messages):
    from backend.app.gateway.context.models import ContextDeriveCreate

    return ContextDeriveCreate(title=title, sources=sources, messages=messages)


def _definition_update(messages):
    from backend.app.gateway.context.models import ContextDefinitionUpdate

    return ContextDefinitionUpdate(messages=messages)


def _decision(decision, definition_hash, projection_hash):
    from backend.app.gateway.context.models import ContextProjectionDecision

    return ContextProjectionDecision(
        decision=decision,
        definition_hash=definition_hash,
        projection_hash=projection_hash,
    )


if __name__ == "__main__":
    asyncio.run(unittest.main())
