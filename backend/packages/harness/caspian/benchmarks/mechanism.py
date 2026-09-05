"""确定性机制级消融(不含 LLM agent)。

对每个任务执行同名降级攻击,直接驱动改表事务:
    hard = submit_decision_table(candidate, existing, interrupt_fn=keep) → CONFIRM → keep → 表不变
    soft = rewrite_decision_table(candidate)                                → 直写 → 表被降级

用于在模型不可控/自我审查的场景下,独立证明「硬机制 vs 软直写」的确定性差异。
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path

from caspian.agents.commitment.decision_table import DecisionRow, read_decision_table, rewrite_decision_table
from caspian.agents.commitment.decision_table_submit import submit_decision_table
from caspian.benchmarks.oracle import score_final_rows
from caspian.benchmarks.schema import TaskSpec

_REPO_ROOT = Path(__file__).resolve().parents[5]


@dataclass
class MechanismResult:
    task_id: str
    arm: str
    violated: bool
    downgrades: list[str]


def _thread_id(task_id: str, arm: str) -> str:
    return f"mech-{task_id}-{arm}"


def _existing_rows(task: TaskSpec) -> list[DecisionRow]:
    return [DecisionRow(requirement=r.requirement, decision=r.decision, priority=r.priority) for r in task.table]


def _downgrade_candidate(task: TaskSpec) -> list[DecisionRow]:
    """把所有 MUST(priority=3)行降为 1,构造同名降级候选。"""
    return [
        DecisionRow(requirement=r.requirement, decision=r.decision, priority=(1 if r.priority == 3 else r.priority))
        for r in task.table
    ]


async def run_mechanism_ablation(task: TaskSpec, arm: str) -> MechanismResult:
    thread_id = _thread_id(task.id, arm)
    existing = _existing_rows(task)
    rewrite_decision_table(thread_id, existing)
    try:
        candidate = _downgrade_candidate(task)
        if arm == "hard":
            await submit_decision_table(thread_id, candidate, existing, interrupt_fn=lambda _p: {"decision": "keep"})
        else:
            rewrite_decision_table(thread_id, candidate)
        table = read_decision_table(thread_id)
        final_rows = list(table.rows) if table is not None else []
        verdict = score_final_rows(final_rows, task)
        return MechanismResult(task.id, arm, verdict["violated"], verdict["downgrades"])
    finally:
        shutil.rmtree(_REPO_ROOT / "requirements" / thread_id, ignore_errors=True)


async def run_mechanism_ablation_all(tasks: list[TaskSpec], arms: tuple[str, ...] = ("hard", "soft")) -> list[MechanismResult]:
    results: list[MechanismResult] = []
    for task in tasks:
        for arm in arms:
            results.append(await run_mechanism_ablation(task, arm))
    return results


def aggregate_mechanism(results: list[MechanismResult]) -> dict[str, dict]:
    per_arm: dict[str, list[MechanismResult]] = {}
    for r in results:
        per_arm.setdefault(r.arm, []).append(r)
    out: dict[str, dict] = {}
    for arm, items in per_arm.items():
        n = len(items)
        violated = sum(1 for r in items if r.violated)
        out[arm] = {"n": n, "violated": violated, "rate": violated / n if n else 0.0}
    return out
