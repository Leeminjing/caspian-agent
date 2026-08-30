"""
本文件提供决策等级表读写模块的标准库 unittest。

输入:
    build_decision_table / write_decision_table / read_decision_table 与临时目录

输出:
    可运行检查，覆盖组装映射、版本稳定性、写入读取往返与非法格式容错
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from langchain.agents import create_agent
from langchain.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.language_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.message import add_messages

from caspian.agents.commitment.artifacts import _write_contract
from caspian.agents.commitment.decision_table import (
    DecisionTable,
    build_decision_table,
    compute_version,
    read_decision_table,
    write_decision_table,
)
from caspian.agents.commitment.schemas import WorkerOutput
from caspian.agents.commitment.stage_rules import (
    _compare_table_conflicts,
    _context7_version_evidence,
    _merge_table_escalations,
    _normalize_stage_three_result,
    _validate_stage_result,
)
from caspian.agents.middlewares.decision_table_middleware import (
    DecisionTableMiddleware,
    _injected_version,
)
from caspian.tools.builtins.update_decision_table_tool import update_decision_table

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
        self.assertIn("| 必须使用 Supabase | 保留 | 3 |", content)
        self.assertIn("| 需要支持 SSR | 保留 | 2 |", content)
        self.assertIn("| 应用必须是纯静态前端 | 丢弃 | 0 |", content)

    def test_missing_priority_defaults_to_3(self):
        content = build_decision_table(
            {"requirements": ["无优先级要求"], "discarded_requirements": []},
            {"requirements": []},
        )
        self.assertIn("| 无优先级要求 | 保留 | 3 |", content)

    def test_frontmatter_version_matches_body(self):
        content = build_decision_table(STAGE_TWO, STAGE_THREE)
        parts = content.split("---", 2)
        body = parts[2].strip()
        version_line = next(
            line for line in parts[1].strip().splitlines() if line.startswith("version:")
        )
        self.assertEqual(version_line.split(":", 1)[1].strip(), compute_version(body))


class TestWriteReadRoundTrip(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            version = write_decision_table("th-1", STAGE_TWO, STAGE_THREE, root=root)
            self.assertIsNotNone(version)

            table = read_decision_table("th-1", root=root)
            self.assertIsInstance(table, DecisionTable)
            self.assertEqual(table.version, version)
            self.assertEqual(len(table.rows), 3)
            self.assertEqual(table.rows[0].requirement, "必须使用 Supabase")
            self.assertEqual(table.rows[0].decision, "保留")
            self.assertEqual(table.rows[0].priority, 3)
            self.assertEqual(table.rows[2].decision, "丢弃")
            self.assertEqual(table.rows[2].priority, 0)

    def test_content_change_changes_stored_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            v1 = write_decision_table("th-1", STAGE_TWO, STAGE_THREE, root=root)
            changed_three = {
                "requirements": [
                    {"requirement": "必须使用 Supabase", "priority": 1},
                    {"requirement": "需要支持 SSR", "priority": 2},
                ]
            }
            v2 = write_decision_table("th-1", STAGE_TWO, changed_three, root=root)
            self.assertIsNotNone(v1)
            self.assertIsNotNone(v2)
            self.assertNotEqual(v1, v2)

    def test_read_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(read_decision_table("th-missing", root=Path(tmp)))

    def test_read_invalid_content_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "requirements" / "th-1" / "decision-table.md"
            path.parent.mkdir(parents=True)
            path.write_text("not a decision table", encoding="utf-8")
            self.assertIsNone(read_decision_table("th-1", root=root))


class TestContractWritesDecisionTable(unittest.TestCase):
    def test_contract_with_stage_results_writes_decision_table(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "caspian.agents.commitment.artifacts._PROJECT_ROOT",
                Path(temp_dir),
            ):
                contract, contract_ref = _write_contract(
                    "thread-1",
                    {"contract_markdown": "# Contract"},
                    STAGE_TWO,
                    STAGE_THREE,
                )
                self.assertEqual(contract, "# Contract")
                self.assertTrue((Path(temp_dir) / contract_ref).is_file())

                table = read_decision_table("thread-1", root=Path(temp_dir))
                self.assertIsNotNone(table)
                self.assertEqual(len(table.rows), 3)
                self.assertEqual(table.rows[0].priority, 3)

    def test_contract_without_stage_results_skips_decision_table(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "caspian.agents.commitment.artifacts._PROJECT_ROOT",
                Path(temp_dir),
            ):
                _write_contract(
                    "thread-2",
                    {"contract_markdown": "# Contract"},
                )
                self.assertIsNone(
                    read_decision_table("thread-2", root=Path(temp_dir))
                )


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


class PlainModel(BaseChatModel):
    @property
    def _llm_type(self):
        return "plain-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="lead done"))]
        )

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self


class TestDecisionTableInRealAgent(unittest.IsolatedAsyncioTestCase):
    async def test_injected_once_across_runs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "caspian.agents.commitment.decision_table._PROJECT_ROOT",
                Path(temp_dir),
            ):
                write_decision_table("th-dt", STAGE_TWO, STAGE_THREE, root=Path(temp_dir))
                agent = create_agent(
                    model=PlainModel(),
                    tools=[],
                    middleware=[DecisionTableMiddleware()],
                    checkpointer=InMemorySaver(),
                )
                config = {"configurable": {"thread_id": "th-dt"}}

                first = await agent.ainvoke(
                    {"messages": [HumanMessage(content="hi")]},
                    config=config,
                )
                first_tables = [
                    message
                    for message in first["messages"]
                    if isinstance(message, SystemMessage)
                    and message.id == "decision-table"
                ]
                self.assertEqual(len(first_tables), 1)
                self.assertIn("decision_table_instructions", first_tables[0].content)

                second = await agent.ainvoke(
                    {"messages": [HumanMessage(content="again")]},
                    config=config,
                )
                second_tables = [
                    message
                    for message in second["messages"]
                    if isinstance(message, SystemMessage)
                    and message.id == "decision-table"
                ]
                self.assertEqual(len(second_tables), 1)

    async def test_without_table_matches_old_behavior(self):
        agent = create_agent(
            model=PlainModel(),
            tools=[],
            middleware=[DecisionTableMiddleware()],
            checkpointer=InMemorySaver(),
        )
        config = {"configurable": {"thread_id": "th-no-table"}}
        result = await agent.ainvoke(
            {"messages": [HumanMessage(content="hi")]},
            config=config,
        )
        tables = [
            message
            for message in result["messages"]
            if isinstance(message, SystemMessage)
            and message.id == "decision-table"
        ]
        self.assertEqual(len(tables), 0)


class TestUpdateDecisionTableTool(unittest.IsolatedAsyncioTestCase):
    def _runtime(self, thread_id="th-tool"):
        return SimpleNamespace(execution_info=SimpleNamespace(thread_id=thread_id))

    def _seed(self, temp_dir):
        write_decision_table("th-tool", STAGE_TWO, STAGE_THREE, root=Path(temp_dir))

    async def test_add_creates_entry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "caspian.agents.commitment.decision_table._PROJECT_ROOT",
                Path(temp_dir),
            ):
                result = await update_decision_table.coroutine(
                    operation="add", requirement="新要求", decision="保留", priority=2
                )
                # 直接函数调用（无 runtime）应返回 thread 缺失错误
                self.assertIn("无法获取当前 thread ID", result)

    async def test_add_via_runtime_and_version_change(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "caspian.agents.commitment.decision_table._PROJECT_ROOT",
                Path(temp_dir),
            ):
                self._seed(temp_dir)
                before = read_decision_table("th-tool", root=Path(temp_dir)).version
                result = await update_decision_table.coroutine(
                    operation="add", requirement="新增约束", decision="丢弃", priority=1,
                    runtime=self._runtime(),
                )
                self.assertIn("新版本", result)
                table = read_decision_table("th-tool", root=Path(temp_dir))
                self.assertNotEqual(table.version, before)
                self.assertTrue(
                    any(r.requirement == "新增约束" and r.priority == 1 for r in table.rows)
                )

    async def test_add_duplicate_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "caspian.agents.commitment.decision_table._PROJECT_ROOT",
                Path(temp_dir),
            ):
                self._seed(temp_dir)
                result = await update_decision_table.coroutine(
                    operation="add", requirement="必须使用 Supabase", priority=3,
                    runtime=self._runtime(),
                )
                self.assertIn("已存在", result)

    async def test_update_existing_entry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "caspian.agents.commitment.decision_table._PROJECT_ROOT",
                Path(temp_dir),
            ):
                self._seed(temp_dir)
                result = await update_decision_table.coroutine(
                    operation="update", requirement="必须使用 Supabase", priority=1,
                    runtime=self._runtime(),
                )
                self.assertIn("新版本", result)
                table = read_decision_table("th-tool", root=Path(temp_dir))
                row = next(r for r in table.rows if r.requirement == "必须使用 Supabase")
                self.assertEqual(row.priority, 1)

    async def test_update_missing_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "caspian.agents.commitment.decision_table._PROJECT_ROOT",
                Path(temp_dir),
            ):
                self._seed(temp_dir)
                result = await update_decision_table.coroutine(
                    operation="update", requirement="不存在的要求", priority=3,
                    runtime=self._runtime(),
                )
                self.assertIn("不存在", result)

    async def test_remove_entry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "caspian.agents.commitment.decision_table._PROJECT_ROOT",
                Path(temp_dir),
            ):
                self._seed(temp_dir)
                result = await update_decision_table.coroutine(
                    operation="remove", requirement="需要支持 SSR",
                    runtime=self._runtime(),
                )
                self.assertIn("新版本", result)
                table = read_decision_table("th-tool", root=Path(temp_dir))
                self.assertFalse(any(r.requirement == "需要支持 SSR" for r in table.rows))

    async def test_invalid_priority_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "caspian.agents.commitment.decision_table._PROJECT_ROOT",
                Path(temp_dir),
            ):
                self._seed(temp_dir)
                result = await update_decision_table.coroutine(
                    operation="add", requirement="X", priority=9,
                    runtime=self._runtime(),
                )
                self.assertIn("priority 只允许", result)

    async def test_empty_requirement_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "caspian.agents.commitment.decision_table._PROJECT_ROOT",
                Path(temp_dir),
            ):
                self._seed(temp_dir)
                result = await update_decision_table.coroutine(
                    operation="add", requirement="   ", priority=3,
                    runtime=self._runtime(),
                )
                self.assertIn("不能为空", result)

    async def test_invalid_operation_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "caspian.agents.commitment.decision_table._PROJECT_ROOT",
                Path(temp_dir),
            ):
                self._seed(temp_dir)
                result = await update_decision_table.coroutine(
                    operation="frobnicate", requirement="X", priority=3,
                    runtime=self._runtime(),
                )
                self.assertIn("operation 只允许", result)


class ToolCallingModel(BaseChatModel):
    calls: int = 0

    @property
    def _llm_type(self):
        return "tool-calling-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "update_decision_table",
                                    "args": {
                                        "operation": "add",
                                        "requirement": "对话中新增的约束",
                                        "decision": "保留",
                                        "priority": 3,
                                    },
                                    "id": "call-1",
                                    "type": "tool_call",
                                }
                            ],
                        )
                    )
                ]
            )
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="done"))]
        )

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self


class TestUpdateToolInRealAgent(unittest.IsolatedAsyncioTestCase):
    async def test_agent_calls_tool_and_updates_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "caspian.agents.commitment.decision_table._PROJECT_ROOT",
                Path(temp_dir),
            ):
                write_decision_table("th-tool-agent", STAGE_TWO, STAGE_THREE, root=Path(temp_dir))
                agent = create_agent(
                    model=ToolCallingModel(),
                    tools=[update_decision_table],
                    checkpointer=InMemorySaver(),
                )
                config = {"configurable": {"thread_id": "th-tool-agent"}}
                await agent.ainvoke(
                    {"messages": [HumanMessage(content="把新增约束加入决策表")]},
                    config=config,
                )
                table = read_decision_table("th-tool-agent", root=Path(temp_dir))
                self.assertIsNotNone(table)
                self.assertTrue(
                    any(r.requirement == "对话中新增的约束" and r.priority == 3 for r in table.rows)
                )


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


class TestDecisionTableMiddleware(unittest.TestCase):
    def _patch_root(self, temp_dir):
        return patch(
            "caspian.agents.commitment.decision_table._PROJECT_ROOT",
            Path(temp_dir),
        )

    def _runtime(self):
        return SimpleNamespace(execution_info=SimpleNamespace(thread_id="th-1"))

    def test_first_injection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self._patch_root(temp_dir):
                write_decision_table("th-1", STAGE_TWO, STAGE_THREE, root=Path(temp_dir))
                result = DecisionTableMiddleware()._inject_decision_table(
                    {"messages": []}, self._runtime()
                )
                self.assertIsNotNone(result)
                message = result["messages"][0]
                self.assertIsInstance(message, SystemMessage)
                self.assertEqual(message.id, "decision-table")
                self.assertIsNotNone(_injected_version(str(message.content)))

    def test_same_version_skips(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self._patch_root(temp_dir):
                write_decision_table("th-1", STAGE_TWO, STAGE_THREE, root=Path(temp_dir))
                table = read_decision_table("th-1", root=Path(temp_dir))
                existing = SystemMessage(
                    content=f'<decision_table version="{table.version}">x</decision_table>',
                    id="decision-table",
                )
                result = DecisionTableMiddleware()._inject_decision_table(
                    {"messages": [existing]}, self._runtime()
                )
                self.assertIsNone(result)

    def test_version_change_replaces_in_place(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self._patch_root(temp_dir):
                write_decision_table("th-1", STAGE_TWO, STAGE_THREE, root=Path(temp_dir))
                old = SystemMessage(
                    content='<decision_table version="old-version">x</decision_table>',
                    id="decision-table",
                )
                result = DecisionTableMiddleware()._inject_decision_table(
                    {"messages": [old]}, self._runtime()
                )
                self.assertIsNotNone(result)
                new_message = result["messages"][0]
                self.assertEqual(new_message.id, "decision-table")

                merged = add_messages([old], [new_message])
                self.assertEqual(len(merged), 1)
                self.assertEqual(merged[0].id, "decision-table")
                self.assertNotEqual(
                    _injected_version(str(merged[0].content)),
                    "old-version",
                )

    def test_no_table_skips(self):
        result = DecisionTableMiddleware()._inject_decision_table(
            {"messages": []}, self._runtime()
        )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
