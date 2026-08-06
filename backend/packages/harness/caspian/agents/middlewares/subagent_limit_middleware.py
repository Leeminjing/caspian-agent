"""
本文件对外提供 SubagentLimitMiddleware：单响应并发与单 run 委托总额的确定性硬限制。

对外提供:
    SubagentLimitMiddleware(AgentMiddleware) — after_model 钩子截断超额 task 工具调用

输入:
    max_concurrent: int — 单响应并发上限（默认 3，clamp [1,4]）
    max_total: int — 单 run 委托总额（默认 6，clamp [1,50]）
    state: AgentState — 当前图状态（含 messages 与 delegations 账本）
    runtime: Runtime — 运行时（context 中含 run_id）

输出:
    dict | None — 截断后的消息状态增量；未超限返回 None

具体工作流:
    (1) after_model 检查最后一条 AIMessage 的 task tool_calls 数量
    (2) 从 state.delegations 按 run_id 统计既往委托
    (3) 允许数 = min(max_concurrent, max_total - prior)；超限截断并同 id 替换 AIMessage
    (4) 总额耗尽 → 移除全部 task 调用 + 追加提示文本

示例:
    middleware = SubagentLimitMiddleware(max_concurrent=3, max_total=6)
"""

import logging

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import AgentState
from langchain.messages import AIMessage
from langchain_core.messages import AnyMessage
from typing_extensions import override

from caspian.config.subagents_config import (
    DEFAULT_MAX_TOTAL_SUBAGENTS_PER_RUN,
    clamp_subagent_concurrency,
    clamp_total_subagents_per_run,
)

logger = logging.getLogger(__name__)

_TOTAL_LIMIT_STOP_MSG = (
    "\n\n[SubagentLimit] The per-run subagent delegation limit has been reached for this run. "
    "Do not delegate more work; synthesize from the results already collected."
)


def _runtime_run_id(runtime) -> str | None:
    context = getattr(runtime, "context", None)
    if not isinstance(context, dict):
        return None
    run_id = context.get("run_id")
    return str(run_id) if run_id else None


def _delegation_run_id(entry: dict) -> str | None:
    run_id = entry.get("run_id")
    return str(run_id) if run_id else None


def _delegation_id(entry: dict) -> str | None:
    delegation_id = entry.get("id")
    return str(delegation_id) if delegation_id else None


def _count_prior_delegations(delegations: object, *, run_id: str | None) -> int:
    """按 run_id 统计既往委托数（run_id 缺失时全量保守计数）。"""
    if not isinstance(delegations, list):
        return 0
    ids: set[str] = set()
    for entry in delegations:
        if run_id is not None and _delegation_run_id(entry) != run_id:
            continue
        delegation_id = _delegation_id(entry)
        if delegation_id is not None:
            ids.add(delegation_id)
    return len(ids)


def _clone_ai_message_with_tool_calls(message: AIMessage, tool_calls: list[dict]) -> AIMessage:
    """以相同 message id 克隆 AIMessage 并替换 tool_calls（触发 reducer 原位替换）。"""
    return message.model_copy(update={"tool_calls": tool_calls})


class SubagentLimitMiddleware(AgentMiddleware[AgentState]):
    """截断单次模型响应中超额 task 工具调用，并限制单 run 委托总额。"""

    def __init__(self, max_concurrent: int = 3, max_total: int = DEFAULT_MAX_TOTAL_SUBAGENTS_PER_RUN):
        super().__init__()
        self.max_concurrent = clamp_subagent_concurrency(max_concurrent)
        self.max_total = clamp_total_subagents_per_run(max_total)

    def _truncate_task_calls(self, state: AgentState, runtime=None) -> dict | None:
        messages = state.get("messages", [])
        if not messages:
            return None

        last_msg = messages[-1]
        if not isinstance(last_msg, AIMessage):
            return None

        tool_calls = getattr(last_msg, "tool_calls", None)
        if not tool_calls:
            return None

        task_indices = [i for i, tc in enumerate(tool_calls) if tc.get("name") == "task"]
        if not task_indices:
            return None

        run_id = _runtime_run_id(runtime)
        if run_id is None:
            logger.warning(
                "SubagentLimit 未从 runtime context 获取 run_id，按全部既往委托保守计数"
            )
        prior = _count_prior_delegations(state.get("delegations"), run_id=run_id)
        remaining_total = max(0, self.max_total - prior)
        allowed_task_calls = min(self.max_concurrent, remaining_total)

        if len(task_indices) <= allowed_task_calls:
            return None

        indices_to_drop = set(task_indices[allowed_task_calls:])
        truncated_tool_calls = [
            tc for i, tc in enumerate(tool_calls) if i not in indices_to_drop
        ]
        logger.warning(
            "已截断 %s 个超额 task 调用 (并发上限: %s; 总额上限: %s; 既往委托: %s)",
            len(indices_to_drop),
            self.max_concurrent,
            self.max_total,
            prior,
        )

        content = _TOTAL_LIMIT_STOP_MSG if remaining_total == 0 else None
        updated_msg = _clone_ai_message_with_tool_calls(last_msg, truncated_tool_calls)
        if content:
            updated_msg = updated_msg.model_copy(update={"content": str(last_msg.content or "") + content})
        return {"messages": [updated_msg]}

    @override
    def after_model(self, state: AgentState, runtime) -> dict | None:
        return self._truncate_task_calls(state, runtime)

    @override
    async def aafter_model(self, state: AgentState, runtime) -> dict | None:
        return self._truncate_task_calls(state, runtime)
