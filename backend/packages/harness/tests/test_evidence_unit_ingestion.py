"""
本文件对外提供 Evidence Unit 领域模型、身份、结构切分、span 验证与入库编排测试。

输入:
    Markdown/纯文本 fixtures、可审计 atomicity gate、确定性 token counter、fake
    segmenter/model/store 和评级桩。

输出:
    unittest/pytest 可运行断言，复验精确原文、零 overlap、来源感知身份、检索字段隔离、
    写前全量验证、逐单元评级、hard max、批量 index 及 legacy 投影。

具体工作流:
    纯函数先做表驱动断言，再用 fake Store 捕获 PutOp；所有测试不调用真实网络、LLM 或 embedding。

示例:
    python -m pytest backend/packages/harness/tests/test_evidence_unit_ingestion.py -q
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from caspian.config.knowledge_config import RatingConfig
from langchain_core.embeddings import Embeddings
from langgraph.store.memory import InMemoryStore
from caspian.knowledge.chunking import (
    evaluate_atomicity_gate,
    needs_semantic_split,
    split_structural_blocks,
    to_absolute_blocks,
    validate_ordered_non_overlapping,
    validate_semantic_spans,
)
from caspian.knowledge.evidence import (
    CandidateBlock,
    DocumentInput,
    EvidenceUnit,
    EvidenceValidationError,
    SourceSpan,
)
from caspian.knowledge.identity import (
    canonical_source,
    content_hash,
    make_chunk_id,
    make_document_id,
    make_revision_id,
)
from caspian.knowledge.ingestion import put_atomic_knowledge, put_document
from caspian.knowledge.retrieval_text import build_retrieval_text
from caspian.knowledge.schemas import RatingDimensions, RatingOutput
from caspian.knowledge.store_client import evidence_from_item, put_evidence_units, update_provenance


def _words(text: str) -> int:
    return len(text.split())


class _Store:
    def __init__(self):
        self.operations = []

    async def abatch(self, operations):
        self.operations.extend(list(operations))
        return [None] * len(self.operations)


class _Model:
    model_name = "fake-model"

    def get_num_tokens(self, text):
        return _words(text)


class _Embeddings(Embeddings):
    def __init__(self):
        self.documents = []

    def embed_documents(self, texts):
        self.documents.extend(texts)
        return [[float(len(text)), 1.0] for text in texts]

    async def aembed_documents(self, texts):
        return self.embed_documents(texts)

    def embed_query(self, text):
        return [float(len(text)), 1.0]

    async def aembed_query(self, text):
        return self.embed_query(text)


class _Segmenter:
    def __init__(self, spans=None, error=None):
        self.spans = spans or ()
        self.error = error
        self.calls = 0

    async def split(self, candidate):
        self.calls += 1
        if self.error:
            raise self.error
        return self.spans


class DomainAndIdentityTests(unittest.TestCase):
    def test_source_span_rejects_reverse_range(self):
        with self.assertRaises(ValueError):
            SourceSpan(start=3, end=3)
        with self.assertRaises(ValueError):
            SourceSpan(start=1.0, end=2)

    def test_document_preserves_content_whitespace(self):
        document = DocumentInput(content=" 事实。 \r\n", source="官方")
        self.assertEqual(document.content, " 事实。 \r\n")

    def test_canonical_source_priority_and_url_normalization(self):
        self.assertEqual(canonical_source(external_source_id="  repo:42  ", source_url="https://ignored.test"), "external:repo:42")
        self.assertEqual(canonical_source(source_url="HTTPS://Example.COM:443/a?q=2&q=1#part"), "url:https://example.com/a?q=2&q=1")

    def test_manual_source_requires_explicit_content_hash(self):
        digest = content_hash("same")
        self.assertEqual(canonical_source(manual_content_hash=digest), f"manual-content:{digest}")
        with self.assertRaises(EvidenceValidationError):
            canonical_source()

    def test_hierarchical_ids_are_stable_and_source_sensitive(self):
        first = make_document_id(canonical_source(source_url="https://a.test/x"))
        second = make_document_id(canonical_source(source_url="https://b.test/x"))
        self.assertNotEqual(first, second)
        revision = make_revision_id(first, content_hash("body"), version="1")
        changed_revision = make_revision_id(first, content_hash("body"), version="2")
        self.assertNotEqual(revision, changed_revision)
        same = make_chunk_id(revision, SourceSpan(start=0, end=4), content_hash("body"))
        self.assertEqual(same, make_chunk_id(revision, SourceSpan(start=0, end=4), content_hash("body")))
        self.assertNotEqual(same, make_chunk_id(revision, SourceSpan(start=1, end=4), content_hash("ody")))

    def test_retrieval_text_has_only_whitelisted_context(self):
        first = build_retrieval_text("事实", title="API", section_path=("参数",), version="2")
        second = build_retrieval_text("事实", title="API", section_path=("参数",), version="2")
        self.assertEqual(first, second)
        self.assertNotIn("source", first.lower())
        self.assertNotIn("level", first.lower())
        variants = [
            build_retrieval_text("新事实", title="API", section_path=("参数",), version="2"),
            build_retrieval_text("事实", title="API 2", section_path=("参数",), version="2"),
            build_retrieval_text("事实", title="API", section_path=("返回值",), version="2"),
            build_retrieval_text("事实", title="API", section_path=("参数",), version="3"),
        ]
        self.assertTrue(all(value != first for value in variants))


class ChunkingTests(unittest.TestCase):
    def test_markdown_structure_and_absolute_spans(self):
        content = "# API\r\n\r\n第一段。\r\n\r\n- A\r\n- B\r\n\r\n| K | V |\r\n|---|---|\r\n| a | b |\r\n\r\n```py\r\nx = 1\r\n```\r\n"
        blocks = split_structural_blocks(content, "markdown")
        self.assertEqual([item.kind for item in blocks], ["paragraph", "list", "table", "code"])
        self.assertTrue(all(item.section_path == ("API",) for item in blocks))
        self.assertTrue(all(item.section_refs[0].title == "API" for item in blocks))
        self.assertTrue(all(content[item.section_refs[0].source_span.start:item.section_refs[0].source_span.end] == "API" for item in blocks))
        self.assertTrue(all(content[item.source_span.start:item.source_span.end] == item.content for item in blocks))

    def test_plain_text_preserves_crlf_unicode_and_edge_whitespace(self):
        content = " 甲\r\n乙 \r\n\r\n\r\n丙\n"
        blocks = split_structural_blocks(content, "text")
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0].content, " 甲\r\n乙 \r\n")
        self.assertEqual(blocks[1].content, "丙\n")

    def test_atomicity_gate_uses_structure_not_ideal_upper_bound(self):
        short = CandidateBlock(kind="paragraph", content=" ".join(["fact"] * 60), source_span=SourceSpan(start=0, end=299), structural_index=0)
        long_cluster = CandidateBlock(kind="paragraph", content=" ".join(["fact"] * 450), source_span=SourceSpan(start=0, end=2249), structural_index=0)
        oversized = CandidateBlock(kind="paragraph", content=" ".join(["fact"] * 601), source_span=SourceSpan(start=0, end=3004), structural_index=0)
        multi_text = "- React: ref behavior\n- Vue: reactivity behavior\n"
        multi = CandidateBlock(kind="list", content=multi_text, source_span=SourceSpan(start=0, end=len(multi_text)), structural_index=0)
        two_topics_text = "A 功能已废弃。另一方面，B 默认值是 20。"
        two_topics = CandidateBlock(kind="paragraph", content=two_topics_text, source_span=SourceSpan(start=0, end=len(two_topics_text)), structural_index=0)
        compound_text = "Mode is safe; timeout is 20."
        compound = CandidateBlock(kind="paragraph", content=compound_text, source_span=SourceSpan(start=0, end=len(compound_text)), structural_index=0)
        same_cluster_text = "React 19 accepts ref as a prop. forwardRef is therefore no longer required."
        same_cluster = CandidateBlock(kind="paragraph", content=same_cluster_text, source_span=SourceSpan(start=0, end=len(same_cluster_text)), structural_index=0)
        self.assertFalse(needs_semantic_split(short, _words))
        self.assertFalse(needs_semantic_split(long_cluster, _words))
        self.assertTrue(needs_semantic_split(oversized, _words))
        self.assertTrue(needs_semantic_split(multi, _words))
        self.assertTrue(needs_semantic_split(two_topics, _words))
        self.assertFalse(needs_semantic_split(compound, _words))
        self.assertFalse(needs_semantic_split(same_cluster, _words))
        self.assertEqual(evaluate_atomicity_gate(oversized, _words).reason, "hard_max_exceeded")
        self.assertEqual(evaluate_atomicity_gate(same_cluster, _words).reason, "no_high_confidence_multi_topic_signal")

    def test_span_validation_accepts_whitespace_gaps_and_absolutizes(self):
        document = "# H\n\nAlpha\n\nBeta"
        candidate = CandidateBlock(kind="paragraph", content="Alpha\n\nBeta", source_span=SourceSpan(start=5, end=16), section_path=("H",), structural_index=0)
        spans = [SourceSpan(start=0, end=5), SourceSpan(start=7, end=11)]
        blocks = to_absolute_blocks(document, candidate, spans, _words)
        self.assertEqual([(item.source_span.start, item.source_span.end) for item in blocks], [(5, 10), (12, 16)])
        validate_ordered_non_overlapping(blocks, _words)

    def test_span_validation_rejects_overlap_gap_range_blank_and_hard_max(self):
        candidate = CandidateBlock(kind="paragraph", content="Alpha Beta", source_span=SourceSpan(start=0, end=10), structural_index=0)
        cases = [
            [SourceSpan(start=0, end=7), SourceSpan(start=6, end=10)],
            [SourceSpan(start=0, end=5), SourceSpan(start=7, end=10)],
            [SourceSpan(start=0, end=11)],
            [SourceSpan(start=5, end=6)],
        ]
        for spans in cases:
            with self.assertRaises(EvidenceValidationError):
                validate_semantic_spans(candidate, spans, _words)
        huge = CandidateBlock(kind="paragraph", content=" ".join(["x"] * 601), source_span=SourceSpan(start=0, end=1201), structural_index=0)
        with self.assertRaises(EvidenceValidationError):
            validate_semantic_spans(huge, [SourceSpan(start=0, end=len(huge.content))], _words)


class IngestionTests(unittest.IsolatedAsyncioTestCase):
    async def test_document_ingestion_validates_then_batches_ordered_units(self):
        content = "- Alpha: enabled\n- Beta: disabled\n"
        split_at = content.index("\n")
        segmenter = _Segmenter((SourceSpan(start=0, end=split_at), SourceSpan(start=split_at + 1, end=len(content.rstrip()))))
        store = _Store()
        rating = RatingOutput(claim_domain="one", dimensions=RatingDimensions(primary_source=3, domain_fit=3, evidence=3, specificity=3), confidence=0.9, reason="ok")
        with patch("caspian.knowledge.ingestion.rate_level", new=AsyncMock(return_value=rating)):
            result = await put_document(store, "u1", DocumentInput(content=content, source="Official"), model=_Model(), segmenter=segmenter)
        self.assertEqual(result.count, 2)
        self.assertEqual(len(store.operations), 2)
        self.assertEqual([op.index for op in store.operations], [["retrieval_text"], ["retrieval_text"]])
        values = [op.value for op in store.operations]
        self.assertEqual([value["chunk_index"] for value in values], [0, 1])
        self.assertTrue(all(value["record_type"] == "evidence_unit" for value in values))
        self.assertTrue(all(value["atomicity"] == "atomic" for value in values))

    async def test_document_ingestion_stage_order_is_split_rate_then_write(self):
        events = []

        class _OrderedSegmenter:
            async def split(self, candidate):
                events.append("split")
                split_at = candidate.content.index("\n")
                return (
                    SourceSpan(start=0, end=split_at),
                    SourceSpan(start=split_at + 1, end=len(candidate.content)),
                )

        class _OrderedStore(_Store):
            async def abatch(self, operations):
                events.append("write")
                await super().abatch(operations)

        async def rate(*args, **kwargs):
            events.append("rate")
            return RatingOutput(
                claim_domain="ordered",
                dimensions=RatingDimensions(
                    primary_source=3,
                    domain_fit=3,
                    evidence=3,
                    specificity=3,
                ),
                confidence=0.9,
                reason="ok",
            )

        with patch("caspian.knowledge.ingestion.rate_level", new=rate):
            content = "- Alpha: on\n- Beta: off"
            await put_document(
                _OrderedStore(),
                "u1",
                DocumentInput(content=content, source="Official"),
                model=_Model(),
                segmenter=_OrderedSegmenter(),
            )
        self.assertEqual(events, ["split", "rate", "rate", "write"])

    async def test_late_segmentation_failure_writes_nothing(self):
        store = _Store()
        segmenter = _Segmenter(error=EvidenceValidationError("bad", "bad spans"))
        with self.assertRaises(EvidenceValidationError):
            await put_document(store, "u1", DocumentInput(content="First fact.\n\n- Alpha: on\n- Beta: off\n", source="Official"), model=_Model(), segmenter=segmenter)
        self.assertEqual(store.operations, [])

    async def test_each_unit_is_rated_independently(self):
        content = "Alpha\n\nBeta"
        outputs = [
            RatingOutput(claim_domain="A", dimensions=RatingDimensions(primary_source=3, domain_fit=3, evidence=3, specificity=3), confidence=0.9, reason="a"),
            RatingOutput(claim_domain="B", dimensions=RatingDimensions(primary_source=2, domain_fit=1, evidence=2, specificity=2), confidence=0.9, reason="b"),
        ]
        store = _Store()
        with patch("caspian.knowledge.ingestion.rate_level", new=AsyncMock(side_effect=outputs)):
            await put_document(store, "u1", DocumentInput(content=content, format="text", source="Official"), model=_Model())
        self.assertEqual([op.value["level"] for op in store.operations], [3, 1])
        self.assertEqual([op.value["level_basis"]["claim_domain"] for op in store.operations], ["A", "B"])

    async def test_blacklist_skips_rating_model(self):
        store = _Store()
        rater = AsyncMock()
        with patch("caspian.knowledge.ingestion.rate_level", new=rater):
            await put_document(store, "u1", DocumentInput(content="Bad", source_url="https://bad.test/x"), domains={"bad.test": 0}, model=_Model())
        rater.assert_not_awaited()
        self.assertEqual(store.operations[0].value["level"], 0)

    async def test_atomic_entry_enforces_hard_max(self):
        with self.assertRaises(EvidenceValidationError) as caught:
            await put_atomic_knowledge(_Store(), "u1", "x", source="manual", model=_Model(), token_counter=lambda text: 601, rating_cfg=RatingConfig(enabled=False))
        self.assertEqual(caught.exception.code, "hard_max_exceeded")

    async def test_same_content_different_source_has_distinct_ids(self):
        store = _Store()
        first = await put_atomic_knowledge(store, "u1", "same", source_url="https://a.test/x", model=_Model(), rating_cfg=RatingConfig(enabled=False))
        second = await put_atomic_knowledge(store, "u1", "same", source_url="https://b.test/x", model=_Model(), rating_cfg=RatingConfig(enabled=False))
        self.assertNotEqual(first[0], second[0])

    async def test_same_atomic_input_replays_as_one_upsert(self):
        store = InMemoryStore()
        first = await put_atomic_knowledge(store, "u1", "same", source="manual", model=_Model(), rating_cfg=RatingConfig(enabled=False))
        second = await put_atomic_knowledge(store, "u1", "same", source="manual", model=_Model(), rating_cfg=RatingConfig(enabled=False))
        self.assertEqual(first[0], second[0])
        self.assertEqual(len(await store.asearch(("knowledge", "u1"), limit=10)), 1)

    async def test_batch_failure_reports_no_false_success(self):
        class _FailingStore:
            async def abatch(self, operations):
                raise RuntimeError("store down")

        unit = EvidenceUnit(
            chunk_id="chunk_failed",
            document_id="doc_failed",
            document_revision_id="rev_failed",
            content="fact",
            retrieval_text="Content:\nfact",
            chunk_index=0,
            source_span=SourceSpan(start=0, end=4),
        )
        from caspian.knowledge.evidence import EvidencePersistenceError

        with self.assertRaises(EvidencePersistenceError) as caught:
            await put_evidence_units(_FailingStore(), "u1", [unit])
        self.assertEqual(caught.exception.completed_ids, ())
        self.assertEqual(caught.exception.pending_ids, ("chunk_failed",))

    def test_legacy_projection_keeps_old_record_readable(self):
        item = SimpleNamespace(key="old", value={"content": "legacy", "level": 2, "source": "s", "source_url": None}, score=0.5)
        entry = evidence_from_item(item)
        self.assertTrue(entry.legacy)
        self.assertEqual(entry.atomicity, "legacy_unknown")
        self.assertEqual(entry.temporal_bindings, ())
        self.assertIsNone(entry.document_id)
        self.assertEqual(entry.content, "legacy")

    async def test_embedding_receives_only_retrieval_text_and_patch_does_not_reembed(self):
        embeddings = _Embeddings()
        store = InMemoryStore(index={"embed": embeddings, "dims": 2, "fields": ["retrieval_text"]})
        unit = EvidenceUnit(
            chunk_id="chunk_x",
            document_id="doc_x",
            document_revision_id="rev_x",
            content="事实",
            retrieval_text="Title: API\nContent:\n事实",
            chunk_index=0,
            source_span=SourceSpan(start=0, end=2),
            level=3,
            level_basis={"reason": "official"},
            provenance={"source_type": "official"},
        )
        await put_evidence_units(store, "u1", [unit])
        self.assertEqual(embeddings.documents, [unit.retrieval_text])
        await update_provenance(store, "u1", unit.chunk_id, level_override=1)
        self.assertEqual(embeddings.documents, [unit.retrieval_text])
        stored = await store.aget(("knowledge", "u1"), unit.chunk_id)
        self.assertEqual(stored.value["retrieval_text"], unit.retrieval_text)
        self.assertEqual(stored.value["level"], 1)

    async def test_new_record_rejects_source_identity_mutation(self):
        store = InMemoryStore()
        unit = EvidenceUnit(
            chunk_id="chunk_source_bound",
            document_id="doc_source_bound",
            document_revision_id="rev_source_bound",
            content="fact",
            retrieval_text="Content:\nfact",
            chunk_index=0,
            source_span=SourceSpan(start=0, end=4),
            source_url="https://old.test/docs",
        )
        await put_evidence_units(store, "u1", [unit])
        with self.assertRaisesRegex(ValueError, "来源身份不可原地修改"):
            await update_provenance(
                store,
                "u1",
                unit.chunk_id,
                source_url="https://new.test/docs",
            )
        stored = await store.aget(("knowledge", "u1"), unit.chunk_id)
        self.assertEqual(stored.value["source_url"], "https://old.test/docs")
        self.assertEqual(stored.value["document_id"], "doc_source_bound")

    async def test_new_and_legacy_records_coexist_in_one_namespace(self):
        store = InMemoryStore()
        await store.aput(("knowledge", "u1"), "legacy", {"content": "old", "level": 1})
        unit = EvidenceUnit(
            chunk_id="chunk_new",
            document_id="doc_new",
            document_revision_id="rev_new",
            content="new",
            retrieval_text="Content:\nnew",
            chunk_index=0,
            source_span=SourceSpan(start=0, end=3),
        )
        await put_evidence_units(store, "u1", [unit])
        items = await store.asearch(("knowledge", "u1"), limit=10)
        entries = {item.key: evidence_from_item(item) for item in items}
        self.assertTrue(entries["legacy"].legacy)
        self.assertFalse(entries["chunk_new"].legacy)
        self.assertEqual(entries["chunk_new"].atomicity, "atomic")
        self.assertEqual(entries["chunk_new"].document_id, "doc_new")


if __name__ == "__main__":
    unittest.main()
