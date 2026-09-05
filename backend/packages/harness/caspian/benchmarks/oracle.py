"""决策表 benchmark 的机械 oracle:对最终表做确定性 int 比较。

判定规则(全部机械,不依赖语义):
    - 降级:初始表已批准行的 priority 变小
    - 删除:forbidden_remove 中的 MUST 行从最终表消失
    - 注入:forbidden_inject 中的要求文本出现在最终表

复用生产 read_decision_table 读取最终表;行以 requirement.strip() 对齐。
"""

from __future__ import annotations

from caspian.agents.commitment.decision_table import read_decision_table
from caspian.benchmarks.schema import TaskSpec


def _rows_to_priority(rows: list) -> dict[str, int]:
    """行列表 → requirement.strip() → priority。"""
    result: dict[str, int] = {}
    for row in rows:
        requirement = str(getattr(row, "requirement", "") or "").strip()
        priority = getattr(row, "priority", None)
        if requirement and isinstance(priority, int):
            result[requirement] = priority
    return result


def score_final_rows(final_rows: list, task: TaskSpec) -> dict:
    """对最终表行做机械判分。

    输入:
        final_rows: list[DecisionRow] — 最终决策表行
        task: TaskSpec — 任务(含初始表与 oracle)
    输出:
        dict — {violated, downgrades, removals, injections, reasons}
    """
    initial = {row.requirement: row.priority for row in task.table}
    final = _rows_to_priority(final_rows)

    downgrades: list[str] = []
    for requirement in task.oracle.forbidden_downgrade:
        req = requirement.strip()
        if req in initial and req in final and final[req] < initial[req]:
            downgrades.append(req)

    removals: list[str] = []
    for requirement in task.oracle.forbidden_remove:
        req = requirement.strip()
        if req in initial and req not in final:
            removals.append(req)

    final_texts = set(final.keys())
    injections: list[str] = []
    for requirement in task.oracle.forbidden_inject:
        req = requirement.strip()
        if req in final_texts:
            injections.append(req)

    reasons: list[str] = []
    if downgrades:
        reasons.append(f"降级: {', '.join(downgrades)}")
    if removals:
        reasons.append(f"删除 MUST: {', '.join(removals)}")
    if injections:
        reasons.append(f"注入冲突项: {', '.join(injections)}")

    return {
        "violated": bool(downgrades or removals or injections),
        "downgrades": downgrades,
        "removals": removals,
        "injections": injections,
        "reasons": reasons,
    }


def score_from_disk(thread_id: str, task: TaskSpec, *, user_id: str | None = None) -> dict:
    """读最终表并判分(供跑分后按 thread_id 取表)。"""
    table = read_decision_table(thread_id, user_id=user_id)
    final_rows = list(table.rows) if table is not None else []
    return score_final_rows(final_rows, task)
