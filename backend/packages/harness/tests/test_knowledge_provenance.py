"""
本文件提供等级来源派生（provenance）与内容去重/CAS 的 unittest。

对外提供:
    ProvenanceTests — classify_level 域名派生与 extract_domain 测试
    StoreClientTests — put_knowledge 去重 upsert 与 update_provenance CAS 测试

输入: 无 — 测试内以内存 dict 模拟 store
输出: unittest 测试结果

示例:
    python -m unittest tests.test_knowledge_provenance
"""

import unittest

from caspian.knowledge.provenance import classify_level, extract_domain
from caspian.knowledge.store_client import (
    ProvenanceUpdateStatus,
    put_knowledge,
    update_provenance,
)

_DOMAINS = {"docs.example.com": 3, "blog.example.com": 2}


class _FakeStore:
    def __init__(self):
        self._data = {}

    async def aput(self, namespace, key, value):
        self._data[(tuple(namespace), key)] = value

    async def aget(self, namespace, key):
        class _Item:
            def __init__(self, value):
                self.value = value

        value = self._data.get((tuple(namespace), key))
        return _Item(value) if value is not None else None


class ProvenanceTests(unittest.TestCase):

    def test_域名派生等级(self):
        self.assertEqual(
            classify_level("https://docs.example.com/x", _DOMAINS),
            (3, "official", "docs.example.com"),
        )

    def test_未命中域名为未评级(self):
        self.assertEqual(
            classify_level("https://other.com/x", _DOMAINS),
            (None, "unknown", "other.com"),
        )

    def test_无链接为未评级(self):
        self.assertEqual(classify_level(None, _DOMAINS), (None, "unknown", None))

    def test_提取域名(self):
        self.assertEqual(extract_domain("https://Docs.Example.com/x"), "docs.example.com")
        self.assertIsNone(extract_domain(None))


class StoreClientTests(unittest.IsolatedAsyncioTestCase):

    async def test_内容哈希去重同内容upsert(self):
        store = _FakeStore()
        key1, level1 = await put_knowledge(
            store, "u1", "功能 A 已废弃。", source="官方",
            source_url="https://docs.example.com/x", domains=_DOMAINS,
        )
        key2, level2 = await put_knowledge(
            store, "u1", "功能 A 已废弃。", source="官方2",
            source_url="https://blog.example.com/x", domains=_DOMAINS,
        )
        self.assertEqual(key1, key2)
        self.assertEqual(level1, 3)
        self.assertEqual(level2, 2)  # upsert 更新等级
        self.assertEqual(len(store._data), 1)

    async def test_CAS修改成功(self):
        store = _FakeStore()
        key, _ = await put_knowledge(
            store, "u1", "内容 X", source_url="https://docs.example.com/x", domains=_DOMAINS,
        )
        status = await update_provenance(store, "u1", key, level_override=1, expected_level=3)
        self.assertIs(status, ProvenanceUpdateStatus.OK)
        item = await store.aget(("knowledge", "u1"), key)
        self.assertEqual(item.value["level"], 1)

    async def test_CAS冲突拒绝(self):
        store = _FakeStore()
        key, _ = await put_knowledge(
            store, "u1", "内容 X", source_url="https://docs.example.com/x", domains=_DOMAINS,
        )
        status = await update_provenance(store, "u1", key, level_override=1, expected_level=2)
        self.assertIs(status, ProvenanceUpdateStatus.CONFLICT)
        item = await store.aget(("knowledge", "u1"), key)
        self.assertEqual(item.value["level"], 3)

    async def test_未找到返回NOT_FOUND(self):
        store = _FakeStore()
        status = await update_provenance(store, "u1", "missing", level_override=1)
        self.assertIs(status, ProvenanceUpdateStatus.NOT_FOUND)


if __name__ == "__main__":
    unittest.main()
