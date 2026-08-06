import unittest
from types import SimpleNamespace

from langchain.messages import AIMessage

from caspian.agents.middlewares.subagent_limit_middleware import SubagentLimitMiddleware


def _ai_message(task_count, extra_tool=False):
    tool_calls = [
        {
            "name": "task",
            "args": {"description": f"task-{i}", "prompt": "p", "subagent_type": "general-purpose"},
            "id": f"task-call-{i}",
            "type": "tool_call",
        }
        for i in range(task_count)
    ]
    if extra_tool:
        tool_calls.append({"name": "read_file_tool", "args": {"path": "/x"}, "id": "read-1", "type": "tool_call"})
    return AIMessage(content="", tool_calls=tool_calls)


def _runtime(run_id="run-1"):
    return SimpleNamespace(context={"run_id": run_id})


def _state(messages, delegations=None):
    return {"messages": messages, "delegations": delegations or []}


class ConcurrencyLimitTests(unittest.TestCase):
    def test_within_limit_unchanged(self):
        mw = SubagentLimitMiddleware(max_concurrent=3, max_total=6)
        result = mw._truncate_task_calls(_state([_ai_message(3)]), _runtime())
        self.assertIsNone(result)

    def test_excess_truncated(self):
        mw = SubagentLimitMiddleware(max_concurrent=3, max_total=6)
        result = mw._truncate_task_calls(_state([_ai_message(5)]), _runtime())
        kept = result["messages"][0].tool_calls
        task_calls = [tc for tc in kept if tc["name"] == "task"]
        self.assertEqual(len(task_calls), 3)
        self.assertEqual(task_calls[0]["id"], "task-call-0")

    def test_non_task_tools_preserved(self):
        mw = SubagentLimitMiddleware(max_concurrent=3, max_total=6)
        result = mw._truncate_task_calls(_state([_ai_message(5, extra_tool=True)]), _runtime())
        names = [tc["name"] for tc in result["messages"][0].tool_calls]
        self.assertIn("read_file_tool", names)

    def test_no_task_calls_unchanged(self):
        mw = SubagentLimitMiddleware(max_concurrent=3, max_total=6)
        msg = AIMessage(content="", tool_calls=[{"name": "bash_tool", "args": {}, "id": "b1", "type": "tool_call"}])
        result = mw._truncate_task_calls(_state([msg]), _runtime())
        self.assertIsNone(result)


class TotalLimitTests(unittest.TestCase):
    def test_total_cap_within_batch(self):
        mw = SubagentLimitMiddleware(max_concurrent=3, max_total=6)
        prior = [
            {"id": "d1", "run_id": "run-1", "description": "a", "subagent_type": "t", "status": "completed", "created_at": "x"},
            {"id": "d2", "run_id": "run-1", "description": "b", "subagent_type": "t", "status": "completed", "created_at": "x"},
        ]
        result = mw._truncate_task_calls(_state([_ai_message(5)], prior), _runtime())
        kept = [tc for tc in result["messages"][0].tool_calls if tc["name"] == "task"]
        self.assertEqual(len(kept), 3)  # allowed = min(3, 6-2) = 3

    def test_total_exhausted_removes_all_and_notes(self):
        mw = SubagentLimitMiddleware(max_concurrent=3, max_total=6)
        prior = [
            {"id": f"d{i}", "run_id": "run-1", "description": "x", "subagent_type": "t", "status": "completed", "created_at": "x"}
            for i in range(6)
        ]
        result = mw._truncate_task_calls(_state([_ai_message(2)], prior), _runtime())
        kept = [tc for tc in result["messages"][0].tool_calls if tc["name"] == "task"]
        self.assertEqual(len(kept), 0)
        self.assertIn("delegation limit has been reached", str(result["messages"][0].content))

    def test_run_id_filtering(self):
        mw = SubagentLimitMiddleware(max_concurrent=3, max_total=2)
        prior = [
            {"id": "other-run", "run_id": "run-9", "description": "x", "subagent_type": "t", "status": "completed", "created_at": "x"}
        ]
        result = mw._truncate_task_calls(_state([_ai_message(3)], prior), _runtime("run-1"))
        kept = [tc for tc in result["messages"][0].tool_calls if tc["name"] == "task"]
        self.assertEqual(len(kept), 2)

    def test_missing_run_id_counts_all_conservatively(self):
        mw = SubagentLimitMiddleware(max_concurrent=3, max_total=6)
        prior = [
            {"id": f"d{i}", "description": "x", "subagent_type": "t", "status": "completed", "created_at": "x"}
            for i in range(5)
        ]
        result = mw._truncate_task_calls(_state([_ai_message(3)], prior), SimpleNamespace(context={}))
        kept = [tc for tc in result["messages"][0].tool_calls if tc["name"] == "task"]
        self.assertEqual(len(kept), 1)  # allowed = min(3, 6-5) = 1


class ClampInMiddlewareTests(unittest.TestCase):
    def test_clamped_values(self):
        mw = SubagentLimitMiddleware(max_concurrent=99, max_total=0)
        self.assertEqual(mw.max_concurrent, 4)
        self.assertEqual(mw.max_total, 1)


if __name__ == "__main__":
    unittest.main()
