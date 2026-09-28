"""本文件对外提供候选决策表的结构与语义检查适配器。

输入为完整候选行和当前有效行；输出为与授权独立的检查事实和是否需要审批。
工作流复用现有语义扫描，保留冲突理由与候选、旧行的对应关系；扫描不可用时要求人工确认。
示例：`findings = await check_candidate(candidate, current)`。
"""

from __future__ import annotations

from caspian.agents.commitment.decision_table import DecisionRow, Guard
from caspian.agents.commitment.decision_table_detect import detect_decision_table
from caspian.decision_governance.domain import Row


def _legacy_rows(rows: tuple[Row, ...]) -> list[DecisionRow]:
    converted: list[DecisionRow] = []
    for row in rows:
        guards = [Guard.from_dict(value) for value in row.guards]
        if any(guard is None for guard in guards):
            raise ValueError(f"决策行 {row.id} 含非法守卫")
        converted.append(DecisionRow(
            id=row.id, requirement=row.requirement, decision=row.decision,
            priority=row.priority, guards=guards,
        ))
    return converted


async def check_candidate(
    candidate: tuple[Row, ...], current: tuple[Row, ...], model=None
) -> tuple[dict, ...]:
    verdict = await detect_decision_table(_legacy_rows(candidate), _legacy_rows(current), model)
    return ({
        "kind": "semantic_conflict_scan",
        "requires_approval": verdict.require_confirm,
        "reasons": list(verdict.reasons),
        "recommendation": verdict.recommendation,
        "conflicts": [conflict.model_dump() for conflict in verdict.conflicts],
    },)
