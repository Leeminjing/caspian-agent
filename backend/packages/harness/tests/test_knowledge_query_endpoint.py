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

from backend.app.gateway.routers.knowledge import (
    DocumentIngestRequest,
    get_knowledge_list,
    ingest_document,
    query_knowledge,
    QueryRequest,
)
from caspian.knowledge.evidence import DocumentIngestionResult, EvidencePersistenceError, EvidenceValidationError


class _FakeItem:
    def __init__(self, key: str, content: str, level: int, level_basis: dict | None = None):
        self.key = key
        self.value = {
            "content": content,
            "level": level,
            "source": "",
            "source_url": None,
        }
        if level_basis is not None:
            self.value["level_basis"] = level_basis
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

    async def test_文档入库返回稳定批量身份(self):
        expected = DocumentIngestionResult(
            document_id="doc_x",
            document_revision_id="rev_x",
            count=2,
            chunk_ids=("chunk_a", "chunk_b"),
        )
        with patch("backend.app.gateway.routers.knowledge.put_document", return_value=expected):
            response = await ingest_document(
                DocumentIngestRequest(content="# API\n\n事实。", source="官方"),
                _request(_FakeStore([])),
            )
        self.assertEqual(response.status_code, 201)
        self.assertIn(b'"count":2', response.body)
        self.assertIn(b'"chunk_a"', response.body)

    async def test_文档验证错误映射稳定422(self):
        error = EvidenceValidationError("span_gap", "spans 遗漏原文")
        with patch("backend.app.gateway.routers.knowledge.put_document", side_effect=error):
            with self.assertRaises(HTTPException) as caught:
                await ingest_document(
                    DocumentIngestRequest(content="事实。", source="官方"),
                    _request(_FakeStore([])),
                )
        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(caught.exception.detail["code"], "span_gap")

    async def test_文档缺失来源身份返回422且不写入(self):
        with self.assertRaises(HTTPException) as caught:
            await ingest_document(
                DocumentIngestRequest(content="事实。"),
                _request(_FakeStore([])),
            )
        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(caught.exception.detail["code"], "missing_source_identity")

    async def test_文档持久化错误映射明确500(self):
        error = EvidencePersistenceError("write failed", pending_ids=("chunk_x",))
        with patch("backend.app.gateway.routers.knowledge.put_document", side_effect=error):
            with self.assertRaises(HTTPException) as caught:
                await ingest_document(
                    DocumentIngestRequest(content="事实。", source="官方"),
                    _request(_FakeStore([])),
                )
        self.assertEqual(caught.exception.status_code, 500)
        self.assertEqual(caught.exception.detail["pending_ids"], ["chunk_x"])

    async def test_空库返回结构化空响应(self):
        result = await query_knowledge(
            QueryRequest(query="x"),
            _request(_FakeStore([])),
        )
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["ledger"], [])
        self.assertEqual(result["result"]["final_evidence_set"], [])
        self.assertIn("知识库中没有检索到相关内容", result["result"]["notes"][0])

    async def test_judge失败返回未治理(self):
        async def boom(*args, **kwargs):
            raise RuntimeError("boom")

        with (
            patch("caspian.knowledge.pipeline.create_chat_model", return_value=None),
            patch("caspian.knowledge.pipeline.judge_conflicts", side_effect=boom),
        ):
            result = await query_knowledge(
                QueryRequest(query="x"),
                _request(_FakeStore([
                    _FakeItem("a", "c1", 1),
                    _FakeItem("b", "c2", 2),
                ])),
            )
        self.assertEqual(result["status"], "unadjudicated")
        self.assertEqual(len(result["candidates"]), 2)
        for item in result["ledger"]:
            self.assertEqual(item["status"], "unadjudicated")

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

    async def test_列表响应包含level_basis(self):
        store = _FakeStore([
            _FakeItem("k1", "c", 2, level_basis={
                "rated_by": "test-model",
                "rated_at": "2026-09-06T00:00:00+00:00",
                "confidence": 0.9,
                "reason": "r",
                "claim_domain": "PostgreSQL",
                "dimensions": {"primary_source": 3, "domain_fit": 3, "evidence": 3, "specificity": 3},
                "mapping_rule": "no cap → L3",
                "unrated_reason": None,
            }),
        ])
        result = await get_knowledge_list(_request(store))
        entry = result["entries"][0]
        self.assertEqual(entry["level_basis"]["rated_by"], "test-model")
        self.assertEqual(entry["level_basis"]["mapping_rule"], "no cap → L3")

    async def test_列表响应扩展EvidenceUnit追踪字段(self):
        item = _FakeItem("chunk_x", "c", 2)
        item.value.update({
            "record_type": "evidence_unit",
            "chunk_id": "chunk_x",
            "document_id": "doc_x",
            "document_revision_id": "rev_x",
            "title": "API",
            "section_path": ["参数"],
            "chunk_index": 0,
            "source_span": {"start": 0, "end": 1},
            "version": "2",
        })
        result = await get_knowledge_list(_request(_FakeStore([item])))
        entry = result["entries"][0]
        self.assertEqual(entry["document_id"], "doc_x")
        self.assertEqual(entry["source_span"], {"start": 0, "end": 1})
        self.assertFalse(entry["legacy"])


if __name__ == "__main__":
    unittest.main()
