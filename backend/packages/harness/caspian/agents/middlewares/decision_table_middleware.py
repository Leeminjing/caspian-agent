"""
本文件对外提供 DecisionTableMiddleware 类，作为决策等级表的版本去重注入中间件。

对外提供:
    DecisionTableMiddleware(AgentMiddleware) — 覆盖 before_agent / abefore_agent 钩子，
    读取当前 thread 的决策等级表并以固定 message id 注入 SystemMessage（含等级表与仲裁规则），
    版本一致时跳过注入（零 token 去重）

输入:
    before_agent / abefore_agent:
        state: AgentState — 当前 agent 状态（含 messages，可含历史注入的等级表消息）
        runtime: ToolRuntime — LangGraph 运行时（含 execution_info.thread_id）

输出:
    dict | None — 需要注入时返回 {"messages": [SystemMessage]} 状态增量；
                  等级表不存在或版本一致时返回 None

具体工作流:
    (1) 从 runtime.execution_info 获取 thread_id，无法获取时跳过
    (2) 读取 requirements/{thread_id}/decision-table.md，不存在时跳过
    (3) 扫描 state.messages 找 id="decision-table" 的 SystemMessage，解析其内嵌版本
    (4) 版本与磁盘一致 → 返回 None（已在上下文中，零 token）
    (5) 版本不一致或不存在 → 返回固定 id 的 SystemMessage（等级表全文 + 仲裁规则文本），
        add_messages reducer 按 id 原位替换，消息历史始终只有一份等级表

示例:
    from caspian.agents.middlewares.decision_table_middleware import DecisionTableMiddleware

    middleware = DecisionTableMiddleware()
    # 在 create_agent(middleware=[..., middleware]) 中使用
"""

import logging
import re

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import AgentState
from langchain_core.messages import SystemMessage
from langgraph.prebuilt import ToolRuntime

from caspian.agents.commitment.decision_table import DecisionTable, read_decision_table

logger = logging.getLogger(__name__)

_MESSAGE_ID = "decision-table"

_VERSION_PATTERN = re.compile(r'<decision_table version="([^"]+)"')

_ARBITRATION_RULES = """<decision_table_instructions>
This is the thread's decision LEVEL TABLE (决策等级表). It contains human-approved decisions, and each decision carries a LEVEL (等级): 3=必须 must, 2=可协商 negotiable, 1=可选 optional. The LEVEL is the governing mechanism for decision conflicts — when decisions clash, the LEVEL decides which one wins.

Before proposing any new requirement or decision, scan ALL entries in this table for conflicts. Conflicts include semantic ones (wording changes, technology substitutions, and other surface-unrelated clashes) - not just exact text matches.

Conflict governance by LEVEL:
- New decision conflicts with an entry, and its level is LOWER than the entry's level → you MUST abandon the new decision and follow the existing entry. Do not execute, propose, or argue for it.
- New decision conflicts with an entry, and its level is EQUAL or HIGHER, or its level cannot be determined → you MUST stop and ask the user to confirm before proceeding.

LEVEL comparison is numeric: 3 > 2 > 1. Compare with code-like rigor, never guess the comparison result.
</decision_table_instructions>"""


def _build_content(table: DecisionTable) -> str:
    """组装等级表注入内容（受保护 helper）。

    输入:
        table: DecisionTable — 决策等级表实例

    输出:
        str — 等级表全文（含版本标记）+ 仲裁规则文本
    """
    rows_md = "\n".join(
        f"| {row.requirement} | {row.decision} | {row.priority} |"
        for row in table.rows
    )
    return (
        f'<decision_table version="{table.version}" updated="{table.updated}">\n'
        f"{rows_md}\n"
        f"</decision_table>\n\n"
        f"{_ARBITRATION_RULES}"
    )


def _injected_version(content: str) -> str | None:
    """从已注入的 SystemMessage 内容解析版本号（受保护 helper）。

    输入:
        content: str — SystemMessage 内容

    输出:
        str | None — 内嵌版本号，未找到返回 None
    """
    match = _VERSION_PATTERN.search(content)
    return match.group(1) if match else None


class DecisionTableMiddleware(AgentMiddleware):

    def _inject_decision_table(
        self, state: AgentState, runtime: ToolRuntime
    ) -> dict | None:
        """核心逻辑：读取等级表，版本一致跳过，否则固定 id 注入。

        输入:
            state: AgentState — 当前 agent 状态
            runtime: ToolRuntime — LangGraph 运行时

        输出:
            dict | None — {"messages": [SystemMessage]} 状态增量，无等级表或版本一致时返回 None
        """
        thread_id = None
        if runtime.execution_info is not None:
            thread_id = runtime.execution_info.thread_id
        if thread_id is None:
            logger.warning("DecisionTableMiddleware: 无法获取 thread_id，跳过注入")
            return None

        table = read_decision_table(str(thread_id))
        if table is None:
            return None

        for message in reversed(state.get("messages", [])):
            if isinstance(message, SystemMessage) and message.id == _MESSAGE_ID:
                if _injected_version(str(message.content)) == table.version:
                    return None
                break

        logger.info(
            "DecisionTableMiddleware: 注入等级表 (thread_id=%s, version=%s)",
            thread_id,
            table.version,
        )
        return {
            "messages": [
                SystemMessage(content=_build_content(table), id=_MESSAGE_ID)
            ]
        }

    def before_agent(self, state: AgentState, runtime: ToolRuntime) -> dict | None:
        """同步钩子：agent 执行前注入等级表。

        输入:
            state: AgentState — 当前 agent 状态
            runtime: ToolRuntime — LangGraph 运行时

        输出:
            dict | None — 状态增量，无等级表或版本一致时返回 None
        """
        try:
            return self._inject_decision_table(state, runtime)
        except Exception:
            logger.error("DecisionTableMiddleware.before_agent 异常，跳过注入", exc_info=True)
            return None

    async def abefore_agent(self, state: AgentState, runtime: ToolRuntime) -> dict | None:
        """异步钩子：逻辑与同步版本同构。

        输入:
            state: AgentState — 当前 agent 状态
            runtime: ToolRuntime — LangGraph 运行时

        输出:
            dict | None — 状态增量，无等级表或版本一致时返回 None
        """
        try:
            return self._inject_decision_table(state, runtime)
        except Exception:
            logger.error("DecisionTableMiddleware.abefore_agent 异常，跳过注入", exc_info=True)
            return None
