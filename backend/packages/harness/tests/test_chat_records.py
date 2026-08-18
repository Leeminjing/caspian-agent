"""
本文件提供聊天记录历史读取接口的标准库 unittest。

输入:
    get_thread_messages 路由函数、InMemorySaver 测试 checkpointer 与临时 thread_id

输出:
    可运行检查，覆盖有历史消息读取、空线程返回空列表、接口受认证保护
"""

import asyncio
import unittest
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from backend.app.gateway.middleware.auth import (
    _AUTH_WHITELIST_PATHS,
    _AUTH_WHITELIST_PREFIXES,
)
from backend.app.gateway.routers.chat_records import get_thread_messages


async def _seed_checkpointer(checkpointer, thread_id, messages):
    """向 InMemorySaver 写入一条带消息的 checkpoint（模拟运行时格式）。"""
    config = {"configurable": {"thread_id": thread_id}}
    checkpoint = {
        "v": 4,
        "ts": "2026-08-14T00:00:00+00:00",
        "id": f"cp-{thread_id}",
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


class ChatRecordsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.checkpointer = InMemorySaver()
        self.request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(checkpointer=self.checkpointer))
        )

    async def test_有历史thread返回序列化消息数组(self):
        await _seed_checkpointer(
            self.checkpointer,
            "th-001",
            [HumanMessage(content="你好"), AIMessage(content="收到")],
        )
        result = await get_thread_messages("th-001", self.request)
        self.assertEqual(
            [message["type"] for message in result["messages"]],
            ["human", "ai"],
        )
        self.assertEqual(result["messages"][0]["content"], "你好")
        self.assertEqual(result["messages"][1]["content"], "收到")

    async def test_空线程返回空消息数组(self):
        result = await get_thread_messages("never-ran", self.request)
        self.assertEqual(result, {"messages": [], "archived": []})

    def test_messages接口不在认证白名单(self):
        self.assertNotIn("/api/threads/{thread_id}/messages", _AUTH_WHITELIST_PATHS)
        self.assertFalse(
            any(
                "/api/threads" in path or path.startswith("/api/threads")
                for path in _AUTH_WHITELIST_PREFIXES
            )
        )


if __name__ == "__main__":
    asyncio.run(unittest.main())
