"""
本文件提供离散等级治理引擎 govern 的标准库 unittest 场景矩阵。

输入:
    EvidenceEntry / ConflictRelation 构造的候选与冲突关系

输出:
    可运行检查，覆盖规格 12 节全部治理行为：无冲突保留、部分重叠部分压制、
    跨等级明确冲突压制（相似度/数量不可推翻）、多级冲突最高等级胜出、
    同等级冲突不裁决、不确定冲突不压制、未评级按最低档、查询隔离、账本可解释。
"""

import unittest

from caspian.knowledge.governance import govern
from caspian.knowledge.schemas import ConflictRelation, EvidenceEntry


def _entry(id, content, level, score=None):
    return EvidenceEntry(id=id, content=content, level=level, score=score)


def _status(result, id):
    for item in result.ledger:
        if item.id == id:
            return item
    raise AssertionError(f"账本缺少条目 {id}")


class GovernanceSpecMatrixTests(unittest.TestCase):

    def test_3_1_无冲突时低等级保留(self):
        result = govern(
            [_entry("a", "Python 3.14 支持功能 A。", 3),
             _entry("b", "功能 A 可以这样使用……", 1)],
            [],
        )
        self.assertEqual(_status(result, "a").status, "retained")
        self.assertEqual(_status(result, "b").status, "retained")
        self.assertEqual(len(result.final_evidence_set), 2)
        self.assertEqual(result.notes, [])

    def test_4_1_跨等级明确冲突高等级胜出(self):
        result = govern(
            [_entry("a", "功能 A 已经废弃。", 3),
             _entry("b", "功能 A 仍然推荐使用。", 1)],
            [ConflictRelation(a="a", b="b", relation="explicit", scope="full")],
        )
        self.assertEqual(_status(result, "a").status, "retained")
        suppressed = _status(result, "b")
        self.assertEqual(suppressed.status, "suppressed")
        self.assertIn("更高等级证据 a", suppressed.reason)
        self.assertEqual([e.id for e in result.final_evidence_set], ["a"])

    def test_4_2_相似度不得推翻等级结果(self):
        result = govern(
            [_entry("a", "功能 A 已经废弃。", 3, score=0.61),
             _entry("b", "功能 A 仍然推荐使用。", 1, score=0.98)],
            [ConflictRelation(a="a", b="b", relation="explicit", scope="full")],
        )
        self.assertEqual(_status(result, "a").status, "retained")
        self.assertEqual(_status(result, "b").status, "suppressed")

    def test_4_3_多级冲突最高等级胜出且同结论低等级保留(self):
        result = govern(
            [_entry("a", "A", 3),
             _entry("b", "非A", 2),
             _entry("c", "非A", 1),
             _entry("d", "A", 0)],
            [
                ConflictRelation(a="a", b="b", relation="explicit", scope="full"),
                ConflictRelation(a="a", b="c", relation="explicit", scope="full"),
                ConflictRelation(a="b", b="d", relation="explicit", scope="full"),
                ConflictRelation(a="c", b="d", relation="explicit", scope="full"),
            ],
        )
        self.assertEqual(_status(result, "a").status, "retained")
        self.assertEqual(_status(result, "b").status, "suppressed")
        self.assertEqual(_status(result, "c").status, "suppressed")
        self.assertEqual(_status(result, "d").status, "retained")

    def test_6_多数低等级不得压制高等级(self):
        entries = [_entry("high", "A", 3)]
        conflicts = []
        for i in range(20):
            lid = f"low{i}"
            entries.append(_entry(lid, "非A", 1))
            conflicts.append(
                ConflictRelation(a="high", b=lid, relation="explicit", scope="full")
            )
        result = govern(entries, conflicts)
        self.assertEqual(_status(result, "high").status, "retained")
        for i in range(20):
            self.assertEqual(_status(result, f"low{i}").status, "suppressed")
        self.assertEqual(len(result.final_evidence_set), 1)

    def test_5_1_同等级冲突不裁决(self):
        result = govern(
            [_entry("a", "A", 2), _entry("b", "非A", 2)],
            [ConflictRelation(a="a", b="b", relation="explicit", scope="full")],
        )
        self.assertEqual(_status(result, "a").status, "conflict_same_level")
        self.assertEqual(_status(result, "b").status, "conflict_same_level")
        self.assertEqual(len(result.final_evidence_set), 2)
        self.assertTrue(any("同等级冲突" in note for note in result.notes))

    def test_7_1_可能冲突不压制(self):
        result = govern(
            [_entry("a", "A", 3), _entry("b", "非A", 1)],
            [ConflictRelation(a="a", b="b", relation="potential", scope="full")],
        )
        self.assertEqual(_status(result, "a").status, "potential_conflict")
        self.assertEqual(_status(result, "b").status, "potential_conflict")
        self.assertEqual(len(result.final_evidence_set), 2)
        self.assertTrue(any("潜在分歧" in note for note in result.notes))

    def test_3_2_部分重叠只压制冲突部分(self):
        result = govern(
            [_entry("a", "功能 A 已经废弃。", 3),
             _entry("b", "功能 A 仍然推荐使用。参数格式是 X。", 1)],
            [ConflictRelation(
                a="a", b="b", relation="explicit", scope="partial",
                claim_a="功能 A 已经废弃", claim_b="功能 A 仍然推荐使用",
            )],
        )
        self.assertEqual(_status(result, "a").status, "retained")
        partial = _status(result, "b")
        self.assertEqual(partial.status, "retained_partial")
        self.assertEqual(partial.suppressed_claims, ["功能 A 仍然推荐使用"])
        final_b = next(e for e in result.final_evidence_set if e.id == "b")
        self.assertIn("参数格式是 X", final_b.content)

    def test_1_2_未评级按最低档比较(self):
        result = govern(
            [_entry("a", "A", 1), _entry("b", "非A", None)],
            [ConflictRelation(a="a", b="b", relation="explicit", scope="full")],
        )
        self.assertEqual(_status(result, "a").status, "retained")
        self.assertEqual(_status(result, "b").status, "suppressed")
        self.assertEqual(_status(result, "b").level_display, "未评级")

    def test_9_2_账本可解释(self):
        result = govern(
            [_entry("a", "功能 A 已经废弃。", 3),
             _entry("b", "功能 A 仍然推荐。", 1)],
            [ConflictRelation(a="a", b="b", relation="explicit", scope="full")],
        )
        suppressed = _status(result, "b")
        self.assertEqual(suppressed.level_display, "L1")
        self.assertEqual(suppressed.status, "suppressed")
        self.assertIn("与更高等级证据 a（L3）冲突", suppressed.reason)

    def test_1_3_等级修改改变治理结果(self):
        entries = [_entry("a", "功能 A 已经废弃。", 1),
                   _entry("b", "功能 A 仍然推荐。", 3)]
        conflicts = [ConflictRelation(a="a", b="b", relation="explicit", scope="full")]
        result = govern(entries, conflicts)
        self.assertEqual(_status(result, "a").status, "suppressed")
        # 模拟 PATCH 改等级：a 升为 L3、b 降为 L1
        entries[0].level = 3
        entries[1].level = 1
        result = govern(entries, conflicts)
        self.assertEqual(_status(result, "b").status, "suppressed")
        self.assertEqual(_status(result, "a").status, "retained")

    def test_10_查询之间互不污染(self):
        entries = [_entry("a", "A", 3), _entry("b", "非A", 1)]
        result1 = govern(
            entries,
            [ConflictRelation(a="a", b="b", relation="explicit", scope="full")],
        )
        self.assertEqual(_status(result1, "b").status, "suppressed")
        result2 = govern(entries, [])
        self.assertEqual(_status(result2, "b").status, "retained")
        # 治理结果零持久化：条目对象本身未被修改
        self.assertEqual(entries[1].content, "非A")
        self.assertEqual(entries[1].level, 1)

    def test_无效冲突关系被忽略(self):
        result = govern(
            [_entry("a", "A", 3), _entry("b", "非A", 1)],
            [ConflictRelation(a="a", b="ghost", relation="explicit", scope="full"),
             ConflictRelation(a="a", b="a", relation="explicit", scope="full")],
        )
        self.assertEqual(_status(result, "a").status, "retained")
        self.assertEqual(_status(result, "b").status, "retained")

    def test_被压制者不能再压制他人(self):
        result = govern(
            [_entry("a", "A", 3), _entry("b", "非A", 2), _entry("c", "非A", 1)],
            [ConflictRelation(a="a", b="b", relation="explicit", scope="full"),
             ConflictRelation(a="b", b="c", relation="explicit", scope="full")],
        )
        self.assertEqual(_status(result, "b").status, "suppressed")
        # c 只与被压制的 b 冲突，不被 b 反向压制
        self.assertEqual(_status(result, "c").status, "retained")


class KnowledgeToolFormatTests(unittest.TestCase):
    """knowledge_query 工具的模型文本组装：被压制证据不进模型文本、部分压制带注解。"""

    def _result(self):
        from caspian.knowledge.schemas import (
            FinalEvidence,
            GovernanceResult,
            LedgerEntry,
        )

        return GovernanceResult(
            final_evidence_set=[
                FinalEvidence(id="a", content="功能 A 已经废弃。", level_display="L3"),
                FinalEvidence(
                    id="b",
                    content="功能 A 仍然推荐使用。参数格式是 X。",
                    level_display="L1",
                    suppressed_claims=["功能 A 仍然推荐使用"],
                ),
            ],
            ledger=[
                LedgerEntry(id="a", level_display="L3", status="retained"),
                LedgerEntry(
                    id="b",
                    level_display="L1",
                    status="retained_partial",
                    suppressed_claims=["功能 A 仍然推荐使用"],
                ),
                LedgerEntry(
                    id="c",
                    level_display="L0",
                    status="suppressed",
                    reason="与更高等级证据 a（L3）冲突",
                ),
            ],
        )

    def test_被压制证据不进入模型文本(self):
        from caspian.tools.builtins.knowledge_query_tool import _format_evidence_text

        text = _format_evidence_text(self._result())
        self.assertIn("已经废弃", text)
        self.assertIn("参数格式是 X", text)
        # suppressed 条目的具体原因（含证据 id）只在账本，不进模型文本
        self.assertNotIn("a（L3）冲突", text)

    def test_部分压制命题带注解进入模型文本(self):
        from caspian.tools.builtins.knowledge_query_tool import _format_evidence_text

        text = _format_evidence_text(self._result())
        self.assertIn("功能 A 仍然推荐使用", text)
        self.assertIn("不得作为依据", text)

    def test_存在被压制条目时文本有声明(self):
        from caspian.tools.builtins.knowledge_query_tool import _format_evidence_text

        text = _format_evidence_text(self._result())
        self.assertIn("离散等级治理", text)
        self.assertIn("不得引用其内容", text)


if __name__ == "__main__":
    unittest.main()
