"""
本文件对外提供 list_uploaded_files 工具，供 agent 按需查询当前 thread 的所有历史上传文件。

对外提供:
    list_uploaded_files — LangChain @tool，扫描当前 thread 的 uploads/ 目录

输入:
    include_outline: bool | list[str] — 控制 .md 文件的 outline/preview 生成:
        - False/省略: 不生成 outline
        - True: 为所有 .md 生成
        - ["a.md"]: 仅为指定文件名生成
    runtime: ToolRuntime — LangGraph 运行时注入，用于获取 thread_id

输出:
    list[dict] — [{filename, size, outline?|preview?}, ...]

具体工作流:
    (1) 从 runtime.execution_info 获取 thread_id
    (2) 从 runtime.context 获取 user_id
    (3) 构造 uploads 目录真实路径
    (4) 扫描目录，收集文件名和大小
    (5) 根据 include_outline 决定是否为 .md 文件提取 outline/preview
    (6) 返回文件列表

示例:
    result = list_uploaded_files(runtime=runtime)
    # → [{"filename": "report.docx", "size": 43008}, ...]

    result = list_uploaded_files(include_outline=True, runtime=runtime)
    # → [{"filename": "a.md", "size": 100, "outline": [{"line": 1, "text": "标题"}]}, ...]
"""

import os

from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime

from focus.agents.middlewares.uploads_middleware import _extract_outline_for_file
from focus.sandbox.path_utils import REAL_ROOT


def _get_user_id(runtime: ToolRuntime) -> str | None:
    """从 Runtime.context 中获取 user_id。

    输入:
        runtime: ToolRuntime — LangGraph 运行时

    输出:
        str | None — user_id

    工作流:
        (1) 从 runtime.context 取 user_id（即 worker.py 中 agent.astream(context=langgraph_context) 传入的 context）
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


@tool(parse_docstring=True)
def list_uploaded_files(
    include_outline: bool | list[str] = False,
    runtime: ToolRuntime = None,
) -> list[dict]:
    """List all uploaded files for the current thread.

    Use this tool to discover which files have been uploaded to the current thread.
    Historical uploaded files are not automatically listed in context — you must call this tool to find them.

    When to use the list_uploaded_files tool:
    - When you need to discover which files have been uploaded in previous runs
    - When you need to see all available files with their metadata

    When NOT to use the list_uploaded_files tool:
    - When you already know the file path — use read_file_tool or grep directly
    - When you only need to read file content — use read_file_tool directly

    Notes:
    - Current run uploads are automatically listed in <current_uploads> — this tool is for discovering older files.
    - Use include_outline to expand .md file outlines for better navigation.

    Args:
        include_outline: Controls outline/preview generation for .md files:
            - Omit or pass false (default): No outlines — returns filename and size only.
            - Pass true: Generate outlines for all .md files.
            - Pass a list of filenames, e.g. ["report.md", "notes.md"]: Generate outlines only for those files.
    """
    thread_id = None
    if runtime is not None and runtime.execution_info is not None:
        thread_id = runtime.execution_info.thread_id
    if thread_id is None:
        return [{"error": "无法获取当前 thread ID"}]

    user_id = _get_user_id(runtime) if runtime is not None else None
    if user_id is None:
        return [{"error": "无法获取当前 user ID"}]

    uploads_dir = os.path.join(
        REAL_ROOT.format(user_id=user_id, thread_id=thread_id),
        "uploads",
    )
    if not os.path.isdir(uploads_dir):
        return []

    # 确定哪些 .md 需要展开 outline
    expand_all = include_outline is True
    expand_names: set[str] = set()
    if isinstance(include_outline, list):
        expand_names = set(include_outline)

    result: list[dict] = []
    try:
        for entry in os.scandir(uploads_dir):
            if not entry.is_file():
                continue
            filename = entry.name
            size = entry.stat().st_size
            item: dict = {"filename": filename, "size": size}

            if os.path.splitext(filename)[1].lower() == ".md":
                should_expand = expand_all or filename in expand_names
                if should_expand:
                    extra = _extract_outline_for_file(entry.path)
                    if extra:
                        item.update(extra)

            result.append(item)
    except Exception:
        return [{"error": "扫描 uploads 目录失败"}]

    return result
