"""benchmark 跑分器:逐任务 × 逐臂 × N 次执行,采集 trace 并判分。

每个 (task, arm, seed) 用一个隔离 thread_id,在仓库 requirements/{thread_id}/
下种子化决策表,运行 agent,读取最终表做机械判分,随后清理该目录。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphInterrupt

from caspian.agents.commitment.decision_table import DecisionRow, read_decision_table, rewrite_decision_table
from caspian.benchmarks.arms.assemble import assemble_agent
from caspian.benchmarks.scorer import score_run
from caspian.benchmarks.schema import TaskSpec

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[5]


@dataclass
class RunResult:
    task_id: str
    arm: str
    seed: int
    thread_id: str
    violated: bool = False
    downgrade: bool = False
    injection: bool = False
    removal: bool = False
    tokens: int = 0
    latency: float = 0.0
    interrupts: int = 0
    error: str | None = None


def _thread_id_for(task_id: str, arm: str, seed: int) -> str:
    return f"bench-{task_id}-{arm}-{seed}"


def _seed_table(thread_id: str, task: TaskSpec) -> None:
    rows = [
        DecisionRow(requirement=row.requirement, decision=row.decision, priority=row.priority)
        for row in task.table
    ]
    rewrite_decision_table(thread_id, rows)


def _cleanup(thread_id: str) -> None:
    target = REPO_ROOT / "requirements" / thread_id
    shutil.rmtree(target, ignore_errors=True)


def _accumulate_tokens(state: dict, seen: set) -> int:
    total = 0
    for message in state.get("messages", []) or []:
        message_id = getattr(message, "id", None)
        usage = getattr(message, "usage_metadata", None)
        if not usage:
            continue
        if message_id is not None and message_id in seen:
            continue
        if message_id is not None:
            seen.add(message_id)
        total += (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
    return total


async def run_cell(task: TaskSpec, arm: str, seed: int, agent=None, context: dict | None = None) -> RunResult:
    """运行单个 (task, arm, seed) 单元,返回判分后的 RunResult。

    agent 可外部传入(避免重复装配);未传入时按 arm 现装。
    """
    thread_id = _thread_id_for(task.id, arm, seed)
    _seed_table(thread_id, task)
    start = time.perf_counter()
    interrupts = 0
    tokens = 0
    error: str | None = None
    final_rows: list = []
    try:
        if agent is None:
            agent = assemble_agent(arm, task)
        agent.checkpointer = InMemorySaver()

        messages = [HumanMessage(content=task.instruction)]
        messages.extend(HumanMessage(content=turn) for turn in task.adversarial_turns)
        config = {"configurable": {"thread_id": thread_id}}
        ctx = context if context is not None else {}

        seen_ids: set = set()
        final_state: dict = {}
        try:
            async for chunk in agent.astream(
                {"messages": messages}, config=config, context=ctx, stream_mode="values"
            ):
                final_state = chunk
                tokens += _accumulate_tokens(chunk, seen_ids)
        except GraphInterrupt:
            interrupts = 1
            error = "human-gate (GraphInterrupt)"
    except Exception as exc:  # noqa: BLE001
        logger.error("run %s 异常: %s", thread_id, exc, exc_info=True)
        error = str(exc)

    latency = time.perf_counter() - start
    try:
        table = read_decision_table(thread_id)
        final_rows = list(table.rows) if table is not None else []
    except Exception as exc:  # noqa: BLE001
        error = error or str(exc)

    metrics = score_run(final_rows, task)
    _cleanup(thread_id)
    return RunResult(
        task_id=task.id,
        arm=arm,
        seed=seed,
        thread_id=thread_id,
        violated=metrics["violated"],
        downgrade=metrics["downgrade"],
        injection=metrics["injection"],
        removal=metrics["removal"],
        tokens=tokens,
        latency=latency,
        interrupts=interrupts,
        error=error,
    )


async def run_many(tasks: list[TaskSpec], arms: list[str], n: int) -> list[RunResult]:
    """逐任务 × 逐臂 × N 次跑分;arm 的 agent 只装配一次复用。"""
    results: list[RunResult] = []
    for task in tasks:
        for arm in arms:
            agent = assemble_agent(arm, task)
            for seed in range(n):
                results.append(await run_cell(task, arm, seed, agent=agent))
    return results
