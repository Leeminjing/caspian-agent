"""
本文件对外提供 update_decision_table 内置工具，供 agent 在普通对话中更新当前 thread 的决策等级表。

对外提供:
    update_decision_table — LangChain @tool，单次单操作增/删/改等级表条目，机械校验防瞎改

输入:
    operation: str — add（新增）/ update（更新）/ remove（删除）
    requirement: str — 条目要求文本（非空，≤200 字符）
    decision: str — 条目决策（保留 / 丢弃），add/update 时校验
    priority: int — 条目等级（1/2/3），add/update 时校验
    runtime: ToolRuntime — LangGraph 运行时注入，从中取当前 thread_id

输出:
    str — 操作结果：成功返回新 version，校验失败或前置不满足返回错误说明（不修改文件）

具体工作流:
    (1) 从 runtime.execution_info 获取当前 thread_id（不可由参数指定，限定作用域）
    (2) 读取当前 thread 的决策等级表，不存在时视为空表
    (3) 校验操作合法性:add 时同 requirement 不得已存在；update/remove 时 requirement 必须存在
    (4) 校验条目字段:requirement 非空且 ≤200 字符；add/update 时 decision∈{保留,丢弃}、priority∈{1,2,3}
    (5) 执行单次操作并重写文件，返回新 version；任何校验失败不落盘

示例:
    result = update_decision_table(operation="add", requirement="必须使用 Supabase", decision="保留", priority=3, runtime=runtime)
    # → "决策等级表已更新，新版本 6d1cee0cec81"
"""

from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime

from caspian.agents.commitment.decision_table import (
    DecisionRow,
    read_decision_table,
    rewrite_decision_table,
)

_MAX_REQUIREMENT_LENGTH = 200
_VALID_DECISIONS = {"保留", "丢弃"}
_VALID_PRIORITIES = {1, 2, 3}


def _validate_requirement(requirement: str) -> str | None:
    """校验要求文本（受保护 helper）。

    输入:
        requirement: str — 待校验的要求文本

    输出:
        str | None — 校验失败返回错误说明，通过返回 None
    """
    if not requirement or not requirement.strip():
        return "requirement 不能为空"
    if len(requirement) > _MAX_REQUIREMENT_LENGTH:
        return f"requirement 不能超过 {_MAX_REQUIREMENT_LENGTH} 字符"
    return None


def _validate_entry(decision: str, priority: int) -> str | None:
    """校验条目字段（受保护 helper）。

    输入:
        decision: str — 条目决策
        priority: int — 条目等级

    输出:
        str | None — 校验失败返回错误说明，通过返回 None
    """
    if decision not in _VALID_DECISIONS:
        return f"decision 只允许 {'/'.join(sorted(_VALID_DECISIONS))}"
    if priority not in _VALID_PRIORITIES:
        return "priority 只允许 1、2、3"
    return None


@tool
def update_decision_table(
    operation: str,
    requirement: str,
    decision: str = "保留",
    priority: int = 3,
    runtime: ToolRuntime = None,
) -> str:
    """Update the thread's decision LEVEL TABLE (决策等级表) — add / update / remove one entry.

    Use this tool to modify the thread's decision level table during normal conversation.
    The level table holds human-approved decisions; every entry carries a LEVEL (等级):
    3=必须 must, 2=可协商 negotiable, 1=可选 optional. The LEVEL governs conflict
    resolution — a higher-level decision overrides lower-level ones.

    When to use the update_decision_table tool:
    - The user explicitly asks to add, change, or remove a decision/requirement entry
    - A decision made in conversation should become binding for future runs

    When NOT to use the update_decision_table tool:
    - The user is merely discussing an option without deciding — confirm intent first
    - A conflicting decision exists with higher level — ask the user instead of overwriting

    Args:
        operation: One of "add", "update", "remove".
        requirement: The requirement text of the entry (non-empty, max 200 chars).
        decision: Entry decision ("保留" or "丢弃"). Used for add/update.
        priority: Entry LEVEL (等级, 1, 2 or 3). Used for add/update.
    """
    thread_id = None
    if runtime is not None and runtime.execution_info is not None:
        thread_id = runtime.execution_info.thread_id
    if thread_id is None:
        return "无法获取当前 thread ID，拒绝更新"

    table = read_decision_table(str(thread_id))
    rows = list(table.rows) if table is not None else []

    requirement = str(requirement).strip()
    if operation not in {"add", "update", "remove"}:
        return "operation 只允许 add / update / remove"

    if error := _validate_requirement(requirement):
        return error

    existing = [row for row in rows if row.requirement == requirement]
    if operation == "add":
        if existing:
            return f"条目 '{requirement}' 已存在，如需修改请使用 update"
        if error := _validate_entry(decision, priority):
            return error
        rows.append(DecisionRow(requirement=requirement, decision=decision, priority=priority))
    elif operation == "update":
        if not existing:
            return f"条目 '{requirement}' 不存在，如需新增请使用 add"
        if error := _validate_entry(decision, priority):
            return error
        rows = [
            DecisionRow(requirement=requirement, decision=decision, priority=priority)
            if row.requirement == requirement
            else row
            for row in rows
        ]
    else:  # remove
        if not existing:
            return f"条目 '{requirement}' 不存在"
        rows = [row for row in rows if row.requirement != requirement]

    version = rewrite_decision_table(str(thread_id), rows)
    if version is None:
        return "写入决策等级表失败，请重试"
    return f"决策等级表已更新，新版本 {version}"
