"""本文件对外提供承诺层离线 benchmark harness。

输入为模拟委派器、InMemorySaver 与阶段响应；输出为阶段序列及人工中断记录。
工作流运行 supervisor，自动恢复人工节点并收集 ToolMessage 阶段结果。
示例：`python -m caspian.benchmarks.commitment.harness`。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from caspian.agents.commitment.workflow import _build_supervisor
from caspian.benchmarks.commitment.mock_delegator import MockDelegator

_REPO_ROOT = Path(__file__).resolve().parents[6]


def _base_state(thread_id: str, source_text: str) -> dict:
    return {
        "messages": [HumanMessage(content=source_text)],
        "stage": 0,
        "awaiting_human": None,
        "artifacts": {},
        "source_text": source_text,
        "thread_id": thread_id,
        "knowledge_files": [],
    }


def _stage_sequence(result: dict) -> list[int]:
    stages: list[int] = []
    for msg in result.get("messages", []):
        if not isinstance(msg, ToolMessage):
            continue
        try:
            payload = json.loads(str(msg.content))
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict) and "stage" in payload:
            stages.append(int(payload["stage"]))
    return stages


async def run_supervisor(source_text: str, thread_id: str) -> dict:
    """跑一次 supervisor,自动 approve 所有人工节点,返回阶段序列与中断阶段。"""
    delegator = MockDelegator()
    supervisor = _build_supervisor(delegator)
    supervisor.checkpointer = InMemorySaver()
    config = {"configurable": {"thread_id": thread_id}}

    state: dict | Command = _base_state(thread_id, source_text)
    interrupts: list[int] = []
    result: dict = {}
    async def no_table_operation(_state, _artifacts):
        return SimpleNamespace(
            operation_id="benchmark-table-operation", status="committed",
            changes=[], checks=[], result_revision=1,
        )

    with patch("caspian.agents.commitment.workflow.propose_contract_table", no_table_operation):
        while True:
            result = await supervisor.ainvoke(state, config=config)
            if result.get("__interrupt__"):
                for it in result["__interrupt__"]:
                    interrupts.append(int(it.value.get("stage", 0)))
                state = Command(resume={"decision": "approve"})
                continue
            break

    return {
        "stages": _stage_sequence(result),
        "interrupts": interrupts,
        "final_stage": int(result.get("stage", 0)),
    }


def cleanup(thread_id: str) -> None:
    shutil.rmtree(_REPO_ROOT / "requirements" / thread_id, ignore_errors=True)
    (Path(_REPO_ROOT) / "knowledge" / "React-19.0.0.md").unlink(missing_ok=True)
