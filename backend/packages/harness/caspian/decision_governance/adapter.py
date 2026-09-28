"""本文件对外提供旧决策行到治理行的转换、服务构造及操作结果渲染。

输入为现有工具或承诺层的行结构；输出为稳定 ID 的领域行及带状态的用户说明。
工作流仅做协议转换，授权、检查和提交始终由 DecisionTableService 执行。
示例：`rows = to_rows(legacy_rows)`。
"""

from __future__ import annotations

import json
from uuid import uuid4

from caspian.agents.commitment.decision_table import DecisionRow
from caspian.decision_governance.domain import Row
from caspian.decision_governance.service import DecisionTableService
from caspian.persistence.engine import get_session


def to_rows(rows: list[DecisionRow]) -> tuple[Row, ...]:
    return tuple(Row(
        id=row.id or str(uuid4()),
        requirement=row.requirement,
        decision=row.decision,
        priority=row.priority,
        guards=tuple(guard.to_dict() for guard in row.guards),
    ) for row in rows)


def service() -> DecisionTableService:
    return DecisionTableService(get_session)


def result_text(operation) -> str:
    labels = {
        "processing": "处理中", "awaiting_approval": "等待有权主体审批，原表未改变",
        "committed": "已生效", "rejected": "审批拒绝，原表未改变",
        "cancelled": "已取消，原表未改变", "failed": "执行失败，原表未改变",
        "version_conflict": "版本冲突，原表未被本次请求修改，请刷新基准后重新确认",
    }
    label = "权限不足，原表未改变" if operation.status == "rejected" and operation.error == "权限不足" else labels.get(operation.status, operation.status)
    suffix = f"；新修订 {operation.result_revision}" if operation.result_revision is not None else ""
    return f"改表操作 {operation.operation_id}（Run {operation.run_id}）：{label}{suffix}"


async def detailed_result_text(table_service: DecisionTableService, operation) -> str:
    text = result_text(operation)
    if operation.status != "version_conflict":
        return text
    events = await table_service.operation_events(operation.operation_id)
    conflict = next((event.payload for event in reversed(events) if event.event_type == "version_conflict"), {})
    return f"{text}；当前修订 {conflict.get('current_revision')}；介入差异 {json.dumps(conflict.get('intervening_changes', []), ensure_ascii=False)}"
