"""
本文件提供决策等级表检测模块（decision_table_detect）的标准库 unittest。

输入:
    DecisionRow 构造的候选与旧表，以及可注入的假模型

输出:
    可运行检查，覆盖语义冲突解析、等级裁决（降级/升级/同级/未声明）与检测通过放行
"""

import unittest

from caspian.agents.commitment.decision_table import DecisionRow
from caspian.agents.commitment.decision_table_detect import (
    DecisionTableAction,
    DecisionTableConflict,
    _adjudicate_conflicts,
    _parse_semantic_conflicts,
)


class TestParseSemanticConflicts(unittest.TestCase):
    def test_parses_and_filters_unknown_existing(self):
        known = {"必须使用 Supabase"}
        raw = {
            "conflicts": [
                {"candidate": "改用 Supabase", "existing": "必须使用 Supabase", "relation": "explicit", "explanation": "改词冲突"},
                {"candidate": "X", "existing": "不存在条目", "relation": "explicit"},
                {"candidate": "Y", "existing": "必须使用 Supabase", "relation": "invalid_relation"},
            ]
        }
        result = _parse_semantic_conflicts(raw, known)
        # 第一条保留；第二条 existing 不在 known → 过滤；第三条 relation 非法 → 归一为 explicit
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].relation, "explicit")
        self.assertEqual(result[1].relation, "explicit")


class TestAdjudicateConflicts(unittest.TestCase):
    def _existing(self):
        return [DecisionRow(requirement="必须使用 Supabase", decision="保留", priority=3)]

    def test_no_conflict_passes(self):
        verdict = _adjudicate_conflicts([], self._existing(), [])
        self.assertEqual(verdict.action, DecisionTableAction.COMMIT)
        self.assertEqual(verdict.conflicts, [])

    def test_downgrade_requires_confirm(self):
        # 候选等级 1 < 旧表 3 → 降级冲突 → 走 CONFIRM（中断，用户二选一）
        conflicts = [
            DecisionTableConflict(candidate="必须使用 Supabase", existing="必须使用 Supabase", relation="explicit"),
        ]
        candidate = [DecisionRow(requirement="必须使用 Supabase", decision="保留", priority=1)]
        verdict = _adjudicate_conflicts(conflicts, self._existing(), candidate)
        self.assertEqual(verdict.action, DecisionTableAction.CONFIRM)
        self.assertTrue(any("降级" in reason for reason in verdict.reasons))

    def test_escalation_requires_confirm(self):
        # 候选等级 3 >= 旧表 3（同级）→ 需人工确认
        conflicts = [
            DecisionTableConflict(candidate="必须使用 Supabase", existing="必须使用 Supabase", relation="explicit"),
        ]
        candidate = [DecisionRow(requirement="必须使用 Supabase", decision="保留", priority=3)]
        verdict = _adjudicate_conflicts(conflicts, self._existing(), candidate)
        self.assertEqual(verdict.action, DecisionTableAction.CONFIRM)

    def test_potential_only_not_confirmed(self):
        # 仅 potential 且无 explicit → 不阻断（不确定不压制），直接提交
        conflicts = [
            DecisionTableConflict(candidate="必须使用 Supabase", existing="必须使用 Supabase", relation="potential"),
        ]
        candidate = [DecisionRow(requirement="必须使用 Supabase", decision="保留", priority=3)]
        verdict = _adjudicate_conflicts(conflicts, self._existing(), candidate)
        self.assertEqual(verdict.action, DecisionTableAction.COMMIT)


if __name__ == "__main__":
    unittest.main()
