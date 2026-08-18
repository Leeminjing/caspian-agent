"""
本文件对外提供 add_knowledge 内置工具：lead agent 把信息带等级写入知识库。

对外提供:
    add_knowledge_tool — 入库一条知识（离散权威等级可选，缺省未评级）

输入:
    content: str — 知识正文
    level: int | None — 离散权威等级 0-3（L0 低权威 … L3 高权威），None=未评级
    source: str — 来源名称（如"官方文档"）
    source_url: str | None — 来源链接
    runtime: ToolRuntime — 注入运行时（取 runtime.store 与 runtime.context.user_id）

输出:
    str — 入库结果说明（条目 id 与等级展示）

具体工作流:
    (1) 从 runtime 取 store 与 user_id；缺失时返回说明性错误字符串
    (2) put_knowledge 写入 ("knowledge", user_id) 命名空间
    (3) 返回 id 与 level_display

示例:
    result = await add_knowledge_tool.ainvoke({
        "content": "功能 A 在 3.14 版本已废弃。",
        "level": 3,
        "source": "官方文档",
        "source_url": "https://example.com/changelog",
    })
"""

import logging

from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime

from caspian.knowledge.schemas import level_display
from caspian.knowledge.store_client import put_knowledge

logger = logging.getLogger(__name__)


@tool
async def add_knowledge(
    content: str,
    level: int | None = None,
    source: str = "",
    source_url: str | None = None,
    runtime: ToolRuntime = None,
) -> str:
    """把经过核实的信息写入受治理的知识库（离散等级治理 RAG）。

    When to use: 用户要求记忆/收录某条信息，或你在任务中核实了重要事实且
    后续会话可能引用时。等级必须如实反映来源权威性：
    3=L3 官方/最高权威，2=L2 较高权威，1=L1 普通来源，0=L0 低权威；
    无法确定等级时不要填写（系统按"未评级"处理，视为最低档）。

    When NOT to use: 未经验证的猜测、临时讨论内容、与用户目标无关的信息。

    Args:
        content: 知识正文，一句话到一段话。
        level: 离散权威等级 0-3，可省略（未评级）。
        source: 来源名称，如"官方文档"。
        source_url: 来源链接，可省略。
    """
    store = getattr(runtime, "store", None)
    ctx = getattr(runtime, "context", None)
    user_id = ctx.get("user_id") if isinstance(ctx, dict) else None
    if store is None or user_id is None:
        return "知识库不可用：缺少 store 或 user_id 运行上下文。"

    try:
        key = await put_knowledge(
            store,
            str(user_id),
            content=content,
            level=level,
            source=source,
            source_url=source_url,
        )
    except ValueError as exc:
        return f"入库失败：{exc}"
    return f"已入库（id={key}，等级 {level_display(level)}）。"
