"""承诺层 benchmark 机械测试的单元测试(零 LLM)。"""

from __future__ import annotations

import unittest

from caspian.benchmarks.commitment.integrity import (
    test_human_nodes,
    test_injection,
    test_invalid_stage,
    test_stage_sequence,
)


class TestCommitmentIntegrity(unittest.TestCase):
    def test_stage_sequence_strict_1_to_9(self):
        r = test_stage_sequence(3)
        self.assertEqual(r["passed"], r["n"])

    def test_injection_cannot_skip_stages(self):
        r = test_injection()
        self.assertEqual(r["passed"], r["n"])

    def test_human_nodes_all_hit(self):
        r = test_human_nodes(3)
        self.assertEqual(r["passed"], r["n"])

    def test_invalid_stage_rejected(self):
        r = test_invalid_stage()
        self.assertTrue(r["rejected"])


if __name__ == "__main__":
    unittest.main()
