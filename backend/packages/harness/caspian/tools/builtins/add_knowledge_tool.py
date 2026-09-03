"""
本文件对外提供 add_knowledge 内置工具：lead agent 把信息带来源写入知识库。

对外提供:
    add_knowledge_tool — 入库一条知识（等级由来源归属确定性派生，不由调用方指定）

输入:
    content: str — 知识正文
    source: str — 来源名称（如"官方文档"）
    source_url: str | None — 来源链接（等级依据其域名策略派生）
    runtime: ToolRuntime — 注入运行时（取 runtime.store 与 runtime.context.user_id）

输出:
    str — 入库结果说明（条目 id 与派生等级展示）

具体工作流:
    (1) 从 runtime 取 store 与 user_id；缺失时返回说明性错误字符串
    (2) put_knowledge 写入 ("knowledge", user_id) 命名空间，等级由 source_url 派生
    (3) 返回 id 与 level_display

示例:
    result = await add_knowledge.ainvoke({
        "content": "功能 A 在 3.14 版本已废弃。",
        "source": "官方文档",
        "source_url": "https://docs.example.com/changelog",
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
    source: str = "",
    source_url: str | None = None,
    runtime: ToolRuntime = None,
) -> str:
    """把经过核实的信息写入受治理的知识库（离散等级治理 RAG）。

    When to use: 用户要求记忆/收录某条信息，或你在任务中核实了重要事实且
    后续会话可能引用时。等级由来源链接的域名策略自动派生，你无需也**不得**自行
    指定等级——只需如实提供来源名称与链接。

    When NOT to use: 未经验证的猜测、临时讨论内容、与用户目标无关的信息。

    Args:
        content: 知识正文，一句话到一段话。
        source: 来源名称，如"官方文档"。
        source_url: 来源链接，等级由该链接的域名策略派生，可省略（省略则未评级）。
    """
    store = getattr(runtime, "store", None)
    ctx = getattr(runtime, "context", None)
    user_id = ctx.get("user_id") if isinstance(ctx, dict) else None
    if store is None or user_id is None:
        return "知识库不可用：缺少 store 或 user_id 运行上下文。"

    try:
        key, level = await put_knowledge(
            store,
            str(user_id),
            content=content,
            source=source,
            source_url=source_url,
        )
    except ValueError as exc:
        return f"入库失败：{exc}"
    return f"已入库（id={key}，等级 {level_display(level)}）。"
