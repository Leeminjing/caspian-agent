"""工具失败回传（tool-error-reporting）单元测试：统一收口捕获任何工具异常回传 LLM。"""

import asyncio
from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage

from caspian.agents.middlewares.tool_error_middleware import ToolErrorMiddleware


def _run(coro):
    return asyncio.run(coro)


def _request(tool_call_id="c1", name="bash"):
    return SimpleNamespace(tool_call={"id": tool_call_id, "name": name, "args": {}})


def _ok_handler(request):
    async def _run():
        return ToolMessage(content="ok", tool_call_id=request.tool_call["id"])
    return _run()


class TestToolErrorMiddleware:
    def test_success_passthrough(self):
        result = _run(ToolErrorMiddleware().awrap_tool_call(_request(), _ok_handler))
        assert isinstance(result, ToolMessage)
        assert result.content == "ok"
        assert result.tool_call_id == "c1"

    def test_runtime_error_reported(self):
        def bad_handler(request):
            raise RuntimeError("Shell 'sh' not found in PATH (looked for: sh)")

        result = _run(ToolErrorMiddleware().awrap_tool_call(_request("c7", "sh"), bad_handler))
        assert isinstance(result, ToolMessage)
        assert result.content.startswith("[tool_error] RuntimeError:")
        assert "Shell 'sh' not found in PATH" in result.content
        assert result.tool_call_id == "c7"
        assert result.name == "sh"

    def test_security_error_reported(self):
        def bad_handler(request):
            from caspian.sandbox.path_utils import SecurityError
            raise SecurityError("绝对路径 'C:/mnt/user-data/workspace' 不在白名单中，拒绝执行")

        result = _run(ToolErrorMiddleware().awrap_tool_call(_request(), bad_handler))
        assert isinstance(result, ToolMessage)
        assert result.content.startswith("[tool_error] SecurityError:")
        assert "不在白名单" in result.content

    def test_goal_error_reported(self):
        def bad_handler(request):
            from caspian.goal.domain import GoalError
            raise GoalError("create_goal requires a direct human turn", "GOAL_TOOL_AUTHORITY_REQUIRED")

        result = _run(ToolErrorMiddleware().awrap_tool_call(_request(), bad_handler))
        assert isinstance(result, ToolMessage)
        assert result.content.startswith("[tool_error] GoalError:")
        assert "direct human turn" in result.content

    def test_cancelled_error_not_swallowed(self):
        def cancel_handler(request):
            raise asyncio.CancelledError()

        try:
            _run(ToolErrorMiddleware().awrap_tool_call(_request(), cancel_handler))
        except asyncio.CancelledError:
            return
        raise AssertionError("CancelledError 不应被吞掉")
