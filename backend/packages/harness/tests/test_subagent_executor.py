import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from langchain.tools import tool

from caspian.subagents.config import SubagentConfig
from caspian.subagents.executor import (
    SubagentExecutor,
    SubagentResult,
    SubagentStatus,
    _ensure_isolated_loop,
    _extract_final_result,
    _filter_tools,
    _shutdown_isolated_subagent_loop,
)


@tool
def _dummy_tool_a(x: int = 1) -> str:
    """Dummy tool A."""
    return "a"


@tool
def _dummy_tool_b(x: int = 1) -> str:
    """Dummy tool B."""
    return "b"


class TerminalSingleWriteTests(unittest.TestCase):
    def _result(self):
        return SubagentResult(task_id="t1", trace_id="tr", status=SubagentStatus.RUNNING)

    def test_first_write_wins(self):
        result = self._result()
        self.assertTrue(result.try_set_terminal(SubagentStatus.COMPLETED, result="ok"))
        self.assertEqual(result.status, SubagentStatus.COMPLETED)
        self.assertEqual(result.result, "ok")

    def test_second_write_rejected(self):
        result = self._result()
        result.try_set_terminal(SubagentStatus.COMPLETED, result="ok")
        self.assertFalse(result.try_set_terminal(SubagentStatus.FAILED, error="later"))
        self.assertEqual(result.status, SubagentStatus.COMPLETED)
        self.assertEqual(result.result, "ok")
        self.assertIsNone(result.error)

    def test_non_terminal_argument_raises(self):
        result = self._result()
        with self.assertRaises(ValueError):
            result.try_set_terminal(SubagentStatus.RUNNING)

    def test_all_terminal_statuses(self):
        for status in (SubagentStatus.COMPLETED, SubagentStatus.FAILED,
                       SubagentStatus.CANCELLED, SubagentStatus.TIMED_OUT):
            result = self._result()
            self.assertTrue(result.try_set_terminal(status))


class FilterToolsTests(unittest.TestCase):
    def setUp(self):
        self.tools = [_dummy_tool_a, _dummy_tool_b]

    def test_no_filters_inherits_all(self):
        self.assertEqual(len(_filter_tools(self.tools, None, None)), 2)

    def test_allowlist(self):
        filtered = _filter_tools(self.tools, ["_dummy_tool_a"], None)
        self.assertEqual([t.name for t in filtered], ["_dummy_tool_a"])

    def test_denylist(self):
        filtered = _filter_tools(self.tools, None, ["_dummy_tool_b"])
        self.assertEqual([t.name for t in filtered], ["_dummy_tool_a"])

    def test_task_disallowed_by_default(self):
        config = SubagentConfig(name="x", description="d")
        self.assertIn("task", config.disallowed_tools)


class ExtractFinalResultTests(unittest.TestCase):
    def test_sentinel_on_none(self):
        self.assertEqual(_extract_final_result(None, name="x"), "No response generated")

    def test_sentinel_on_empty(self):
        self.assertEqual(_extract_final_result({"messages": []}, name="x"), "No response generated")

    def test_empty_ai_content_sentinel(self):
        from langchain.messages import AIMessage

        self.assertEqual(
            _extract_final_result({"messages": [AIMessage(content="")]}, name="x"),
            "No response generated",
        )

    def test_extracts_last_ai_message(self):
        from langchain.messages import AIMessage

        state = {"messages": [AIMessage(content="第一轮"), AIMessage(content="完成分析")]}
        self.assertEqual(_extract_final_result(state, name="x"), "完成分析")

    def test_falls_back_to_last_message(self):
        from langchain.messages import HumanMessage

        state = {"messages": [HumanMessage(content="兜底文本")]}
        self.assertEqual(_extract_final_result(state, name="x"), "兜底文本")


class CancelIdempotentTests(unittest.TestCase):
    def test_cancel_event_set_is_idempotent(self):
        result = SubagentResult(task_id="t1", trace_id="tr", status=SubagentStatus.RUNNING)
        result.cancel_event.set()
        result.cancel_event.set()
        self.assertTrue(result.cancel_event.is_set())


class IsolatedLoopTests(unittest.TestCase):
    """隔离事件循环：单例复用 + 同步 execute 路径（父上下文已在事件循环内）。"""

    def tearDown(self):
        _shutdown_isolated_subagent_loop()

    def test_isolated_loop_is_singleton_and_stays_open(self):
        first = _ensure_isolated_loop()
        second = _ensure_isolated_loop()
        self.assertIs(first, second)
        self.assertFalse(first.is_closed())

    def test_execute_inside_running_loop_uses_isolated_loop(self):
        async def scenario():
            executor = SubagentExecutor(
                SubagentConfig(name="x", description="d"),
                [],
                parent_model="deepseek-v4-flash",
            )
            completed = SubagentResult(
                task_id="t1",
                trace_id="tr",
                status=SubagentStatus.COMPLETED,
                result="ok",
            )
            executor._aexecute = AsyncMock(return_value=completed)  # type: ignore[method-assign]
            # 当前协程在事件循环内：execute() 同步路径必须走隔离 loop 而非 asyncio.run
            result = executor.execute("任务")
            return result

        result = asyncio.run(scenario())
        self.assertEqual(result.status, SubagentStatus.COMPLETED)
        self.assertEqual(result.result, "ok")

    def test_contextvars_propagated_to_isolated_loop(self):
        import contextvars

        marker = contextvars.ContextVar("smoke_marker", default="absent")

        async def scenario():
            marker.set("present")
            loop = _ensure_isolated_loop()

            async def probe():
                return marker.get()

            from caspian.subagents.executor import _submit_to_isolated_loop_in_context
            from contextvars import copy_context

            future = _submit_to_isolated_loop_in_context(copy_context(), probe)
            return future.result(timeout=10)

        self.assertEqual(asyncio.run(scenario()), "present")


class CheckpointerFalseTests(unittest.TestCase):
    """subagent 装配 SHALL 传 checkpointer=False（不写父 checkpoint 命名空间）。"""

    def test_create_agent_receives_checkpointer_false(self):
        async def scenario():
            executor = SubagentExecutor(
                SubagentConfig(name="x", description="d"),
                [],
                parent_model="deepseek-v4-flash",
            )
            fake_agent = MagicMock()

            async def fake_stream(*args, **kwargs):
                yield {"messages": []}

            fake_agent.astream = fake_stream
            with (
                patch(
                    "caspian.subagents.executor.create_agent",
                    return_value=fake_agent,
                ) as mock_create,
                patch(
                    "caspian.subagents.executor.get_app_config",
                    return_value=SimpleNamespace(
                        models=[SimpleNamespace(name="deepseek-v4-flash")],
                    ),
                ),
                patch(
                    "caspian.subagents.executor.create_chat_model",
                    return_value=MagicMock(),
                ),
                patch(
                    "caspian.agents.middlewares.builder.build_subagent_middlewares",
                    return_value=[],
                ),
            ):
                result = await executor._aexecute("任务")
            kwargs = mock_create.call_args.kwargs
            return result, kwargs

        result, kwargs = asyncio.run(scenario())
        self.assertEqual(result.status, SubagentStatus.COMPLETED)
        self.assertIs(kwargs["checkpointer"], False)


if __name__ == "__main__":
    unittest.main()
