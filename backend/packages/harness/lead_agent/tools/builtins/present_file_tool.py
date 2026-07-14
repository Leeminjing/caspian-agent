"""
本文件对外提供 present_file_tool，将 outputs 目录中已存在的文件展示给用户。

内部辅助:
    _get_thread_id — 从 ToolRuntime.execution_info 解析当前线程 ID，返回 str | None
    _normalize_presented_filepath — 将传入路径规范化为 /mnt/user-data/outputs/... 虚拟路径，返回 str
    _sanitize_present_error — 对错误信息脱敏，避免暴露本地真实路径，返回 str

输入:
    filepaths: list[str] — 需要展示给用户的文件路径列表，每个路径可以是 /mnt/user-data/outputs/... 虚拟路径，也可以是后端真实线程目录下的 outputs 文件路径
    ToolRuntime — LangGraph 运行时注入，提供 state、config、execution_info

输出:
    Command — 路径合法时更新 artifacts 和 messages，路径非法时返回错误 ToolMessage

具体工作流:
    (1) present_file_tool 接收 filepaths 列表
    (2) 对每个路径调用 _normalize_presented_filepath
    (3) _normalize_presented_filepath 先通过 _get_thread_id 解析当前线程 ID
    (4) 从 runtime.context 获取 user_id，结合 REAL_ROOT 模板构造 outputs_path
    (5) 若传入虚拟路径则解析为真实路径，若传入真实路径则展开为绝对路径
    (6) 校验真实路径是否位于当前线程 outputs 目录中
    (7) 路径合法则转换回 /mnt/user-data/outputs/... 虚拟路径
    (8) 全部合法则返回更新 artifacts 和 messages 的 Command
    (9) 任意路径非法则返回错误 ToolMessage 的 Command，不更新 artifacts

示例:
    runtime: ToolRuntime = ...
    result = present_file_tool(
        filepaths=[
            "/mnt/user-data/outputs/report.md",
            "/mnt/user-data/outputs/result.png",
        ],
        runtime=runtime,
    )
    输出 Command:
    {
        "artifacts": ["/mnt/user-data/outputs/report.md", "/mnt/user-data/outputs/result.png"],
        "messages": [ToolMessage("Successfully presented files")]
    }
"""

import os

from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command

from lead_agent.sandbox.path_utils import REAL_ROOT, VRROOT, resolve_path


def _get_thread_id(runtime: ToolRuntime) -> str | None:
    if runtime.execution_info is None:
        return None
    return runtime.execution_info.thread_id


def _sanitize_present_error(error: Exception, runtime: ToolRuntime) -> str:
    msg = str(error)
    try:
        thread_id = _get_thread_id(runtime)
        if thread_id:
            real_root = REAL_ROOT.format(thread_id=thread_id)
            msg = msg.replace(real_root, VRROOT)
    except Exception:
        pass
    msg = msg.replace("\\", "/")
    return f"present_files 失败: {msg}"


def _normalize_presented_filepath(filepath: str, runtime: ToolRuntime) -> str:
    thread_id = _get_thread_id(runtime)
    if thread_id is None:
        raise ValueError("无法获取当前线程 ID，present_files 只能在 Agent 运行上下文中使用")

    user_id = None
    try:
        ctx = runtime.context
        if ctx and isinstance(ctx, dict):
            user_id = ctx.get("user_id")
    except Exception:
        pass
    if user_id is None:
        raise ValueError("无法获取 user_id，present_files 只能在 Agent 运行上下文中使用")

    outputs_path = os.path.join(REAL_ROOT.format(user_id=user_id, thread_id=thread_id), "outputs")
    outputs_path_abs = os.path.abspath(outputs_path)

    if filepath.startswith(VRROOT + "/"):
        real_path = resolve_path(filepath, user_id, thread_id)
        real_path_abs = os.path.abspath(real_path)
    else:
        real_path_abs = os.path.abspath(filepath)

    if not real_path_abs.startswith(outputs_path_abs):
        raise ValueError(
            f"文件不在当前线程 outputs 目录中: '{filepath}'，仅允许 outputs 目录下的文件被展示"
        )

    if not os.path.exists(real_path_abs):
        raise ValueError(f"文件不存在: '{filepath}'")

    relative = os.path.relpath(real_path_abs, outputs_path_abs)
    return VRROOT + "/outputs/" + relative.replace("\\", "/")


@tool(parse_docstring=True)
def present_file_tool(
    filepaths: list[str],
    runtime: ToolRuntime,
) -> Command:
    """Make files visible to the user for viewing and rendering in the client interface.

When to use the present_files tool:

- Making any file available for the user to view, download, or interact with
- Presenting multiple related files at once
- After creating files that should be presented to the user

When NOT to use the present_files tool:
- When you only need to read file contents for your own processing
- For temporary or intermediate files not meant for user viewing

Notes:
- You should call this tool after creating files and moving them to the `/mnt/user-data/outputs` directory.
- This tool can be safely called in parallel with other tools. State updates are handled by a reducer to prevent conflicts.

Args:
    filepaths: List of absolute file paths to present to the user. **Only** files in `/mnt/user-data/outputs` can be presented.
"""
    normalized: list[str] = []
    for fp in filepaths:
        try:
            vpath = _normalize_presented_filepath(fp, runtime)
            normalized.append(vpath)
        except Exception as e:
            sanitized = _sanitize_present_error(e, runtime)
            return Command(
                update={
                    "messages": [
                        ToolMessage(
                            content=sanitized,
                            tool_call_id=runtime.tool_call_id,
                        )
                    ]
                }
            )

    return Command(
        update={
            "artifacts": normalized,
            "messages": [
                ToolMessage(
                    content="Successfully presented files",
                    tool_call_id=runtime.tool_call_id,
                )
            ],
        }
    )
