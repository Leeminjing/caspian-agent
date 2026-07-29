"""
本文件对外提供 CommitmentMiddleware，作为 lead agent 执行前的可拆卸承诺层入口。

输入:
    model — 承诺层内部 Worker 和 Evaluator 使用的 BaseChatModel。
    context7_tools — 只提供给承诺子图的 Context7 BaseTool 列表。
    AgentState / runtime — lead agent 当前消息状态和包含 thread_id 的运行时信息。

输出:
    None — 已存在 task_contract 或没有消息时跳过承诺层。
    dict[str, Any] — 完成后写入 task_contract，并用合同 HumanMessage 替换原消息。
    GraphInterrupt — 人工确认节点暂停时向父图传播的中断。

具体工作流:
    (1) before_agent 检查当前 thread 是否已经存在任务合同。
    (2) 将原始消息作为 Supervisor messages 起点并启动隔离的九阶段子图。
    (3) 将子图 interrupt 原样传播给现有 run/resume 链路。
    (4) 子图完成后校验 stage、合同和最终消息。
    (5) 返回状态更新，由 LangGraph reducer 清空原消息并注入合同 HumanMessage。

示例:
    middleware = CommitmentMiddleware(model, context7_tools)
"""

import asyncio
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import AgentState
from langchain.messages import HumanMessage, RemoveMessage
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.errors import GraphInterrupt
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from caspian.agents.commitment.delegation import ReviewedDelegator
from caspian.agents.commitment.schemas import CommitmentState
from caspian.agents.commitment.tracing import _write_commitment_messages
from caspian.agents.commitment.workflow import _build_supervisor

class CommitmentMiddleware(AgentMiddleware):
    state_schema = CommitmentState

    def __init__(
        self,
        model: BaseChatModel,
        context7_tools: list[BaseTool],
    ) -> None:
        super().__init__()
        delegator = ReviewedDelegator(model, context7_tools)
        self._supervisor = _build_supervisor(delegator)

    async def _run(self, state: AgentState, runtime: Any) -> dict[str, Any] | None:
        if state.get("task_contract"):
            return None
        messages = state.get("messages", [])
        if not messages:
            return None
        thread_id = getattr(getattr(runtime, "execution_info", None), "thread_id", None)
        if thread_id is None:
            raise ValueError("CommitmentMiddleware 无法获取 thread_id")
        result: dict[str, Any] = {}
        async for mode, chunk in self._supervisor.astream(
            {
                "messages": list(messages),
                "stage": 0,
                "awaiting_human": None,
                "artifacts": {},
                "thread_id": str(thread_id),
                "knowledge_files": [],
            },
            stream_mode=["values", "custom"],
        ):
            if mode == "custom":
                _write_commitment_messages(chunk)
            elif mode == "values":
                result = chunk
                _write_commitment_messages(
                    {
                        "type": "commitment_messages",
                        "actor": "supervisor",
                        "stage": int(chunk.get("stage", 0)),
                        "messages": list(chunk.get("messages", [])),
                    }
                )
        if interrupts := result.get("__interrupt__"):
            raise GraphInterrupt(interrupts)
        if result.get("stage") != 9:
            raise RuntimeError(
                f"承诺流程异常终止于 stage {result.get('stage', 0)}"
            )
        contract = str(result.get("task_contract", ""))
        final_message = str(result.get("final_message", ""))
        if not contract or not final_message:
            raise RuntimeError("承诺流程未产出合同或最终消息")
        return {
            "task_contract": contract,
            "messages": [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                HumanMessage(content=final_message),
            ],
        }

    def before_agent(self, state: AgentState, runtime: Any) -> dict[str, Any] | None:
        return asyncio.run(self._run(state, runtime))

    async def abefore_agent(
        self, state: AgentState, runtime: Any
    ) -> dict[str, Any] | None:
        return await self._run(state, runtime)
