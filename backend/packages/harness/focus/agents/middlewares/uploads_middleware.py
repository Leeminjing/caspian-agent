"""
本文件对外提供 `UploadsMiddleware` 类，作为 agent 启动时的文件上传感知中间件；以及模块级
`_extract_outline_for_file` 辅助函数（由 `list_uploaded_files` 工具复用）。

对外提供:
    UploadsMiddleware(AgentMiddleware) — 覆盖 before_agent / abefore_agent 钩子，
    从本轮 run 最后一条 HumanMessage 的 additional_kwargs.files 提取上传文件元数据，
    注入 <current_uploads> 标签到 message content 末尾

输入:
    before_agent / abefore_agent:
        state: AgentState — 当前 agent 状态（含 messages）
        runtime: ToolRuntime — LangGraph 运行时注入

输出:
    dict | None — 修改后的 state 增量（仅 messages），无新增文件时返回 None

具体工作流:
    before_agent / abefore_agent:
    (1) 从 state["messages"] 最后一条 HumanMessage 读取 additional_kwargs.files
    (2) 若无 files 或为空 → 返回 None
    (3) 对每个文件:
        (a) 根据 filename 推导虚拟路径 /mnt/user-data/uploads/{filename}
        (b) 解析出真实路径
        (c) 若为 .md 文件 → 调用 _extract_outline_for_file 获取 outline/preview
    (4) 生成 <current_uploads> 标签
    (5) 注入到最后一条 HumanMessage 的 content 末尾
    (6) 返回 { "messages": [...] } 的 state 增量

    _extract_outline_for_file:
    (1) 读取 .md 文件
    (2) 提取 # / ## 标题行 → 返回 outline
    (3) 无标题 → 返回前 5 行非空文本 preview
    (4) 文件不存在或读取失败 → 返回 None

示例:
    from focus.agents.middlewares.uploads_middleware import UploadsMiddleware
    middleware = UploadsMiddleware()
    # 在 create_agent(middleware=[middleware, ...]) 中使用
"""

import logging
import os

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import AgentState
from langchain_core.messages import HumanMessage
from langgraph.prebuilt import ToolRuntime

from focus.sandbox.path_utils import VRROOT, resolve_path

logger = logging.getLogger(__name__)


def _extract_outline_for_file(real_path: str) -> dict | None:
    """从 .md 文件中提取标题大纲或文本预览。

    输入:
        real_path: str — .md 文件的真实磁盘路径

    输出:
        dict | None — {"outline": [{"line": int, "text": str}, ...]}  有标题时
                     {"preview": ["行1", "行2", ...]}                 无标题时
                     None                                              文件不存在/读取失败

    工作流:
        (1) 检查文件是否存在
        (2) 逐行读取，匹配以 # 或 ## 开头的标题行
        (3) 有标题 → 收集 {line, text}，返回 outline
        (4) 无标题 → 收集前 5 行非空文本，返回 preview
        (5) 异常 → 记录 WARNING 并返回 None

    示例:
        result = _extract_outline_for_file("/path/to/report.md")
        # → {"outline": [{"line": 12, "text": "项目背景"}, {"line": 35, "text": "技术方案"}]}
    """
    if not os.path.isfile(real_path):
        logger.warning("_extract_outline_for_file: 文件不存在 '%s'", real_path)
        return None

    try:
        with open(real_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        logger.warning("_extract_outline_for_file: 读取文件失败 '%s'", real_path, exc_info=True)
        return None

    outlines: list[dict] = []
    preview_lines: list[str] = []

    for i, line in enumerate(lines, start=1):
        stripped = line.rstrip("\n").rstrip("\r")
        if stripped.startswith("#"):
            heading_text = stripped.lstrip("#").strip()
            if heading_text:
                outlines.append({"line": i, "text": heading_text})
        elif not outlines and stripped:
            if len(preview_lines) < 5:
                preview_lines.append(stripped)

    if outlines:
        return {"outline": outlines}

    if preview_lines:
        return {"preview": preview_lines}

    return None


def _build_current_uploads(files: list[dict], thread_id: str, user_id: str) -> str | None:
    """根据文件列表生成 <current_uploads> 标签字符串。

    输入:
        files: list[dict] — [{filename, size}] 本轮上传文件元数据
        thread_id: str — 当前 thread ID（用于路径解析）
        user_id: str — 当前 user ID（用于路径解析）

    输出:
        str | None — <current_uploads> 标签完整字符串，无有效文件时返回 None

    工作流:
        (1) 遍历 files
        (2) 对每个文件解析虚拟路径 → 真实路径
        (3) 提取扩展名
        (4) .md 文件额外调用 _extract_outline_for_file
        (5) 组装 <current_uploads> 标签

    示例:
        tag = _build_current_uploads([{"filename": "r.md", "size": 100}], "th-1", "u-1")
        # → "<current_uploads>\n- filename: r.md\n  size: 100\n  extension: .md\n..."
    """
    lines = ["<current_uploads>"]

    for fmeta in files:
        filename = fmeta.get("filename", "")
        size = fmeta.get("size", 0)
        if not filename:
            continue

        _, ext = os.path.splitext(filename)
        ext = ext.lower()

        lines.append(f"- filename: {filename}")
        lines.append(f"  size: {size}")
        lines.append(f"  extension: {ext}")

        if ext == ".md":
            vpath = VRROOT + "/uploads/" + filename
            try:
                real_path = resolve_path(vpath, user_id, thread_id)
                extra = _extract_outline_for_file(real_path)
                if extra:
                    if "outline" in extra:
                        lines.append("  outline:")
                        for item in extra["outline"]:
                            lines.append(f"    L{item['line']}: {item['text']}")
                    elif "preview" in extra:
                        lines.append("  preview:")
                        for preview_line in extra["preview"]:
                            lines.append(f"    {preview_line}")
            except Exception:
                logger.warning("解析 .md 文件路径失败: filename='%s'", filename, exc_info=True)

    lines.append("</current_uploads>")

    if len(lines) <= 2:
        return None
    return "\n".join(lines)


class UploadsMiddleware(AgentMiddleware):

    def _inject_current_uploads(self, state: AgentState, runtime: ToolRuntime) -> dict | None:
        """核心逻辑：提取文件元数据，生成 <current_uploads> 并注入 HumanMessage。

        输入:
            state: AgentState — 当前 agent 状态
            runtime: ToolRuntime — LangGraph 运行时

        输出:
            dict | None — {"messages": [...]} state 增量，无文件时返回 None
        """
        messages = state.get("messages", [])
        if not messages:
            return None

        last_msg = messages[-1]
        if not isinstance(last_msg, HumanMessage):
            return None

        additional_kwargs = getattr(last_msg, "additional_kwargs", None) or {}
        files = additional_kwargs.get("files")
        if not files or not isinstance(files, list):
            return None

        thread_id = None
        if runtime.execution_info is not None:
            thread_id = runtime.execution_info.thread_id
        if thread_id is None:
            logger.warning("UploadsMiddleware: 无法获取 thread_id，跳过 <current_uploads> 注入")
            return None

        user_id = None
        try:
            ctx = runtime.context
            if ctx and isinstance(ctx, dict):
                user_id = ctx.get("user_id")
        except Exception:
            pass
        if user_id is None:
            logger.warning("UploadsMiddleware: 无法获取 user_id，跳过 <current_uploads> 注入")
            return None

        tag = _build_current_uploads(files, str(thread_id), str(user_id))
        if tag is None:
            return None

        new_content = (last_msg.content or "") + "\n\n" + tag
        new_msg = HumanMessage(
            content=new_content,
            id=last_msg.id,
            additional_kwargs=last_msg.additional_kwargs,
        )

        return {"messages": [new_msg]}

    def before_agent(self, state: AgentState, runtime: ToolRuntime) -> dict | None:
        """同步钩子：在 agent 执行前注入 <current_uploads>。

        输入:
            state: AgentState — 当前 agent 状态
            runtime: ToolRuntime — LangGraph 运行时

        输出:
            dict | None — state 增量，无文件时返回 None
        """
        try:
            return self._inject_current_uploads(state, runtime)
        except Exception:
            logger.error("UploadsMiddleware.before_agent 异常，跳过注入", exc_info=True)
            return None

    async def abefore_agent(self, state: AgentState, runtime: ToolRuntime) -> dict | None:
        """异步钩子：逻辑与同步版本同构。

        输入:
            state: AgentState — 当前 agent 状态
            runtime: ToolRuntime — LangGraph 运行时

        输出:
            dict | None — state 增量，无文件时返回 None
        """
        try:
            return self._inject_current_uploads(state, runtime)
        except Exception:
            logger.error("UploadsMiddleware.abefore_agent 异常，跳过注入", exc_info=True)
            return None
