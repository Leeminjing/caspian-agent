"""
本文件对外提供 update_decision_table 内置工具，供 agent 在普通对话中更新当前 thread 的决策等级表。

对外提供:
    update_decision_table — LangChain @tool，以事务方式增/删/改等级表条目：
    机械校验 → 构造候选新表 → 调用共享 submit_decision_table（冲突检测 → 提交/中断）

输入:
    operation: str — add（新增）/ update（更新）/ remove（删除）
    id: str — 条目 id，update/remove 时用于定位（与 requirement 文本无关）
    requirement: str — 条目要求文本，add 必填、update 可选（非空，≤200 字符）
    decision: str — 条目决策（保留 / 丢弃），add/update 时校验
    priority: int — 条目等级（1/2/3），add/update 时校验
    runtime: ToolRuntime — LangGraph 运行时注入，从中取当前 thread_id / user_id

输出:
    str — 操作结果：检测通过提交返回新 version；冲突中断由用户裁定后返回结果；
          校验失败或前置不满足返回错误说明

具体工作流:
    (1) 从 runtime.execution_info 获取当前 thread_id、从 runtime.context 获取 user_id
    (2) 读取当前 thread 的决策等级表，不存在时视为空表
    (3) 校验操作合法性与字段，按 id 定位构造候选新表（add 生成新条目、update/remove 按 id 匹配）
    (4) 调用共享 submit_decision_table 执行冲突检测 → 提交/中断（与手工编辑同源）

示例:
    result = await update_decision_table(operation="add", requirement="必须使用 Supabase", decision="保留", priority=3, runtime=runtime)
    result = await update_decision_table(operation="update", id="abc123", priority=2, runtime=runtime)
"""

import logging

from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime

from caspian.agents.commitment.decision_table import DecisionRow, read_decision_table
from caspian.agents.commitment.decision_table_submit import submit_decision_table

logger = logging.getLogger(__name__)

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


def _build_candidate_rows(
    rows: list[DecisionRow],
    operation: str,
    row_id: str,
    requirement: str,
    decision: str,
    priority: int | None,
) -> tuple[list[DecisionRow] | None, str | None]:
    """构造候选新表条目（受保护 helper）。

    输入:
        rows: list[DecisionRow] — 当前表条目
        operation / row_id / requirement / decision / priority: 操作参数

    输出:
        tuple[list | None, str | None] — (候选条目，错误)；错误非空时候选为 None
    """
    if operation == "add":
        if any(row.requirement == requirement for row in rows):
            return None, f"条目 '{requirement}' 已存在，如需修改请使用 update"
        if priority is None:
            return None, "add 需要提供 priority"
        return rows + [DecisionRow(requirement=requirement, decision=decision, priority=priority)], None

    if operation == "update":
        if not row_id:
            return None, "update 需要提供 id"
        target = next((row for row in rows if row.id == row_id), None)
        if target is None:
            return None, f"条目 id '{row_id}' 不存在"
        if priority is None:
            return None, "update 需要提供 priority"
        updated = DecisionRow(
            requirement=requirement or target.requirement,
            decision=decision,
            priority=priority,
            id=target.id,
            guards=target.guards,
        )
        return [updated if row.id == row_id else row for row in rows], None

    if operation == "remove":
        if not row_id:
            return None, "remove 需要提供 id"
        if not any(row.id == row_id for row in rows):
            return None, f"条目 id '{row_id}' 不存在"
        return [row for row in rows if row.id != row_id], None

    return None, f"operation 只允许 {'/'.join(sorted({'add', 'update', 'remove'}))}"


@tool
async def update_decision_table(
    operation: str,
    requirement: str = "",
    decision: str = "保留",
    priority: int = 3,
    id: str = "",
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
        id: The entry id. Required for update/remove (locates the entry independently of text).
        requirement: The requirement text. Required for add, optional for update (max 200 chars).
        decision: Entry decision ("保留" or "丢弃"). Used for add/update.
        priority: Entry LEVEL (等级, 1, 2 or 3). Used for add/update.
    """
    thread_id = None
    if runtime is not None and runtime.execution_info is not None:
        thread_id = runtime.execution_info.thread_id
    if thread_id is None:
        return "无法获取当前 thread ID，拒绝更新"

    user_id = None
    try:
        ctx = runtime.context
        if isinstance(ctx, dict):
            user_id = ctx.get("user_id")
    except Exception:
        user_id = None

    table = read_decision_table(str(thread_id), user_id=str(user_id) if user_id else None)
    existing = list(table.rows) if table is not None else []
    expected_version = table.version if table is not None else None

    requirement = str(requirement).strip()
    if operation == "add":
        if error := _validate_requirement(requirement):
            return error
    if operation != "remove":
        if error := _validate_entry(decision, priority):
            return error

    effective_priority = priority if operation != "remove" else None
    candidate, build_error = _build_candidate_rows(
        existing, operation, str(id).strip(), requirement, decision, effective_priority
    )
    if build_error:
        return build_error

    # 共享事务：冲突检测 → 提交/中断（与手工编辑同源）
    return await submit_decision_table(
        str(thread_id),
        candidate,
        existing,
        user_id=str(user_id) if user_id else None,
        expected_version=expected_version,
    )
