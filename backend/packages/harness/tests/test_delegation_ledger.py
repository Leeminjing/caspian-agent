import unittest

from langchain.messages import AIMessage, ToolMessage

from caspian.agents.lead_agent_state import DelegationEntry, merge_delegations
from caspian.agents.middlewares.delegation_ledger import (
    extract_delegations,
    render_delegation_ledger,
)
from caspian.subagents.status_contract import make_subagent_additional_kwargs


def _entry(id, status, **extra):
    entry: DelegationEntry = {
        "id": id,
        "description": "desc",
        "subagent_type": "general-purpose",
        "status": status,
        "created_at": "2026-08-06T00:00:00Z",
    }
    entry.update(extra)
    return entry


class MergeDelegationsTests(unittest.TestCase):
    def test_append_new_entry(self):
        merged = merge_delegations([_entry("a", "in_progress")], [_entry("b", "in_progress")])
        self.assertEqual([e["id"] for e in merged], ["a", "b"])

    def test_same_id_in_place_update(self):
        merged = merge_delegations(
            [_entry("a", "in_progress"), _entry("b", "in_progress")],
            [_entry("a", "completed")],
        )
        self.assertEqual([e["id"] for e in merged], ["a", "b"])
        self.assertEqual(merged[0]["status"], "completed")

    def test_terminal_not_overwritten_by_non_terminal(self):
        merged = merge_delegations(
            [_entry("a", "completed")],
            [_entry("a", "in_progress")],
        )
        self.assertEqual(merged[0]["status"], "completed")

    def test_first_seen_order_preserved(self):
        merged = merge_delegations(
            [_entry("a", "in_progress"), _entry("b", "in_progress")],
            [_entry("b", "completed"), _entry("a", "completed")],
        )
        self.assertEqual([e["id"] for e in merged], ["a", "b"])

    def test_empty_new_preserves_existing(self):
        merged = merge_delegations([_entry("a", "in_progress")], None)
        self.assertEqual(len(merged), 1)


class ExtractDelegationsTests(unittest.TestCase):
    def test_rebuild_with_completed_result(self):
        ai = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "task",
                    "args": {"description": "调研", "prompt": "p", "subagent_type": "general-purpose"},
                    "id": "tc-1",
                    "type": "tool_call",
                }
            ],
        )
        tool = ToolMessage(
            content="Task Succeeded. Result: 完成",
            tool_call_id="tc-1",
            name="task",
            additional_kwargs=make_subagent_additional_kwargs("completed", result="完成"),
        )
        entries = extract_delegations([ai, tool])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "completed")
        self.assertEqual(entries[0]["description"], "调研")
        self.assertIn("完成", entries[0]["result_brief"])

    def test_unpaired_call_stays_in_progress(self):
        ai = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "task",
                    "args": {"description": "调研", "prompt": "p", "subagent_type": "bash"},
                    "id": "tc-1",
                    "type": "tool_call",
                }
            ],
        )
        entries = extract_delegations([ai])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "in_progress")
        self.assertEqual(entries[0]["subagent_type"], "bash")


class RenderLedgerTests(unittest.TestCase):
    def test_empty_ledger_renders_empty(self):
        self.assertEqual(render_delegation_ledger([]), "")

    def test_renders_newest_first_with_guidance(self):
        # 最新条目（b，进行中）应渲染在最前；条目 id 不渲染，用独立 description 定位
        rendered = render_delegation_ledger(
            [
                _entry("a", "completed", description="最早任务", result_brief="结果A"),
                _entry("b", "in_progress", description="最新任务"),
            ]
        )
        self.assertIn("## Work already delegated", rendered)
        self.assertLess(rendered.index("最新任务"), rendered.index("结果A"))
        self.assertIn("do NOT delegate again", rendered)

    def test_budget_omission(self):
        entries = [_entry(f"d{i}", "completed", result_brief="r" * 500) for i in range(60)]
        rendered = render_delegation_ledger(entries, max_chars=2000)
        self.assertIn("omitted", rendered)
        self.assertLessEqual(len(rendered), 2000 + 4)

    def test_html_escaping(self):
        rendered = render_delegation_ledger([_entry("a", "in_progress", description="<script>")])
        self.assertIn("&lt;script&gt;", rendered)


if __name__ == "__main__":
    unittest.main()
