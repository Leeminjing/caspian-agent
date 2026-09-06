"""
本文件提供 put_knowledge / update_provenance 编排的 unittest（fake store + mock model）。

输入:
    假 store（aput/aget）、mock 评级模型（返回 RatingOutput / 抛错），
    以及 monkeypatch 的 knowledge.rating 配置。

输出:
    可运行检查，覆盖 design.md D2/D6 与 spec requirement 4/5/7：
    黑名单→L0 跳过评级、正常入库→level+level_basis、评级失败→未评级、override/CAS。
"""

import unittest
from unittest.mock import patch

from caspian.config.knowledge_config import RatingConfig
from caspian.knowledge import store_client
from caspian.knowledge.schemas import RatingDimensions, RatingOutput
from caspian.knowledge.store_client import (
    ProvenanceUpdateStatus,
    put_knowledge,
    update_provenance,
)


class _FakeItem:
    def __init__(self, value):
        self.value = value


class _FakeStore:
    def __init__(self):
        self.data = {}

    async def aput(self, ns, key, value):
        self.data[(tuple(ns), key)] = dict(value)

    async def aget(self, ns, key):
        v = self.data.get((tuple(ns), key))
        return _FakeItem(v) if v is not None else None


class _RatingModel:
    """rate_level 的 mock：bind/with_structured_output/ainvoke 三段。"""

    def __init__(self, output=None, fail=False):
        self._output = output
        self._fail = fail
        self.called = False

    def bind(self, max_tokens):
        self.called = True
        return self

    def with_structured_output(self, schema, method="json_mode"):
        if self._fail:
            raise RuntimeError("structured unavailable")
        return self

    async def ainvoke(self, messages):
        self.called = True
        if self._fail:
            raise RuntimeError("model down")
        return self._output


_RATING_CFG = RatingConfig(
    enabled=True, model="test-model", confidence_threshold=0.5, timeout_seconds=60
)


class _StoreClientTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._cfg_patcher = patch.object(
            store_client, "_load_rating_config", return_value=_RATING_CFG
        )
        self._cfg_patcher.start()
        self.addCleanup(self._cfg_patcher.stop)

    def _stored(self, store, key):
        return store.data[(("knowledge", "u1"), key)]


class PutKnowledgeTests(_StoreClientTestBase):

    async def test_blacklist_yields_l0_without_model_call(self):
        store = _FakeStore()
        model = _RatingModel(output=None)  # 不应被调用
        key, level = await put_knowledge(
            store, "u1", "垃圾内容", source="x",
            source_url="https://blacklisted.example.com/x",
            domains={"blacklisted.example.com": 0},
            model=model,
        )
        self.assertEqual(level, 0)
        self.assertFalse(model.called)
        stored = self._stored(store, key)
        self.assertEqual(stored["level"], 0)
        self.assertEqual(stored["level_basis"]["mapping_rule"], "domain blacklist → L0")

    async def test_normal_ingest_yields_level_and_basis(self):
        store = _FakeStore()
        out = RatingOutput(
            claim_domain="PostgreSQL",
            dimensions=RatingDimensions(
                primary_source=2, domain_fit=1, evidence=2, specificity=2
            ),
            confidence=0.82,
            reason="官方博客但非该领域",
        )
        model = _RatingModel(output=out)
        key, level = await put_knowledge(
            store, "u1", "PostgreSQL upsert", source="OpenAI 博客",
            source_url="https://openai.com/blog/x",
            domains={"openai.com": 2},
            model=model,
        )
        self.assertEqual(level, 1)  # domain_fit==1 → cap L1
        stored = self._stored(store, key)
        self.assertEqual(stored["level"], 1)
        self.assertEqual(stored["level_basis"]["dimensions"]["domain_fit"], 1)
        self.assertIn("domain_fit==1", stored["level_basis"]["mapping_rule"])
        self.assertEqual(stored["level_basis"]["rated_by"], "test-model")

    async def test_rating_failure_degrades_to_unrated(self):
        store = _FakeStore()
        model = _RatingModel(output=None, fail=True)
        key, level = await put_knowledge(
            store, "u1", "x", source="s", source_url="https://docs.example.com/x",
            domains={"docs.example.com": 3}, model=model,
        )
        self.assertIsNone(level)
        stored = self._stored(store, key)
        self.assertIsNone(stored["level"])
        self.assertEqual(stored["level_basis"]["reason"], "rater failed")
        self.assertEqual(stored["level_basis"]["mapping_rule"], "rater failed → unrated")


class UpdateProvenanceTests(_StoreClientTestBase):

    def _seed(self):
        store = _FakeStore()
        return store

    async def test_override_sets_level_and_basis(self):
        store = self._seed()
        out = RatingOutput(
            claim_domain="PostgreSQL",
            dimensions=RatingDimensions(primary_source=3, domain_fit=3, evidence=3, specificity=3),
            confidence=0.9,
            reason="官方一手文档",
        )
        key, _ = await put_knowledge(
            store, "u1", "x", source="s", source_url="https://docs.example.com/x",
            domains={"docs.example.com": 3}, model=_RatingModel(output=out),
        )
        status = await update_provenance(store, "u1", key, level_override=2)
        self.assertEqual(status, ProvenanceUpdateStatus.OK)
        stored = self._stored(store, key)
        self.assertEqual(stored["level"], 2)
        self.assertEqual(stored["level_basis"]["mapping_rule"], "level_override")
        self.assertEqual(stored["provenance"]["source_type"], "override")

    async def test_cas_conflict_is_rejected(self):
        store = self._seed()
        out = RatingOutput(
            claim_domain="PostgreSQL",
            dimensions=RatingDimensions(primary_source=3, domain_fit=3, evidence=3, specificity=3),
            confidence=0.9,
            reason="官方一手文档",
        )
        key, _ = await put_knowledge(
            store, "u1", "x", source="s", source_url="https://docs.example.com/x",
            domains={"docs.example.com": 3}, model=_RatingModel(output=out),
        )
        # 期望等级与实际不符 → CONFLICT
        status = await update_provenance(
            store, "u1", key, level_override=1, expected_level=2
        )
        self.assertEqual(status, ProvenanceUpdateStatus.CONFLICT)
        # 等级未被改动
        self.assertEqual(self._stored(store, key)["level"], 3)


if __name__ == "__main__":
    unittest.main()
