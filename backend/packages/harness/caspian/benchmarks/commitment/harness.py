"""跑 supervisor 的 harness:mock delegator + InMemorySaver + 自动 resume 人工节点。

收集:
- stages:按顺序的阶段序列(从 ToolMessage payload 提取)
- interrupts:人工中断的阶段列表(3/5/6/7)
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

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
