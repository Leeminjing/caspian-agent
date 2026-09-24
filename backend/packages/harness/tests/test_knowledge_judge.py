"""
本文件提供冲突判定 judge 的标准库 unittest（mock LLM）。

输入:
    EvidenceEntry 候选与各类 FakeModel 桩（结构化正常 / 结构化失败回退纯文本 / 坏 JSON）

输出:
    可运行检查，覆盖结构化路径、纯文本兜底路径、坏 JSON 抛错、单候选短路、
    非法关系过滤、去重及 atomicity 驱动的 partial eligibility。
"""

import unittest
from langchain_core.messages import AIMessage

from caspian.knowledge.json_parsing import parse_fenced_or_raw
from caspian.knowledge.judge import (
    _validated_conflicts,
    judge_candidate_payload,
    judge_conflicts,
)
from caspian.knowledge.schemas import EvidenceEntry, JudgeConflictOutput


class _StructuredStub:
    def __init__(self, output):
        self._output = output

    async def ainvoke(self, messages):
        return self._output


class _BindStub:
    def __init__(self, structured_output=None, plain_text=""):
        self._structured_output = structured_output
        self._plain_text = plain_text

    def with_structured_output(self, schema, method="json_mode"):
        if self._structured_output is None:
            raise ValueError("structured output unavailable")
        return _StructuredStub(self._structured_output)

    async def ainvoke(self, messages):
        return AIMessage(content=self._plain_text)


class _ModelStub:
    def __init__(self, structured_output=None, plain_text=""):
        self._structured_output = structured_output
        self._plain_text = plain_text

    def bind(self, max_tokens):
        return _BindStub(self._structured_output, self._plain_text)


class JudgePureHelperTests(unittest.TestCase):

    def test_judge投影只含非权威白名单(self):
        entry = EvidenceEntry(
            id="x",
            content="事实",
            level=3,
            score=0.99,
            source="官方",
            title="API",
            section_path=("参数",),
            version="2",
            published_at="2026-01-01",
            effective_at="2026-02-01",
        )
        payload = judge_candidate_payload(entry)
        self.assertEqual(
            set(payload),
            {"id", "content", "title", "section_path", "version", "published_at", "effective_at", "atomicity"},
        )
        self.assertNotIn("level", payload)
        self.assertNotIn("score", payload)
        self.assertNotIn("source", payload)

    def test_解析fenced_json(self):
        data = parse_fenced_or_raw('```json\n{"conflicts": []}\n```')
        self.assertEqual(data, {"conflicts": []})

    def test_解析raw_json(self):
        data = parse_fenced_or_raw('{"conflicts": [{"a": "x", "b": "y"}]}')
        self.assertEqual(data["conflicts"][0]["a"], "x")

    def test_过滤非法关系(self):
        conflicts = _validated_conflicts(
            [
                {"a": "x", "b": "ghost", "relation": "explicit", "scope": "full"},
                {"a": "x", "b": "x", "relation": "explicit", "scope": "full"},
                {"a": "x", "b": "y", "relation": "weird", "scope": "full"},
                {"a": "x", "b": "y", "relation": "explicit", "scope": "full"},
                {"a": "y", "b": "x", "relation": "potential", "scope": "full"},
            ],
            {"x", "y"},
        )
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].relation, "explicit")

    def test_空span数组归一化为None(self):
        conflicts = _validated_conflicts(
            [{"a": "x", "b": "y", "relation": "explicit", "scope": "full",
              "claim_a": "", "claim_b": "", "claim_a_span": [], "claim_b_span": []}],
            {"x", "y"},
        )
        self.assertEqual(len(conflicts), 1)
        self.assertIsNone(conflicts[0].claim_a_span)
        self.assertIsNone(conflicts[0].claim_b_span)

    def test_接受temporal_disjoint(self):
        conflicts = _validated_conflicts(
            [{"a": "x", "b": "y", "relation": "temporal_disjoint", "scope": "full"}],
            {"x", "y"},
        )
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].relation, "temporal_disjoint")

    def test_partial_only_accepts_proper_subspan_on_indivisible_side(self):
        raw = [{
            "a": "a", "b": "b", "relation": "explicit", "scope": "partial",
            "claim_a": "A", "claim_b": "B1", "claim_a_span": [0, 1], "claim_b_span": [0, 2],
        }]
        accepted = _validated_conflicts(raw, {"a", "b"}, {"a": "A", "b": "B1 B2"}, {"a": "atomic", "b": "indivisible"})
        self.assertEqual((accepted[0].relation, accepted[0].scope), ("explicit", "partial"))
        rejected = _validated_conflicts(raw, {"a", "b"}, {"a": "A", "b": "B1 B2"}, {"a": "atomic", "b": "atomic"})
        self.assertEqual((rejected[0].relation, rejected[0].scope), ("potential", "full"))
        self.assertIsNone(rejected[0].claim_b_span)

    def test_partial_with_full_claims_normalizes_to_full(self):
        conflicts = _validated_conflicts(
            [{"a": "a", "b": "b", "relation": "explicit", "scope": "partial", "claim_a": "A", "claim_b": "B", "claim_a_span": [0, 1], "claim_b_span": [0, 1]}],
            {"a", "b"},
            {"a": "A", "b": "B"},
            {"a": "atomic", "b": "atomic"},
        )
        self.assertEqual(conflicts[0].scope, "full")

    def test_partial_accepts_two_proper_subspans_when_both_units_are_indivisible(self):
        conflicts = _validated_conflicts(
            [{
                "a": "a", "b": "b", "relation": "explicit", "scope": "partial",
                "claim_a": "A1", "claim_b": "B1", "claim_a_span": [0, 2], "claim_b_span": [0, 2],
            }],
            {"a", "b"},
            {"a": "A1 A2", "b": "B1 B2"},
            {"a": "indivisible", "b": "indivisible"},
        )
        self.assertEqual((conflicts[0].relation, conflicts[0].scope), ("explicit", "partial"))


class JudgeTests(unittest.IsolatedAsyncioTestCase):

    async def test_结构化输出正常路径(self):
        model = _ModelStub(
            structured_output=JudgeConflictOutput(
                conflicts=[
                    {"a": "a", "b": "b", "relation": "explicit", "scope": "full"}
                ]
            )
        )
        result = await judge_conflicts(
            [EvidenceEntry(id="a", content="A", level=3),
             EvidenceEntry(id="b", content="非A", level=1)],
            model,
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].relation, "explicit")

    async def test_结构化路径接受metadata时态不相交(self):
        model = _ModelStub(
            structured_output=JudgeConflictOutput(
                conflicts=[{"a": "a", "b": "b", "relation": "temporal_disjoint", "scope": "full"}]
            )
        )
        result = await judge_conflicts(
            [EvidenceEntry(id="a", content="默认值是 20", version="1"), EvidenceEntry(id="b", content="默认值是 30", version="2")],
            model,
        )
        self.assertEqual(result[0].relation, "temporal_disjoint")

    async def test_结构化失败回退纯文本(self):
        model = _ModelStub(
            structured_output=None,
            plain_text='```json\n{"conflicts": [{"a": "a", "b": "b", "relation": "potential", "scope": "partial", "claim_a": "A", "claim_b": "非A"}]}\n```',
        )
        result = await judge_conflicts(
            [EvidenceEntry(id="a", content="A", level=3),
             EvidenceEntry(id="b", content="非A", level=1)],
            model,
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].relation, "potential")
        self.assertEqual(result[0].claim_b, "非A")

    async def test_纯文本路径接受metadata时态不相交(self):
        model = _ModelStub(
            structured_output=None,
            plain_text='{"conflicts":[{"a":"a","b":"b","relation":"temporal_disjoint","scope":"full"}]}',
        )
        result = await judge_conflicts(
            [EvidenceEntry(id="a", content="默认值是 20", effective_at="2025-01-01"), EvidenceEntry(id="b", content="默认值是 30", effective_at="2026-01-01")],
            model,
        )
        self.assertEqual(result[0].relation, "temporal_disjoint")

    async def test_两条路径都失败抛异常(self):
        model = _ModelStub(structured_output=None, plain_text="模型输出的不是 JSON")
        with self.assertRaises(Exception):
            await judge_conflicts(
                [EvidenceEntry(id="a", content="A", level=3),
                 EvidenceEntry(id="b", content="非A", level=1)],
                model,
            )

    async def test_单候选短路不调用模型(self):
        model = _ModelStub(structured_output=None, plain_text="")
        result = await judge_conflicts(
            [EvidenceEntry(id="a", content="A", level=3)], model
        )
        self.assertEqual(result, [])


if __name__ == "__main__":
    import asyncio

    asyncio.run(unittest.main())
