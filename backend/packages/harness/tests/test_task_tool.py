import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("DASHSCOPE_API_KEY", "test-dashscope-key")
os.environ.setdefault("CASPIAN_SANDBOX", "caspian.sandbox.local:LocalSandbox")

from caspian.subagents.config import SubagentConfig  # noqa: E402
from caspian.subagents.executor import SubagentResult, SubagentStatus  # noqa: E402
from caspian.tools.builtins.task_tool import (  # noqa: E402
    is_host_bash_allowed,
    task_tool,
)


def _fake_runtime():
    return SimpleNamespace(
        config={"configurable": {"thread_id": "th-1"}},
        context={"user_id": "u1", "model_name": "deepseek-v4-flash"},
    )


class TaskToolUnknownTypeTests(unittest.TestCase):
    def test_unknown_type_returns_failed(self):
        async def run():
            with patch(
                "caspian.tools.builtins.task_tool.get_subagent_config",
                return_value=None,
            ):
                result = await task_tool.coroutine(
                    runtime=_fake_runtime(),
                    description="测试",
                    prompt="测试任务",
                    subagent_type="missing-type",
                    tool_call_id="tc-1",
                )
            return result

        command = asyncio.run(run())
        message = command.update["messages"][0]
        self.assertEqual(message.name, "task")
        self.assertIn("Unknown subagent type", message.content)
        self.assertEqual(message.additional_kwargs["subagent_status"], "failed")


class BashAvailabilityTests(unittest.TestCase):
    def test_local_sandbox_allows_bash(self):
        app_config = SimpleNamespace(sandbox=SimpleNamespace(use="caspian.sandbox.local:LocalSandbox"))
        self.assertTrue(is_host_bash_allowed(app_config))

    def test_aio_sandbox_allows_bash(self):
        app_config = SimpleNamespace(
            sandbox=SimpleNamespace(use="caspian.community.aio_sandbox:AioSandbox")
        )
        self.assertTrue(is_host_bash_allowed(app_config))

    def test_unknown_sandbox_denies_bash(self):
        app_config = SimpleNamespace(sandbox=SimpleNamespace(use="custom.sandbox:Weird"))
        self.assertFalse(is_host_bash_allowed(app_config))


class NoTaskToolForSubagentsTests(unittest.TestCase):
    def test_get_available_tools_subagent_enabled_false_excludes_task(self):
        async def run():
            from caspian.tools import get_available_tools

            tools = await get_available_tools(subagent_enabled=False)
            return [t.name for t in tools]

        names = asyncio.run(run())
        self.assertNotIn("task", names)


def _running_result_holder(task_id="tc-1"):
    return SubagentResult(task_id=task_id, trace_id="tr", status=SubagentStatus.RUNNING)


def _patch_task_tool_dependencies(result_holder):
    """统一 patch：fake executor + 恒 RUNNING 后台结果，避免真实线程与模型调用。"""
    config = SubagentConfig(name="general-purpose", description="d", timeout_seconds=5)
    fake_executor = MagicMock()
    fake_executor.execute_async.return_value = result_holder.task_id
    fake_executor.model_name = "deepseek-v4-flash"
    return (
        config,
        [
            patch("caspian.tools.builtins.task_tool.get_subagent_config", return_value=config),
            patch("caspian.tools.builtins.task_tool.SubagentExecutor", return_value=fake_executor),
            patch(
                "caspian.tools.builtins.task_tool.get_background_task_result",
                return_value=result_holder,
            ),
        ],
    )


class ParentCancelTests(unittest.TestCase):
    """父 run 取消时：请求协作取消并等待后台任务终态，随后重抛 CancelledError。"""

    def test_parent_cancel_requests_cooperative_cancellation(self):
        async def scenario():
            from contextlib import ExitStack

            result_holder = _running_result_holder()
            config, patches = _patch_task_tool_dependencies(result_holder)
            with ExitStack() as stack:
                for item in patches:
                    stack.enter_context(item)
                mock_cancel = stack.enter_context(
                    patch(
                        "caspian.tools.builtins.task_tool.request_cancel_background_task"
                    )
                )
                stack.enter_context(
                    patch(
                        "caspian.tools.builtins.task_tool._await_subagent_terminal",
                        new=AsyncMock(return_value=result_holder),
                    )
                )
                task = asyncio.create_task(
                    task_tool.coroutine(
                        runtime=_fake_runtime(),
                        description="d",
                        prompt="p",
                        subagent_type="general-purpose",
                        tool_call_id=result_holder.task_id,
                    )
                )
                await asyncio.sleep(0.1)  # 让轮询循环启动
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                return mock_cancel.call_count

        self.assertEqual(asyncio.run(scenario()), 1)


class PollingTimeoutTests(unittest.TestCase):
    """轮询超时安全网：后台任务悬挂时返回 polling_timed_out 并请求取消。"""

    def test_polling_timeout_returns_polling_timed_out(self):
        async def scenario():
            from contextlib import ExitStack

            result_holder = _running_result_holder()
            config, patches = _patch_task_tool_dependencies(result_holder)
            with ExitStack() as stack:
                for item in patches:
                    stack.enter_context(item)
                mock_cancel = stack.enter_context(
                    patch(
                        "caspian.tools.builtins.task_tool.request_cancel_background_task"
                    )
                )
                stack.enter_context(
                    patch("caspian.tools.builtins.task_tool._POLL_INTERVAL_SECONDS", 1)
                )
                # sleep 立即返回，让 61 次轮询（timeout=1 → (1+60)//1）瞬时推进
                stack.enter_context(
                    patch(
                        "caspian.tools.builtins.task_tool.asyncio.sleep",
                        new=AsyncMock(),
                    )
                )
                command = await task_tool.coroutine(
                    runtime=_fake_runtime(),
                    description="d",
                    prompt="p",
                    subagent_type="general-purpose",
                    tool_call_id=result_holder.task_id,
                )
            return command, mock_cancel.call_count

        command, cancel_count = asyncio.run(scenario())
        message = command.update["messages"][0]
        self.assertEqual(message.additional_kwargs["subagent_status"], "polling_timed_out")
        self.assertIn("polling timed out", message.content)
        self.assertEqual(cancel_count, 1)


if __name__ == "__main__":
    unittest.main()
