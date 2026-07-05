"""
本文件将 Sandbox 实例的三个方法包装为 LangChain @tool。

对外提供:
    sandbox_to_tools: 输入 Sandbox 实例，返回 LangChain Tool 列表

工作流:
    使用 @tool 装饰器分别包装 sandbox.read_file / sandbox.write_file / sandbox.run_shell
    三个工具分别命名为 read_file_tool / write_file_tool / run_shell_tool
    调用方通过 sandbox_to_tools(sandbox) 一次性获取全部工具

示例:
    provider = get_sandbox_provider()
    sid = provider.acquire("abc123")
    sb = provider.get(sid)
    tools = sandbox_to_tools(sb)
    # tools[0] is read_file_tool, tools[1] is write_file_tool, tools[2] is run_shell_tool
"""

from langchain_core.tools import tool

from lead_agent.sandbox.base import Sandbox


def sandbox_to_tools(sandbox: Sandbox) -> list:

    @tool
    def read_file_tool(path: str) -> str:
        """读取沙箱中指定虚拟路径的文件内容。

        Args:
            path: 虚拟路径，以 /mnt/user-data/ 开头
        """
        return sandbox.read_file(path)

    @tool
    def write_file_tool(path: str, content: str) -> str:
        """将内容写入沙箱中指定虚拟路径的文件。

        Args:
            path: 虚拟路径，以 /mnt/user-data/ 开头
            content: 要写入的文件内容
        """
        sandbox.write_file(path, content)
        return f"写入成功: {path}"

    @tool
    def run_shell_tool(command: str) -> str:
        """在沙箱根目录下执行 shell 命令并返回结果。

        Args:
            command: 要执行的 shell 命令字符串
        """
        return sandbox.run_shell(command)

    return [read_file_tool, write_file_tool, run_shell_tool]
