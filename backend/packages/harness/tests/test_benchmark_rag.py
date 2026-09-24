"""
本文件对外提供 RAG 治理、Evidence Unit 完整性与报告的离线回归测试。

输入:
    内置治理语料、Evidence Unit fixture、故障注入 fixture 和确定性 token counter。

输出:
    unittest/pytest 可运行断言，覆盖四臂治理、身份/span/overlap、检索字段隔离、
    更新后等级裁决、无关单元稳定性及报告指标。

具体工作流:
    加载 YAML 并运行纯函数 benchmark；所有治理复验复用生产 govern，且不调用网络、
    真实 embedding 或 LLM。

示例:
    python -m pytest backend/packages/harness/tests/test_benchmark_rag.py -q
"""

from __future__ import annotations

import unittest
from pathlib import Path

from caspian.benchmarks.rag.arms import (
    arm_level_governed,
    arm_plain,
    arm_score_based,
    arm_source_count,
)
from caspian.benchmarks.rag.conflictqa import _DATA
from caspian.benchmarks.rag.evidence_integrity import (
    evidence_integrity_metrics,
    governance_isolation_metrics,
    retrieval_text_isolated,
)
from caspian.benchmarks.rag.oracle import correct_info_retained, wrong_info_adopted
from caspian.benchmarks.rag.schema import (
    EvidenceUnitCorpus,
    EvidenceUnitFixture,
    RagCandidate,
    RagConflict,
    RagItem,
    load_evidence_unit_corpus,
    load_rag_corpus,
)
from caspian.benchmarks.rag.runner import run_all
from caspian.benchmarks.rag.report import render_rag_report


def _item() -> RagItem:
    return RagItem(
        id="t",
        query="q",
        candidates=[
            RagCandidate(id="a", content="正确", level=3, score=0.5, source_count=1),
            RagCandidate(id="b", content="错误", level=1, score=0.9, source_count=5),
        ],
        conflicts=[RagConflict(a="a", b="b", relation="explicit")],
        ground_truth="a",
    )


class TestArms(unittest.TestCase):
    def test_plain_keeps_both(self):
        final = arm_plain(_item())
        self.assertEqual(final, {"a", "b"})
        self.assertTrue(wrong_info_adopted(_item(), final))
        self.assertTrue(correct_info_retained(_item(), final))

    def test_score_based_keeps_wrong_drops_correct(self):
        final = arm_score_based(_item())
        self.assertEqual(final, {"b"})
        self.assertTrue(wrong_info_adopted(_item(), final))
        self.assertFalse(correct_info_retained(_item(), final))

    def test_source_count_keeps_wrong_drops_correct(self):
        final = arm_source_count(_item())
        self.assertEqual(final, {"b"})
        self.assertTrue(wrong_info_adopted(_item(), final))
        self.assertFalse(correct_info_retained(_item(), final))

    def test_level_governed_keeps_correct_drops_wrong(self):
        final = arm_level_governed(_item())
        self.assertEqual(final, {"a"})
        self.assertFalse(wrong_info_adopted(_item(), final))
        self.assertTrue(correct_info_retained(_item(), final))


class TestCorpus(unittest.TestCase):
    def test_corpus_loads_20(self):
        path = Path(__file__).resolve().parents[1] / "caspian" / "benchmarks" / "rag" / "corpus.yaml"
        items = load_rag_corpus(path)
        self.assertEqual(len(items), 20)
        for item in items:
            truth = next(c for c in item.candidates if c.id == item.ground_truth)
            wrong = [c for c in item.candidates if c.id != item.ground_truth]
            for w in wrong:
                self.assertLess(w.level, truth.level)

    def test_invalid_ground_truth_raises(self):
        import tempfile
        import yaml

        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
            yaml.safe_dump(
                {"items": [{
                    "id": "x", "query": "q",
                    "candidates": [
                        {"id": "a", "content": "A", "level": 3, "score": 0.5},
                        {"id": "b", "content": "B", "level": 1, "score": 0.9},
                    ],
                    "conflicts": [{"a": "a", "b": "b"}],
                    "ground_truth": "ghost",
                }]},
                f,
                allow_unicode=True,
            )
            name = f.name
        try:
            with self.assertRaises(ValueError):
                load_rag_corpus(name)
        finally:
            Path(name).unlink(missing_ok=True)


class TestEvidenceUnitIntegrity(unittest.TestCase):
    def _path(self):
        return Path(__file__).resolve().parents[1] / "caspian" / "benchmarks" / "rag" / "evidence_units.yaml"

    def test_fixture_has_zero_contract_violations(self):
        corpus = load_evidence_unit_corpus(self._path())
        metrics = evidence_integrity_metrics(corpus, lambda text: len(text.split()))
        self.assertTrue(metrics["passed"])
        self.assertEqual(metrics["identity_collisions"], 0)
        self.assertEqual(metrics["source_overwrites"], 0)
        self.assertEqual(metrics["overlap_violations"], 0)
        self.assertEqual(metrics["hard_max_violations"], 0)
        self.assertEqual(metrics["invalid_span_acceptances"], 0)
        self.assertEqual(metrics["governance_metadata_embedding_violations"], 0)
        self.assertEqual(metrics["governance_level_usage_violations"], 0)
        self.assertEqual(metrics["unrelated_unit_mutations"], 0)
        self.assertEqual(metrics["full_conflicts"], 2)
        self.assertEqual(metrics["partial_conflicts"], 1)

    def test_governance_metadata_does_not_change_embedding_text(self):
        corpus = load_evidence_unit_corpus(self._path())
        unit = next(item for item in corpus.units if item.chunk_id == corpus.governance_probe.changed_id)
        changed = EvidenceUnitFixture(**{**unit.__dict__, "level": 0, "level_basis": {"reason": "changed"}, "provenance": {"source_count": 99}})
        self.assertTrue(retrieval_text_isolated(unit))
        self.assertTrue(retrieval_text_isolated(changed))
        self.assertEqual(unit.retrieval_text, changed.retrieval_text)

    def test_governance_probe_uses_changed_level_and_preserves_unrelated_unit(self):
        metrics = governance_isolation_metrics(load_evidence_unit_corpus(self._path()))
        self.assertEqual(metrics["governance_metadata_embedding_violations"], 0)
        self.assertEqual(metrics["governance_level_usage_violations"], 0)
        self.assertEqual(metrics["unrelated_unit_mutations"], 0)

    def test_fault_injection_triggers_each_integrity_counter(self):
        unit = load_evidence_unit_corpus(self._path()).units[0]
        collided = EvidenceUnitFixture(**{**unit.__dict__, "source": "Other", "document_id": "doc_other"})
        overwritten = EvidenceUnitFixture(**{**unit.__dict__, "source": "Other"})
        overlapping = EvidenceUnitFixture(**{**unit.__dict__, "chunk_id": "overlap", "source_span": (1, 19)})
        oversized = EvidenceUnitFixture(**{**unit.__dict__, "chunk_id": "huge", "content": " ".join(["x"] * 601), "document_content": " ".join(["x"] * 601), "source_span": (0, 1201), "retrieval_text": "Content:\n" + " ".join(["x"] * 601)})
        bad_span = EvidenceUnitFixture(**{**unit.__dict__, "chunk_id": "bad", "source_span": (1, 19)})
        leak = EvidenceUnitFixture(**{**unit.__dict__, "chunk_id": "leak", "retrieval_text": unit.retrieval_text + "\nLevel: 3"})
        corpus = EvidenceUnitCorpus([unit, collided, overwritten, overlapping, oversized, bad_span, leak], [])
        metrics = evidence_integrity_metrics(corpus, lambda text: len(text.split()))
        self.assertGreater(metrics["identity_collisions"], 0)
        self.assertGreater(metrics["source_overwrites"], 0)
        self.assertGreater(metrics["overlap_violations"], 0)
        self.assertGreater(metrics["hard_max_violations"], 0)
        self.assertGreater(metrics["invalid_span_acceptances"], 0)
        self.assertGreater(metrics["embedding_isolation_violations"], 0)

    def test_report_includes_evidence_unit_metrics(self):
        report = render_rag_report(run_all())
        self.assertIn("Evidence Unit 完整性", report)
        self.assertIn("身份碰撞", report)
        self.assertIn("治理 metadata 向量污染", report)
        self.assertIn("治理等级使用违规", report)
        self.assertIn("无关单元变更", report)
        self.assertIn("partial conflict", report)


class TestConflictQA(unittest.TestCase):
    @unittest.skipUnless(
        _DATA.exists(),
        "ConflictQA 数据集未提供(data/conflictqa-popqa-chatgpt.json 缺失),跳过真实数据用例",
    )
    def test_loads_real_data_with_structure(self):
        from caspian.benchmarks.rag.conflictqa import load_conflictqa

        items = load_conflictqa()
        self.assertGreater(len(items), 7000)
        item = items[0]
        self.assertEqual(len(item.candidates), 2)
        self.assertEqual(item.ground_truth, "correct")
        self.assertTrue(item.answers)
        self.assertIn(item.candidates[0].level, (1, 3))
        self.assertIn(item.candidates[1].level, (1, 3))


if __name__ == "__main__":
    unittest.main()
