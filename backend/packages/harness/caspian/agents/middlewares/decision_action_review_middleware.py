"""本文件对外提供 DecisionActionReviewMiddleware，在关键工具执行前取得可审计复核结论。

输入为待执行 tool_call、主 Agent 当前消息、配置和权威表；输出为执行结果或明确未执行的 ToolMessage。
工作流先登记绑定动作，再让模型检查具体参数与当前表；仅 keep 且版本不变时调用下游工具一次。
示例：`create_agent(middleware=[DecisionActionReviewMiddleware()])`。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from langgraph.types import interrupt

from caspian.config.decision_review_config import DecisionReviewConfig
from caspian.decision_governance.adapter import service
from caspian.decision_governance.identity import logical_thread_id
from caspian.decision_governance.policy import Actor
from caspian.decision_governance.review_model import review_action
from caspian.decision_governance.review_protocol import ActionBinding
from caspian.decision_governance.review_store import get_review, register_review, transition_review, update_review
from caspian.persistence.engine import get_session


def _identity(request: ToolCallRequest) -> tuple[str, str, str, str, dict]:
    runtime = request.runtime
    context = runtime.context if isinstance(runtime.context, dict) else {}
    configured = runtime.config.get("configurable", {}) if isinstance(runtime.config, dict) else {}
    user_id = context.get("user_id")
    thread_id = logical_thread_id(runtime)
    run_id = configured.get("run_id") or context.get("run_id")
    actor_id = "subagent" if context.get("is_subagent") else "lead"
    if not all((user_id, thread_id, run_id, request.tool_call.get("id"))):
        raise RuntimeError("关键动作缺少用户、会话、Run 或 tool_call_id")
    return str(user_id), str(thread_id), str(run_id), actor_id, context


def _not_executed(request: ToolCallRequest, reason: str) -> ToolMessage:
    return ToolMessage(
        content=f"[动作前复核] 工具未执行：{reason}",
        tool_call_id=request.tool_call.get("id", ""),
        name=request.tool_call.get("name", "unknown"),
        status="error",
    )


def _restore_result(request: ToolCallRequest, value: dict | None) -> ToolMessage:
    if not value or value.get("type") != "tool_message":
        return _not_executed(request, "动作已执行过，原始结果不可重建；不会重复执行")
    return ToolMessage(
        content=value["content"], tool_call_id=request.tool_call.get("id", ""),
        name=request.tool_call.get("name", "unknown"), status=value.get("status", "success"),
    )


def _human_choice(request: ToolCallRequest, review, conclusion: dict) -> dict:
    return interrupt({
        "type": "decision_action_review",
        "review_id": review.review_id,
        "run_id": review.run_id,
        "tool_call_id": request.tool_call.get("id"),
        "action_name": review.action_name,
        "args": request.tool_call.get("args", {}),
        "table_revision": review.table_revision,
        "content_hash": review.content_hash,
        "conclusion": conclusion,
        "allowed_decisions": ["cancel", "retry_after_table_change"] if conclusion.get("conflict") == "conflict" else ["cancel", "approve"],
    })


class DecisionActionReviewMiddleware(AgentMiddleware):
    async def _prepare(self, request: ToolCallRequest):
        user_id, thread_id, run_id, actor_id, context = _identity(request)
        table = await service().current(user_id, thread_id, Actor(actor_id, "agent"))
        binding = ActionBinding.create(
            request.tool_call["name"], request.tool_call.get("args", {}) or {},
            actor_id, table.revision,
        )
        review = await register_review(
            get_session, user_id=user_id, thread_id=thread_id,
            run_id=run_id, tool_call_id=request.tool_call["id"],
            binding=binding, content_hash=table.content_hash,
            action_args=request.tool_call.get("args", {}) or {},
        )
        if review.status == "pending_review":
            try:
                messages = request.state.get("messages", []) if isinstance(request.state, dict) else []
                conclusion = await review_action(
                    messages, binding, request.tool_call.get("args", {}) or {}, table,
                    context, get_session, user_id=user_id, thread_id=thread_id, run_id=run_id,
                )
            except Exception as exc:
                await transition_review(
                    get_session, review.review_id, expected_status="pending_review",
                    status="pending_review", error=f"{type(exc).__name__}: {exc}",
                )
                return review, table, None, f"复核未完成：{exc}"
            await transition_review(
                get_session, review.review_id, expected_status="pending_review",
                status="reviewed", conclusion=conclusion,
            )
            review = await get_review(get_session, review.review_id)
        return review, table, review.conclusion, None

    async def awrap_tool_call(
        self, request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        context = request.runtime.context if isinstance(request.runtime.context, dict) else {}
        app_config = context.get("app_config")
        config = getattr(app_config, "decision_review", DecisionReviewConfig())
        if not config.enabled or request.tool_call.get("name") not in config.critical_tools:
            return await handler(request)
        try:
            review, table, conclusion, error = await self._prepare(request)
        except Exception as exc:
            return _not_executed(request, f"复核闸门故障：{exc}")
        if error:
            return _not_executed(request, error)
        if review.status == "executed":
            return _restore_result(request, review.result)
        if review.status in {"executing", "execution_failed"}:
            return _not_executed(request, "上次执行结果尚未确认，保持暂停")
        if review.status in {"cancelled", "modified"}:
            return _not_executed(request, review.status)
        if conclusion is None:
            return _not_executed(request, "缺少复核结论")
        decision = conclusion["decision"]
        if review.status == "awaiting_human":
            choice = _human_choice(request, review, conclusion)
            if not isinstance(choice, dict) or choice.get("decision") not in {"cancel", "approve", "retry_after_table_change"}:
                return _not_executed(request, "人工结论无效，动作保持暂停")
            if choice["decision"] == "cancel":
                await update_review(get_session, review.review_id, status="cancelled", result={"human_decision": "cancel"})
                return _not_executed(request, "用户取消动作")
            if choice["decision"] == "retry_after_table_change":
                return _not_executed(request, "请先经授权改表，再重新提交动作")
            if conclusion.get("conflict") == "conflict":
                return _not_executed(request, "当前表冲突仍存在，不能直接批准动作")
            conclusion = {**conclusion, "decision": "keep", "human_decision": "approve"}
            await update_review(get_session, review.review_id, status="reviewed", conclusion=conclusion)
            decision = "keep"
        if decision != "keep":
            status = {"modify": "modified", "cancel": "cancelled", "ask_human": "awaiting_human"}[decision]
            await update_review(get_session, review.review_id, status=status)
            if decision == "ask_human":
                _human_choice(request, review, conclusion)
            detail = json.dumps(conclusion.get("replacement_args"), ensure_ascii=False) if decision == "modify" else conclusion["reason"]
            return _not_executed(request, f"{status}：{detail}")
        try:
            user_id, thread_id, _, actor_id, _ = _identity(request)
            latest = await service().current(user_id, thread_id, Actor(actor_id, "agent"))
        except Exception as exc:
            return _not_executed(request, f"执行前表校验失败：{exc}")
        if latest.version != table.version:
            return _not_executed(request, "复核后决策表已变化，需要重新复核")
        claimed = await transition_review(
            get_session, review.review_id, expected_status="reviewed", status="executing",
        )
        if not claimed:
            latest_review = await get_review(get_session, review.review_id)
            if latest_review.status == "executed":
                return _restore_result(request, latest_review.result)
            return _not_executed(request, "同一动作已被其他执行分支认领或复核状态已变化")
        try:
            result = await handler(request)
        except Exception as exc:
            await update_review(get_session, review.review_id, status="execution_failed", error=f"{type(exc).__name__}: {exc}")
            raise
        stored = (
            {"type": "tool_message", "content": result.content, "status": result.status}
            if isinstance(result, ToolMessage) else {"type": "command"}
        )
        await update_review(get_session, review.review_id, status="executed", result=stored)
        return result

    def wrap_tool_call(
        self, request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        async def async_handler(value):
            return handler(value)
        return asyncio.run(self.awrap_tool_call(request, async_handler))
