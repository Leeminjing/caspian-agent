"""
本文件验证 config.yaml 声明式工具加载路径修复：6 个沙箱工具模块级可解析、
get_available_tools 加载成功、tool_groups 过滤生效、sandbox_to_tools 兼容入口可用。
"""

import asyncio
import unittest
from unittest.mock import Mock

from caspian.reflection.resolvers import resolve_class
from caspian.sandbox.tools import sandbox_to_tools

_SANDBOX_TOOL_NAMES = [
    "read_file_tool",
    "write_file_tool",
    "bash_tool",
    "powershell_tool",
    "cmd_tool",
    "sh_tool",
]

_SHELL_TOOL_NAMES = {"bash_tool", "powershell_tool", "cmd_tool", "sh_tool"}


def _env():
    import os

    os.environ.setdefault("JWT_SECRET", "test-secret")
    os.environ.setdefault("GITHUB_TOKEN", "test-token")
    os.environ.setdefault("OPENAI_API_KEY", "test-key")
    os.environ.setdefault("DASHSCOPE_API_KEY", "test-dashscope-key")
    os.environ.setdefault("CASPIAN_SANDBOX", "caspian.sandbox.local:LocalSandbox")


_env()


class SandboxToolLoadingTests(unittest.TestCase):

    def test_module_level_tools_resolvable(self):
        for name in _SANDBOX_TOOL_NAMES:
            with self.subTest(name=name):
                tool_fn = resolve_class(f"caspian.sandbox.tools:{name}")
                self.assertTrue(hasattr(tool_fn, "name"))
                self.assertTrue(hasattr(tool_fn, "description"))

    def test_available_tools_include_six_sandbox_tools(self):
        from caspian.tools import get_available_tools

        tools = asyncio.run(get_available_tools())
        names = {t.name for t in tools}
        for name in _SANDBOX_TOOL_NAMES:
            self.assertIn(name, names)

    def test_tool_groups_file_read_write(self):
        from caspian.tools import get_available_tools

        tools = asyncio.run(get_available_tools(tool_groups=["file:read", "file:write"]))
        names = {t.name for t in tools}
        self.assertIn("read_file_tool", names)
        self.assertIn("write_file_tool", names)
        self.assertFalse(_SHELL_TOOL_NAMES & names)

    def test_tool_groups_shell(self):
        from caspian.tools import get_available_tools

        tools = asyncio.run(get_available_tools(tool_groups=["shell"]))
        names = {t.name for t in tools}
        self.assertTrue(_SHELL_TOOL_NAMES.issubset(names))
        self.assertNotIn("read_file_tool", names)
        self.assertNotIn("write_file_tool", names)

    def test_sandbox_to_tools_compat_entry(self):
        sandbox = Mock()
        tools = sandbox_to_tools(sandbox)
        names = [t.name for t in tools]
        self.assertEqual(names, _SANDBOX_TOOL_NAMES)

        tools[0].invoke({"path": "/mnt/user-data/workspace/a.py"})
        sandbox.read_file.assert_called_once_with("/mnt/user-data/workspace/a.py")

        tools[1].invoke({"path": "/mnt/user-data/outputs/b.txt", "content": "x"})
        sandbox.write_file.assert_called_once_with("/mnt/user-data/outputs/b.txt", "x")

        tools[2].invoke({"command": "ls -la"})
        sandbox.run_shell.assert_called_once_with("ls -la", shell_type="bash")


if __name__ == "__main__":
    unittest.main()
