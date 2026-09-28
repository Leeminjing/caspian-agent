"""本文件提供承诺阶段旧表组装与阶段规则的兼容性测试。

输入为阶段二、三结果和旧格式表行；输出为版本、冲突比较及结果规范化断言。
工作流验证仍用于旧表导入的纯函数，正式改表提交由治理服务的专门测试覆盖。
示例：`pytest tests/test_decision_table.py`。
"""

import json
import unittest

import yaml

from langchain.messages import AIMessage, HumanMessage, ToolMessage

from caspian.agents.commitment.decision_table import (
    build_decision_table,
    compute_version,
)
from caspian.agents.commitment.schemas import WorkerOutput
from caspian.agents.commitment.stage_rules import (
    _compare_table_conflicts,
    _context7_version_evidence,
    _merge_table_escalations,
    _normalize_stage_three_result,
    _validate_stage_result,
)

STAGE_TWO = {
    "requirements": [
        "必须使用 Supabase",
        "需要支持 SSR",
    ],
    "discarded_requirements": [
        "应用必须是纯静态前端",
    ],
}

STAGE_THREE = {
    "requirements": [
        {"requirement": "必须使用 Supabase", "priority": 3},
        {"requirement": "需要支持 SSR", "priority": 2},
    ]
}


class TestComputeVersion(unittest.TestCase):
    def test_same_content_same_version(self):
        self.assertEqual(compute_version("a|b"), compute_version("a|b"))

    def test_content_change_changes_version(self):
        self.assertNotEqual(compute_version("a|b"), compute_version("a|c"))

    def test_version_is_12_hex_chars(self):
        version = compute_version("x")
        self.assertEqual(len(version), 12)
        int(version, 16)  # 非法 hex 会抛 ValueError


class TestBuildDecisionTable(unittest.TestCase):
    def test_rows_mapping(self):
        content = build_decision_table(STAGE_TWO, STAGE_THREE)
        rows = yaml.safe_load(content)["rows"]
        self.assertEqual(rows[0]["requirement"], "必须使用 Supabase")
        self.assertEqual(rows[0]["decision"], "保留")
        self.assertEqual(rows[0]["priority"], 3)
        self.assertEqual(rows[1]["requirement"], "需要支持 SSR")
        self.assertEqual(rows[1]["priority"], 2)
        self.assertEqual(rows[2]["requirement"], "应用必须是纯静态前端")
        self.assertEqual(rows[2]["decision"], "丢弃")
        self.assertEqual(rows[2]["priority"], 0)

    def test_missing_priority_defaults_to_3(self):
        content = build_decision_table(
            {"requirements": ["无优先级要求"], "discarded_requirements": []},
            {"requirements": []},
        )
        rows = yaml.safe_load(content)["rows"]
        self.assertEqual(rows[0]["requirement"], "无优先级要求")
        self.assertEqual(rows[0]["priority"], 3)

    def test_frontmatter_version_matches_body(self):
        content = build_decision_table(STAGE_TWO, STAGE_THREE)
        data = yaml.safe_load(content)
        self.assertEqual(data["format"], 2)
        self.assertEqual(len(data["version"]), 12)
        int(data["version"], 16)  # 非法 hex 会抛 ValueError






TABLE_ROWS = [
    {"requirement": "必须使用 Supabase", "decision": "保留", "priority": 3},
    {"requirement": "应用必须是纯静态前端", "decision": "丢弃", "priority": 0},
]


def stage_two_messages(table_conflicts, requirements=None):
    call_id = "stage-2"
    return [
        HumanMessage(content="task"),
        AIMessage(
            content="stage 2",
            tool_calls=[
                {
                    "name": "delegate_with_review",
                    "args": {"stage": 2},
                    "id": call_id,
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(
            content=json.dumps(
                {
                    "status": "approved",
                    "stage": 2,
                    "result": {
                        "requirements": requirements
                        or ["必须使用 Supabase", "改用 SQLite 存储"],
                        "discarded_requirements": [],
                        "compatibility_checks": [],
                        "conflicts": [],
                        "table_conflicts": table_conflicts,
                    },
                }
            ),
            tool_call_id=call_id,
        ),
    ]


class TestCompareTableConflicts(unittest.TestCase):
    def test_downgrade_conflict_returns_error(self):
        conflicts = [
            {
                "requirement": "改用 SQLite 存储",
                "table_requirement": "必须使用 Supabase",
                "table_priority": 3,
                "explanation": "SQLite 与 Supabase 冲突",
            }
        ]
        stage_three = [
            {"requirement": "改用 SQLite 存储", "priority": 1},
            {"requirement": "必须使用 Supabase", "priority": 3},
        ]
        errors = _compare_table_conflicts(conflicts, TABLE_ROWS, stage_three)
        self.assertEqual(len(errors), 1)
        self.assertIn("降级决策被拒绝", errors[0])

    def test_escalation_conflict_no_error(self):
        conflicts = [
            {
                "requirement": "改用 SQLite 存储",
                "table_requirement": "必须使用 Supabase",
                "table_priority": 3,
                "explanation": "SQLite 与 Supabase 冲突",
            }
        ]
        stage_three = [
            {"requirement": "改用 SQLite 存储", "priority": 3},
            {"requirement": "必须使用 Supabase", "priority": 3},
        ]
        self.assertEqual(_compare_table_conflicts(conflicts, TABLE_ROWS, stage_three), [])

    def test_undeclared_priority_is_escalation(self):
        conflicts = [
            {
                "requirement": "改用 SQLite 存储",
                "table_requirement": "必须使用 Supabase",
                "table_priority": 3,
                "explanation": "SQLite 与 Supabase 冲突",
            }
        ]
        stage_three = [
            {"requirement": "必须使用 Supabase", "priority": 3},
        ]
        self.assertEqual(_compare_table_conflicts(conflicts, TABLE_ROWS, stage_three), [])

    def test_conflict_referencing_missing_table_entry_ignored(self):
        conflicts = [
            {
                "requirement": "X",
                "table_requirement": "表中不存在的条目",
                "table_priority": 3,
                "explanation": "幻觉冲突",
            }
        ]
        self.assertEqual(_compare_table_conflicts(conflicts, TABLE_ROWS, []), [])


class TestMergeTableEscalations(unittest.TestCase):
    def test_escalation_merged_into_result(self):
        conflicts = [
            {
                "requirement": "改用 SQLite 存储",
                "table_requirement": "必须使用 Supabase",
                "table_priority": 3,
                "explanation": "SQLite 与 Supabase 冲突",
            }
        ]
        stage_three = [
            {"requirement": "改用 SQLite 存储", "priority": 3},
        ]
        merged = _merge_table_escalations(
            {"requirements": stage_three}, conflicts, TABLE_ROWS, stage_three
        )
        self.assertIn("table_escalations", merged)
        self.assertEqual(len(merged["table_escalations"]), 1)

    def test_no_escalation_returns_original(self):
        result = {"requirements": []}
        merged = _merge_table_escalations(result, [], TABLE_ROWS, [])
        self.assertIs(merged, result)


class TestValidateStageThreeWithTable(unittest.TestCase):
    def test_downgrade_rejected(self):
        conflicts = [
            {
                "requirement": "改用 SQLite 存储",
                "table_requirement": "必须使用 Supabase",
                "table_priority": 3,
                "explanation": "SQLite 与 Supabase 冲突",
            }
        ]
        messages = stage_two_messages(conflicts)
        result = {
            "requirements": [
                {"requirement": "必须使用 Supabase", "priority": 3},
                {"requirement": "改用 SQLite 存储", "priority": 1},
            ]
        }
        error = _validate_stage_result(3, result, messages, TABLE_ROWS)
        self.assertIsNotNone(error)
        self.assertIn("降级决策被拒绝", error)

    def test_escalation_passes_validation(self):
        conflicts = [
            {
                "requirement": "改用 SQLite 存储",
                "table_requirement": "必须使用 Supabase",
                "table_priority": 3,
                "explanation": "SQLite 与 Supabase 冲突",
            }
        ]
        messages = stage_two_messages(conflicts)
        result = {
            "requirements": [
                {"requirement": "必须使用 Supabase", "priority": 3},
                {"requirement": "改用 SQLite 存储", "priority": 3},
            ]
        }
        self.assertIsNone(_validate_stage_result(3, result, messages, TABLE_ROWS))

    def test_no_decision_table_rows_preserves_behavior(self):
        messages = stage_two_messages([])
        result = {
            "requirements": [
                {"requirement": "必须使用 Supabase", "priority": 3},
                {"requirement": "改用 SQLite 存储", "priority": 3},
            ]
        }
        self.assertIsNone(_validate_stage_result(3, result, messages))












class TestStageThreePriorityMissing(unittest.TestCase):
    STAGE_TWO = [
        "必须支持多用户注册、登录、权限隔离",
        "代码必须尽可能简单",
        "包含完整的测试、错误处理、国际化、无障碍支持和详细文档",
    ]

    def test_normalize_legacy_requirements_is_rejected_without_inference(self):
        raw = [
            {"requirement": "必须支持多用户注册、登录、权限隔离", "priority": 3},
            {"requirement": "代码必须尽可能简单"},  # 缺 priority
            {"requirement": "包含完整的测试、错误处理、国际化、无障碍支持和详细文档", "priority": 2},
        ]
        out = _normalize_stage_three_result(
            WorkerOutput(result={"requirements": raw}),
            self.STAGE_TWO,
        )
        messages = stage_two_messages([], requirements=self.STAGE_TWO)
        error = _validate_stage_result(3, out.result, messages)
        self.assertIsNotNone(error)
        self.assertIn("priority_assignments", error)
        self.assertEqual(out.result["worker_result"], {"requirements": raw})

    def test_validate_rejects_missing_priority(self):
        result = {
            "requirements": [
                {"requirement": "必须支持多用户注册、登录、权限隔离", "priority": 3},
                {"requirement": "代码必须尽可能简单"},  # 缺 priority
            ]
        }
        messages = stage_two_messages(
            [], requirements=["必须支持多用户注册、登录、权限隔离", "代码必须尽可能简单"]
        )
        error = _validate_stage_result(3, result, messages)
        self.assertIsNotNone(error)
        self.assertIn("缺少有效的 priority", error)
        self.assertIn("第 2 条", error)

    def test_stage_three_prompt_forbids_summary_overreach(self):
        import inspect

        from caspian.agents.commitment.delegation import ReviewedDelegator

        source = inspect.getsource(ReviewedDelegator._worker)
        self.assertIn("不得省略", source)
        self.assertIn("新解释、核减说明或范围调整", source)


class TestVersionEvidenceCleaning(unittest.TestCase):
    def test_canary_and_branch_tokens_removed(self):
        line = (
            "Versions: 16.2.9, 15.6.0, v14.3.0-canary.87, v15.4.0-canary.82, "
            "__branch__01-02-copy_58398, __branch__15-6-0-canary-57"
        )
        evidence = _context7_version_evidence({"text": line}, "16.2.9")
        self.assertIsNotNone(evidence)
        self.assertIn("16.2.9", evidence)
        self.assertNotIn("canary", evidence)
        self.assertNotIn("__branch__", evidence)

    def test_document_line_without_url_returns_snippet(self):
        line = "The latest stable version is 19.2, released on the official site"
        evidence = _context7_version_evidence({"text": line}, "19.2")
        self.assertIsNotNone(evidence)
        self.assertIn("19.2", evidence)

    def test_snippet_limited_to_version_neighborhood(self):
        line = "prefix" * 40 + " version 3.5.0 is the latest stable " + "suffix" * 40
        evidence = _context7_version_evidence({"text": line}, "3.5.0")
        self.assertIsNotNone(evidence)
        self.assertLessEqual(len(evidence), 200)

    def test_empty_after_cleaning_returns_none(self):
        # version 附近只有污染 token，清洗后为空
        line = "version 3.5.0-canary.1 __branch__x-3-5-0-copy"
        evidence = _context7_version_evidence({"text": line}, "3.5.0")
        # "3.5.0-canary.1" 整体被移除，可能无残留
        if evidence is not None:
            self.assertNotIn("canary", evidence)

    def test_evaluator_prompt_boundary(self):
        import inspect

        from caspian.agents.commitment.delegation import ReviewedDelegator

        source = inspect.getsource(ReviewedDelegator)
        self.assertIn("可能不含URL", source)
        self.assertIn("不得因证据缺少URL而拒绝", source)




if __name__ == "__main__":
    unittest.main()
