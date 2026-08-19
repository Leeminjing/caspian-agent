"""
本文件对外提供 /api/knowledge/query 端点响应结构回归测试（cleanup-dead-and-duplicated-code）。

对外提供:
    KnowledgeQueryEndpointTests — 验证查询端点空库响应结构、judge 失败 502 与成功响应结构

输入: 无 — 测试内以 stub store 与 mock patch 构造场景

输出: unittest 测试结果

具体工作流:
    (1) 构造 FakeStore 与 stub Request（app.state.store + state.current_user）
    (2) 空库路径:query_knowledge 返回 candidates=[]、ledger=[] 且 final_evidence_set 为空
    (3) judge 失败路径:patch pipeline.judge_conflicts 抛异常 → 断言 HTTPException 502
    (4) 成功路径:patch judge_conflicts 返回无冲突 → 断言响应含 candidates/ledger/
        final_evidence_set/notes

示例:
    python -m unittest tests.test_knowledge_query_endpoint
"""

import types
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from backend.app.gateway.routers.knowledge import query_knowledge, QueryRequest


class _FakeItem:
    def __init__(self, key: str, content: str, level: int):
        self.key = key
        self.value = {
            "content": content,
            "level": level,
            "source": "",
            "source_url": None,
        }
        self.score = None


class _FakeStore:
    def __init__(self, items):
        self._items = list(items)

    async def asearch(self, *args, **kwargs):
        return list(self._items)


def _request(store) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        app=types.SimpleNamespace(state=types.SimpleNamespace(store=store)),
        state=types.SimpleNamespace(current_user=types.SimpleNamespace(id="u1")),
    )


class KnowledgeQueryEndpointTests(unittest.IsolatedAsyncioTestCase):

    async def test_空库返回结构化空响应(self):
        result = await query_knowledge(
            QueryRequest(query="x"),
            _request(_FakeStore([])),
        )
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["ledger"], [])
        self.assertEqual(result["result"]["final_evidence_set"], [])
        self.assertIn("知识库中没有检索到相关内容", result["result"]["notes"][0])

    async def test_judge失败返回502(self):
        async def boom(*args, **kwargs):
            raise RuntimeError("boom")

        with (
            patch("caspian.knowledge.pipeline.create_chat_model", return_value=None),
            patch("caspian.knowledge.pipeline.judge_conflicts", side_effect=boom),
        ):
            with self.assertRaises(HTTPException) as ctx:
                await query_knowledge(
                    QueryRequest(query="x"),
                    _request(_FakeStore([
                        _FakeItem("a", "c1", 1),
                        _FakeItem("b", "c2", 2),
                    ])),
                )
        self.assertEqual(ctx.exception.status_code, 502)

    async def test_成功响应含完整治理结构(self):
        with (
            patch("caspian.knowledge.pipeline.create_chat_model", return_value=None),
            patch("caspian.knowledge.pipeline.judge_conflicts", return_value=[]),
        ):
            result = await query_knowledge(
                QueryRequest(query="x"),
                _request(_FakeStore([
                    _FakeItem("a", "c1", 1),
                    _FakeItem("b", "c2", 2),
                ])),
            )
        self.assertEqual(len(result["candidates"]), 2)
        self.assertEqual(len(result["result"]["final_evidence_set"]), 2)
        self.assertEqual(len(result["ledger"]), 2)
        self.assertIn("notes", result["result"])
        for item in result["ledger"]:
            self.assertEqual(item["status"], "retained")


if __name__ == "__main__":
    unittest.main()
