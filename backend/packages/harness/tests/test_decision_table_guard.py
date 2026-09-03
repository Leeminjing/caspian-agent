"""
本文件提供决策等级表守卫（guard）匹配引擎与 Guard 模型的纯函数 unittest（不涉及文件读写）。

输入:
    Guard 模型、_extract_texts、_match、_row_violation、_deterministic_collisions 与候选/既有条目

输出:
    可运行检查，覆盖 target 抽取、operator 匹配、forbid/require 方向、Guard 校验与确定性碰撞
"""

import unittest

from caspian.agents.commitment.decision_table import DecisionRow, Guard
from caspian.agents.commitment.decision_table_detect import _deterministic_collisions
from caspian.agents.middlewares.decision_table_guard_middleware import (
    _extract_texts,
    _match,
    _row_violation,
)


class TestGuardFromDict(unittest.TestCase):
    def test_valid_guard(self):
        guard = Guard.from_dict({"kind": "forbid", "target": "shell", "operator": "regex", "pattern": "sqlite"})
        self.assertIsNotNone(guard)
        self.assertEqual(guard.kind, "forbid")

    def test_invalid_kind(self):
        self.assertIsNone(Guard.from_dict({"kind": "bogus", "target": "shell", "operator": "regex", "pattern": "x"}))

    def test_invalid_target(self):
        self.assertIsNone(Guard.from_dict({"kind": "forbid", "target": "bogus", "operator": "regex", "pattern": "x"}))

    def test_invalid_operator(self):
        self.assertIsNone(Guard.from_dict({"kind": "forbid", "target": "shell", "operator": "bogus", "pattern": "x"}))

    def test_uncompilable_regex(self):
        self.assertIsNone(Guard.from_dict({"kind": "forbid", "target": "shell", "operator": "regex", "pattern": "("}))

    def test_empty_pattern(self):
        self.assertIsNone(Guard.from_dict({"kind": "forbid", "target": "shell", "operator": "regex", "pattern": ""}))


class TestExtractTexts(unittest.TestCase):
    def test_shell_command(self):
        self.assertEqual(_extract_texts("bash_tool", {"command": "ls -la"}, "shell"), ["ls -la"])

    def test_file_path(self):
        self.assertEqual(_extract_texts("write_file_tool", {"path": "/mnt/a.sqlite"}, "file_path"), ["/mnt/a.sqlite"])

    def test_file_content(self):
        self.assertEqual(_extract_texts("write_file_tool", {"content": "DATABASE_URL=sqlite"}, "file_content"), ["DATABASE_URL=sqlite"])

    def test_mismatched_tool_returns_empty(self):
        self.assertEqual(_extract_texts("bash_tool", {"command": "ls"}, "file_path"), [])

    def test_knowledge_multi_field(self):
        self.assertEqual(
            _extract_texts("add_knowledge", {"content": "正文", "source": "https://official"}, "knowledge"),
            ["正文", "https://official"],
        )


class TestMatchOperators(unittest.TestCase):
    def test_regex(self):
        self.assertTrue(_match("regex", r"sqlite|better-sqlite3", "pip install better-sqlite3"))

    def test_contains(self):
        self.assertTrue(_match("contains", "sqlite", "use sqlite here"))

    def test_exact(self):
        self.assertTrue(_match("exact", "sqlite", "sqlite"))
        self.assertFalse(_match("exact", "sqlite", "sqlite3"))

    def test_glob(self):
        self.assertTrue(_match("glob", "**/*.sqlite", "a/b/c.sqlite"))


class TestRowViolation(unittest.TestCase):
    def _row(self, kind="forbid", pattern=r"sqlite|better-sqlite3"):
        return DecisionRow(
            requirement="必须使用 Supabase", decision="保留", priority=3, id="R1",
            guards=[Guard(kind=kind, target="shell", operator="regex", pattern=pattern)],
        )

    def test_forbid_hit(self):
        self.assertIsNotNone(_row_violation(self._row(), "bash_tool", {"command": "pip install better-sqlite3"}))

    def test_forbid_miss(self):
        self.assertIsNone(_row_violation(self._row(), "bash_tool", {"command": "npm install react"}))

    def test_require_satisfied(self):
        row = DecisionRow(
            requirement="必须用 supabase", decision="保留", priority=3, id="R2",
            guards=[Guard(kind="require", target="shell", operator="contains", pattern="supabase")],
        )
        self.assertIsNone(_row_violation(row, "bash_tool", {"command": "supabase start"}))

    def test_require_violated(self):
        row = DecisionRow(
            requirement="必须用 supabase", decision="保留", priority=3, id="R2",
            guards=[Guard(kind="require", target="shell", operator="contains", pattern="supabase")],
        )
        self.assertIsNotNone(_row_violation(row, "bash_tool", {"command": "npm start"}))


class TestDeterministicCollisions(unittest.TestCase):
    def test_normalized_collision(self):
        existing = [DecisionRow(requirement="必须使用 Supabase", decision="保留", priority=3, id="a")]
        candidate = [DecisionRow(requirement="必须使用  supabase", decision="保留", priority=1, id="b")]
        conflicts = _deterministic_collisions(candidate, existing)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].relation, "explicit")

    def test_no_collision(self):
        existing = [DecisionRow(requirement="必须使用 Supabase", decision="保留", priority=3, id="a")]
        candidate = [DecisionRow(requirement="使用 SQLite", decision="保留", priority=1, id="b")]
        self.assertEqual(_deterministic_collisions(candidate, existing), [])


if __name__ == "__main__":
    unittest.main()
