"""分层压制 RAG benchmark 的单元测试(schema / oracle / 治理轴四臂)。"""

from __future__ import annotations

import unittest
from pathlib import Path

from caspian.benchmarks.rag.arms import (
    arm_level_governed,
    arm_plain,
    arm_score_based,
    arm_source_count,
)
from caspian.benchmarks.rag.oracle import correct_info_retained, wrong_info_adopted
from caspian.benchmarks.rag.schema import RagCandidate, RagConflict, RagItem, load_rag_corpus


def _item() -> RagItem:
    """正确方低相似度/低来源数/高等级;错误方高相似度/高来源数/低等级。"""
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
        # 每条错误方都应比正确方更低等级
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


class TestConflictQA(unittest.TestCase):
    def test_loads_real_data_with_structure(self):
        from caspian.benchmarks.rag.conflictqa import load_conflictqa

        items = load_conflictqa()
        self.assertGreater(len(items), 7000)
        item = items[0]
        self.assertEqual(len(item.candidates), 2)
        self.assertEqual(item.ground_truth, "correct")
        self.assertTrue(item.answers)
        # 非循环:level 由事实具体度决定,ground_truth 独立于 level
        self.assertIn(item.candidates[0].level, (1, 3))
        self.assertIn(item.candidates[1].level, (1, 3))


if __name__ == "__main__":
    unittest.main()
