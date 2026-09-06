"""
本文件提供入库评级器的标准库 unittest：硬映射矩阵、decide_level 三出口、rate_level mock 三路径。

输入:
    RatingDimensions / RatingOutput 构造的维度分，与各类 FakeModel 桩（结构化正常 / 回退纯文本 / 坏 JSON）

输出:
    可运行检查，覆盖 design.md D4/D6：每条否决规则、no-cap→L3、组合封顶、未评级三出口、评级器两段式。
"""

import unittest

from langchain_core.messages import AIMessage
from pydantic import ValidationError

from caspian.knowledge.rating import (
    decide_level,
    map_dimensions_to_level,
    rate_level,
)
from caspian.knowledge.schemas import RatingDimensions, RatingOutput


def _dim(**kw):
    defaults = dict(primary_source=3, domain_fit=3, evidence=3, specificity=3)
    defaults.update(kw)
    return RatingDimensions(**defaults)


class MappingMatrixTests(unittest.TestCase):

    def test_no_cap_yields_l3(self):
        level, rule = map_dimensions_to_level(_dim())
        self.assertEqual(level, 3)
        self.assertEqual(rule, "no cap → L3")

    def test_primary_source_1_caps_l1(self):
        self.assertEqual(map_dimensions_to_level(_dim(primary_source=1))[0], 1)

    def test_primary_source_2_caps_l2(self):
        self.assertEqual(map_dimensions_to_level(_dim(primary_source=2))[0], 2)

    def test_domain_fit_1_caps_l1(self):
        self.assertEqual(map_dimensions_to_level(_dim(domain_fit=1))[0], 1)

    def test_domain_fit_2_caps_l2(self):
        self.assertEqual(map_dimensions_to_level(_dim(domain_fit=2))[0], 2)

    def test_evidence_1_caps_l1(self):
        self.assertEqual(map_dimensions_to_level(_dim(evidence=1))[0], 1)

    def test_specificity_1_caps_l2(self):
        self.assertEqual(map_dimensions_to_level(_dim(specificity=1))[0], 2)

    def test_openai_blog_about_postgres_caps_l1(self):
        # primary_source=2(间接), domain_fit=1(领域外) → 最严格封顶 L1
        level, rule = map_dimensions_to_level(
            _dim(primary_source=2, domain_fit=1)
        )
        self.assertEqual(level, 1)
        self.assertIn("domain_fit==1", rule)

    def test_combination_takes_most_restrictive(self):
        # primary_source=2(cap L2) + specificity=1(cap L2) → L2；加 evidence=1 → L1
        self.assertEqual(
            map_dimensions_to_level(_dim(primary_source=2, specificity=1))[0], 2
        )
        self.assertEqual(
            map_dimensions_to_level(_dim(evidence=1, specificity=1))[0], 1
        )


class DecideLevelTests(unittest.TestCase):

    def _rating(self, **kw):
        defaults = dict(
            claim_domain="PostgreSQL",
            dimensions=_dim(),
            confidence=0.9,
            reason="官方文档",
            unrated_reason=None,
        )
        defaults.update(kw)
        return RatingOutput(**defaults)

    def test_normal_yields_mapped_level(self):
        level, rule = decide_level(self._rating())
        self.assertEqual(level, 3)
        self.assertEqual(rule, "no cap → L3")

    def test_unrated_reason_yields_none(self):
        level, rule = decide_level(self._rating(unrated_reason="信息不足"))
        self.assertIsNone(level)
        self.assertIn("unrated", rule)

    def test_confidence_below_threshold_yields_none(self):
        level, rule = decide_level(self._rating(confidence=0.3), confidence_threshold=0.5)
        self.assertIsNone(level)
        self.assertIn("confidence", rule)

    def test_confidence_equal_threshold_is_not_unrated(self):
        level, _ = decide_level(self._rating(confidence=0.5), confidence_threshold=0.5)
        self.assertIsNotNone(level)


class SchemaValidationTests(unittest.TestCase):

    def test_dimensions_reject_out_of_range(self):
        with self.assertRaises(ValidationError):
            RatingDimensions(primary_source=4, domain_fit=3, evidence=3, specificity=3)
        with self.assertRaises(ValidationError):
            RatingDimensions(primary_source=0, domain_fit=3, evidence=3, specificity=3)


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


class RateLevelTests(unittest.IsolatedAsyncioTestCase):

    async def _call(self, model):
        return await rate_level(
            content="PostgreSQL 支持 ON CONFLICT 做 upsert。",
            source="官方文档",
            source_url="https://docs.example.com/x",
            mechanical={"source_type": "official", "matched_domain": "docs.example.com"},
            model=model,
        )

    async def test_structured_success(self):
        out = RatingOutput(
            claim_domain="PostgreSQL",
            dimensions=_dim(domain_fit=3, evidence=3),
            confidence=0.95,
            reason="官方一手文档",
        )
        result = await self._call(_ModelStub(structured_output=out))
        self.assertIsInstance(result, RatingOutput)
        self.assertEqual(result.dimensions.primary_source, 3)

    async def test_structured_failure_falls_back_to_plain_text(self):
        plain = (
            '{"claim_domain": "PostgreSQL", '
            '"dimensions": {"primary_source": 2, "domain_fit": 1, "evidence": 2, "specificity": 2}, '
            '"confidence": 0.82, "reason": "官方博客但非该领域", "unrated_reason": null}'
        )
        result = await self._call(_ModelStub(structured_output=None, plain_text=plain))
        self.assertEqual(result.dimensions.domain_fit, 1)
        self.assertIsNone(result.unrated_reason)

    async def test_both_paths_fail_raises(self):
        model = _ModelStub(structured_output=None, plain_text="模型输出的不是 JSON")
        with self.assertRaises(Exception):
            await self._call(model)


class RatingBenchmarkAxisTests(unittest.IsolatedAsyncioTestCase):

    async def test_axis_runs_and_reports_per_item_accuracy(self):
        from caspian.benchmarks.rag.rating import run_rating_accuracy

        # mock 恒返回 L3（全维度 3）→ 只有 expected==3 的样本被判对
        out = RatingOutput(
            claim_domain="PostgreSQL",
            dimensions=_dim(),
            confidence=0.9,
            reason="官方一手文档",
        )
        model = _ModelStub(structured_output=out)
        data = await run_rating_accuracy(model=model)
        self.assertGreater(data["n"], 0)
        self.assertEqual(data["n"], len(data["per_item"]))
        expected_l3 = sum(1 for p in data["per_item"] if p["expected"] == 3)
        self.assertEqual(data["correct"], expected_l3)


if __name__ == "__main__":
    unittest.main()
