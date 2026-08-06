"""
本文件对外提供 6 个模块级沙箱工具（read_file_tool / write_file_tool / bash_tool / powershell_tool / cmd_tool / sh_tool）
与 sandbox_to_tools 兼容入口。

模块级工具（主加载路径）:
    config.yaml 的 tools 声明通过 resolve_class("caspian.sandbox.tools:<tool_name>") 直接解析到本模块的
    模块级 @tool 函数，无需手动装配。工具调用时通过注入的 ToolRuntime 运行时解析 user_id / thread_id，
    经 get_sandbox_provider() 懒获取当前线程的沙箱实例。

sandbox_to_tools（兼容入口）:
    输入 Sandbox 实例，返回 6 个绑定该实例的 LangChain Tool 列表。供显式注入沙箱的调用方式使用。

输入:
    read_file_tool: path（虚拟路径，/mnt/user-data/ 或 /mnt/skills/ 前缀）+ runtime（可选）
    write_file_tool: path（虚拟路径，限 workspace/outputs 子目录）+ content + runtime（可选）
    bash_tool / powershell_tool / cmd_tool / sh_tool: command（shell 命令）+ runtime（可选）

输出:
    str — 文件内容 / 写入结果 / shell 执行输出；无运行上下文时返回说明性错误字符串

具体工作流:
    (1) 工具调用时从 runtime.context 提取 user_id，从 runtime.execution_info.thread_id 提取 thread_id
    (2) 经 get_sandbox_provider().acquire(user_id, thread_id) + .get(sandbox_id) 获取沙箱实例
    (3) read_file_tool 对 /mnt/skills/ 前缀直接委托沙箱读取；其余路径经 validate_subdir 白名单校验后委托
    (4) write_file_tool 经 validate_subdir 校验 workspace/outputs 后写入
    (5) 4 个 shell 工具显式声明 shell_type 委托 sandbox.run_shell
    (6) 无运行上下文（runtime 缺失或 thread_id 不可得）时返回错误字符串，不抛未处理异常

示例:
    # 模块级工具（config.yaml 声明式加载）
    tools = await get_available_tools()
    # 兼容入口
    provider = get_sandbox_provider()
    sid = provider.acquire("abc123", "thread-1")
    sb = provider.get(sid)
    tools = sandbox_to_tools(sb)
    # tools[0] read_file_tool, tools[1] write_file_tool,
    # tools[2] bash_tool, tools[3] powershell_tool, tools[4] cmd_tool, tools[5] sh_tool
"""

from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime

from caspian.sandbox.base import Sandbox
from caspian.sandbox.path_utils import SKILLS_VROOT, validate_subdir
from caspian.sandbox.provider import get_sandbox_provider


def _runtime_user_id(runtime: ToolRuntime) -> str | None:
    """从 ToolRuntime 提取 user_id。

    输入:
        runtime: ToolRuntime — Agent 运行时注入，context 为 worker 传入的 langgraph_context dict

    输出:
        str | None — user_id；不可得时返回 None
    """
    try:
        ctx = runtime.context
        if ctx and isinstance(ctx, dict):
            uid = ctx.get("user_id")
            if uid:
                return str(uid)
    except Exception:
        pass
    return None


def _runtime_sandbox(runtime: ToolRuntime) -> Sandbox:
    """从 ToolRuntime 解析当前线程的沙箱实例。

    输入:
        runtime: ToolRuntime — Agent 运行时注入

    输出:
        Sandbox — 经 get_sandbox_provider 获取的当前 (user_id, thread_id) 沙箱实例

    工作流:
        (1) 从 runtime.execution_info.thread_id 提取 thread_id
        (2) 从 runtime.context 提取 user_id
        (3) 调用 get_sandbox_provider().acquire(user_id, thread_id) 获取沙箱 ID
        (4) 调用 provider.get(sandbox_id) 返回沙箱实例

    异常:
        ValueError — runtime 缺失、thread_id 或 user_id 不可得时抛出
    """
    if runtime is None or runtime.execution_info is None:
        raise ValueError("沙箱工具只能在 Agent 运行上下文中使用")
    thread_id = runtime.execution_info.thread_id
    if thread_id is None:
        raise ValueError("沙箱工具只能在 Agent 运行上下文中使用（无法获取 thread_id）")
    user_id = _runtime_user_id(runtime)
    if user_id is None:
        raise ValueError("沙箱工具只能在 Agent 运行上下文中使用（无法获取 user_id）")
    provider = get_sandbox_provider()
    sandbox_id = provider.acquire(user_id, str(thread_id))
    return provider.get(sandbox_id)


@tool
def read_file_tool(path: str, runtime: ToolRuntime = None) -> str:
    """读取沙箱中指定虚拟路径的文件内容。

    支持两种路径前缀:
    - /mnt/user-data/...: 沙箱内文件，允许访问 uploads / workspace / outputs
    - /mnt/skills/...: skill 文件，只读

    Args:
        path: 虚拟路径，以 /mnt/user-data/ 或 /mnt/skills/ 开头
    """
    try:
        sandbox = _runtime_sandbox(runtime)
    except ValueError as e:
        return str(e)

    if path.startswith(SKILLS_VROOT + "/"):
        return sandbox.read_file(path)

    validate_subdir(path, {"uploads", "workspace", "outputs"})
    return sandbox.read_file(path)


@tool
def write_file_tool(path: str, content: str, runtime: ToolRuntime = None) -> str:
    """将内容写入沙箱中指定虚拟路径的文件。

    允许访问的子目录: workspace / outputs

    Args:
        path: 虚拟路径，以 /mnt/user-data/workspace/ 或 /mnt/user-data/outputs/ 开头
        content: 要写入的文件内容
    """
    try:
        sandbox = _runtime_sandbox(runtime)
    except ValueError as e:
        return str(e)

    validate_subdir(path, {"workspace", "outputs"})
    sandbox.write_file(path, content)
    return f"写入成功: {path}"


@tool
def bash_tool(command: str, runtime: ToolRuntime = None) -> str:
    """在沙箱 workspace 目录下执行 Bash shell 命令并返回结果。

    支持 bash 语法（管道、变量、重定向等）。

    Args:
        command: 要执行的 bash 命令字符串
    """
    try:
        sandbox = _runtime_sandbox(runtime)
    except ValueError as e:
        return str(e)

    return sandbox.run_shell(command, shell_type="bash")


@tool
def powershell_tool(command: str, runtime: ToolRuntime = None) -> str:
    """在沙箱 workspace 目录下执行 PowerShell 命令并返回结果。

    支持 PowerShell 语法（管道、对象操作、cmdlet 等）。

    Args:
        command: 要执行的 PowerShell 命令字符串
    """
    try:
        sandbox = _runtime_sandbox(runtime)
    except ValueError as e:
        return str(e)

    return sandbox.run_shell(command, shell_type="powershell")


@tool
def cmd_tool(command: str, runtime: ToolRuntime = None) -> str:
    """在沙箱 workspace 目录下执行 Windows CMD 命令并返回结果。

    仅 Windows 平台可用。

    Args:
        command: 要执行的 cmd 命令字符串
    """
    try:
        sandbox = _runtime_sandbox(runtime)
    except ValueError as e:
        return str(e)

    return sandbox.run_shell(command, shell_type="cmd")


@tool
def sh_tool(command: str, runtime: ToolRuntime = None) -> str:
    """在沙箱 workspace 目录下执行 POSIX sh 命令并返回结果。

    支持标准 sh 语法。

    Args:
        command: 要执行的 sh 命令字符串
    """
    try:
        sandbox = _runtime_sandbox(runtime)
    except ValueError as e:
        return str(e)

    return sandbox.run_shell(command, shell_type="sh")


def sandbox_to_tools(sandbox: Sandbox) -> list:
    """将 Sandbox 实例的三个方法包装为 LangChain Tool 列表（兼容入口）。

    输入:
        sandbox: Sandbox — 沙箱实例

    输出:
        list — 6 个绑定该沙箱实例的 LangChain Tool（与模块级工具同名同行为）

    注意:
        主加载路径为模块级 @tool 函数（config.yaml 声明式加载）；
        本函数保留供显式注入沙箱的调用方式使用。
    """

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
