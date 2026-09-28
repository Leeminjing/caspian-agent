"""本文件对外提供 update_decision_table 工具，提交可审计的决策表变更。

输入为 add/update/remove、稳定行 ID、决策字段、修改理由及 ToolRuntime 中已验证的会话和 Run。
输出为操作 ID、状态和新修订或拒绝原因；工作流先读取基准快照、构造候选，再由统一服务判定权限、检查和提交。
示例：`await update_decision_table.ainvoke({"operation": "add", "requirement": "必须加密", "reason": "用户明确要求"})`。
"""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime

from caspian.decision_governance.adapter import detailed_result_text, service
from caspian.decision_governance.domain import Row
from caspian.decision_governance.identity import logical_thread_id
from caspian.decision_governance.policy import Actor


def _candidate(
    rows: tuple[Row, ...], operation: str, row_id: str, requirement: str,
    decision: str, priority: int, new_id: str,
) -> tuple[Row, ...]:
    if operation == "add":
        if not requirement.strip() or len(requirement) > 200:
            raise ValueError("新增要求必须为 1 到 200 字符")
        if any(row.requirement == requirement for row in rows):
            raise ValueError("要求已存在，请使用 update")
        return (*rows, Row(new_id, requirement, decision, priority))
    target = next((row for row in rows if row.id == row_id), None)
    if target is None:
        raise ValueError("目标行 ID 不存在")
    if operation == "remove":
        return tuple(row for row in rows if row.id != row_id)
    if operation == "update":
        updated = Row(row_id, requirement or target.requirement, decision, priority, target.guards)
        return tuple(updated if row.id == row_id else row for row in rows)
    raise ValueError("operation 只允许 add、update、remove")


@tool
async def update_decision_table(
    operation: str,
    reason: str,
    requirement: str = "",
    decision: str = "保留",
    priority: int = 3,
    id: str = "",
    runtime: ToolRuntime = None,
) -> str:
    """Propose an auditable add, update, or remove operation on the decision table.

    Args:
        operation: add, update, or remove.
        reason: Why this exact change is needed.
        requirement: Required for add, optional for update.
        decision: 保留 or 丢弃 for add or update.
        priority: Decision level 0 to 3 for add or update.
        id: Stable row ID for update or remove.
    """
    if runtime is None or runtime.execution_info is None:
        return "无法确认当前会话，改表未执行"
    thread_id = logical_thread_id(runtime)
    context = runtime.context if isinstance(runtime.context, dict) else {}
    user_id = context.get("user_id")
    config = runtime.config.get("configurable", {}) if isinstance(runtime.config, dict) else {}
    run_id = config.get("run_id")
    call_id = runtime.tool_call_id
    if not all((thread_id, user_id, run_id, call_id)):
        return "缺少已验证的用户、Run 或工具调用 ID，改表未执行"
    table_service = service()
    key = f"tool:{call_id}"
    try:
        previous = await table_service.find_by_key(str(user_id), str(thread_id), key)
        base = (
            await table_service.revision(str(user_id), str(thread_id), previous.base_revision)
            if previous is not None
            else await table_service.current_for_submission(str(user_id), str(thread_id))
        )
        new_id = str(uuid5(NAMESPACE_URL, f"{thread_id}:{call_id}:row"))
        candidate = _candidate(
            base.rows, operation, id.strip(), requirement.strip(), decision,
            priority, new_id,
        )
        operation_record = await table_service.submit(
            user_id=str(user_id), thread_id=str(thread_id),
            actor=Actor("lead", "agent"), run_id=str(run_id), source="agent_tool",
            idempotency_key=key, base_revision=base.revision,
            rows=candidate, reason=reason,
        )
        return await detailed_result_text(table_service, operation_record)
    except (PermissionError, ValueError, LookupError) as exc:
        return f"决策表改表被拒绝：{exc}"
