"""
本文件对外提供 sandbox_to_tools，将 Sandbox 实例的三个方法包装为 LangChain @tool。

对外提供:
    sandbox_to_tools: 输入 Sandbox 实例，返回 LangChain Tool 列表

工作流:
    使用 @tool 装饰器分别包装 sandbox.read_file / sandbox.write_file / sandbox.run_shell
    read_file_tool 支持两种路径前缀:
        /mnt/user-data/ → validate_subdir 后委托 sandbox.read_file
        /mnt/skills/    → 直接委托 sandbox.read_file（路径解析由 sandbox 内部处理）
    write_file_tool 允许访问 workspace / outputs 两个子目录（禁止写入 uploads）
    run_shell 按 shell 类型拆分为 4 个独立工具:
        bash_tool / powershell_tool / cmd_tool / sh_tool
    每个 shell tool 显式声明并调用 sandbox.run_shell(command, shell_type=<type>)
    调用方通过 sandbox_to_tools(sandbox) 一次性获取 6 个工具

示例:
    provider = get_sandbox_provider()
    sid = provider.acquire("abc123")
    sb = provider.get(sid)
    tools = sandbox_to_tools(sb)
    # tools[0] read_file_tool, tools[1] write_file_tool,
    # tools[2] bash_tool, tools[3] powershell_tool, tools[4] cmd_tool, tools[5] sh_tool
"""

from langchain_core.tools import tool

from lead_agent.sandbox.base import Sandbox
from lead_agent.sandbox.path_utils import SKILLS_VROOT, validate_subdir


def sandbox_to_tools(sandbox: Sandbox) -> list:

    @tool
    def read_file_tool(path: str) -> str:
        """读取沙箱中指定虚拟路径的文件内容。

        支持两种路径前缀:
        - /mnt/user-data/...: 沙箱内文件，允许访问 uploads / workspace / outputs
        - /mnt/skills/...: skill 文件，只读

        Args:
            path: 虚拟路径，以 /mnt/user-data/ 或 /mnt/skills/ 开头
        """
        if path.startswith(SKILLS_VROOT + "/"):
            return sandbox.read_file(path)

        validate_subdir(path, {"uploads", "workspace", "outputs"})
        return sandbox.read_file(path)

    @tool
    def write_file_tool(path: str, content: str) -> str:
        """将内容写入沙箱中指定虚拟路径的文件。

        允许访问的子目录: workspace / outputs

        Args:
            path: 虚拟路径，以 /mnt/user-data/workspace/ 或 /mnt/user-data/outputs/ 开头
            content: 要写入的文件内容
        """
        validate_subdir(path, {"workspace", "outputs"})
        sandbox.write_file(path, content)
        return f"写入成功: {path}"

    @tool
    def bash_tool(command: str) -> str:
        """在沙箱 workspace 目录下执行 Bash shell 命令并返回结果。

        支持 bash 语法（管道、变量、重定向等）。

        Args:
            command: 要执行的 bash 命令字符串
        """
        return sandbox.run_shell(command, shell_type="bash")

    @tool
    def powershell_tool(command: str) -> str:
        """在沙箱 workspace 目录下执行 PowerShell 命令并返回结果。

        支持 PowerShell 语法（管道、对象操作、cmdlet 等）。

        Args:
            command: 要执行的 PowerShell 命令字符串
        """
        return sandbox.run_shell(command, shell_type="powershell")

    @tool
    def cmd_tool(command: str) -> str:
        """在沙箱 workspace 目录下执行 Windows CMD 命令并返回结果。

        仅 Windows 平台可用。

        Args:
            command: 要执行的 cmd 命令字符串
        """
        return sandbox.run_shell(command, shell_type="cmd")

    @tool
    def sh_tool(command: str) -> str:
        """在沙箱 workspace 目录下执行 POSIX sh 命令并返回结果。

        支持标准 sh 语法。

        Args:
            command: 要执行的 sh 命令字符串
        """
        return sandbox.run_shell(command, shell_type="sh")

    return [
        read_file_tool,
        write_file_tool,
        bash_tool,
        powershell_tool,
        cmd_tool,
        sh_tool,
    ]
