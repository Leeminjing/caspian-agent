"""本文件对外提供 DecisionActionReviewMiddleware，作为受决策表约束工具的统一两阶段执行闸门。

输入为待执行工具调用、Run 工作消息、权威决策表和下游处理器；输出为配对的工具结果、明确未执行状态或执行结果未确认状态。
工作流登记动作绑定，先判断实际风险，高影响时完整复核，再做硬层守卫与原子执行认领；恢复时重建结果或暂停。
示例：`create_agent(middleware=[DecisionActionReviewMiddleware()])`。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command, interrupt

from caspian.agents.middlewares.decision_table_guard_middleware import DecisionTableGuardMiddleware, evaluate_guard
from caspian.agents.middlewares.sandbox_audit_middleware import SandboxAuditMiddleware, evaluate_sandbox, path_warning_required
from caspian.decision_governance.action_risk import context_fingerprint
from caspian.decision_governance.adapter import service
from caspian.decision_governance.identity import logical_thread_id
from caspian.decision_governance.policy import Actor
from caspian.decision_governance.review_model import review_action
from caspian.decision_governance.review_protocol import ActionBinding
from caspian.decision_governance.review_store import claim_execution, get_review, register_review, transition_review, update_review
from caspian.decision_governance.risk_model import assess_risk
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
        raise RuntimeError("动作缺少用户、会话、Run 或 tool_call_id")
    return str(user_id), str(thread_id), str(run_id), actor_id, context


def _not_executed(request: ToolCallRequest, reason: str) -> ToolMessage:
    return ToolMessage(
        content=f"[动作前复核] 工具未执行：{reason}",
        tool_call_id=request.tool_call.get("id", ""),
        name=request.tool_call.get("name", "unknown"), status="error",
    )


def _result_unconfirmed(request: ToolCallRequest, reason: str) -> ToolMessage:
    return ToolMessage(
        content=f"[动作前复核] 工具已开始执行，结果未确认：{reason}",
        tool_call_id=request.tool_call.get("id", ""),
        name=request.tool_call.get("name", "unknown"), status="error",
    )


def _restore_result(request: ToolCallRequest, value: dict | None) -> ToolMessage:
    if not value or value.get("type") != "tool_message":
        return _result_unconfirmed(request, "未再次执行")
    return ToolMessage(
        content=value["content"], tool_call_id=request.tool_call.get("id", ""),
        name=value.get("name") or request.tool_call.get("name", "unknown"),
        status=value.get("status", "success"),
    )


def _human_choice(request: ToolCallRequest, review, conclusion: dict) -> dict:
    return interrupt({
        "type": "decision_action_review", "review_id": review.review_id,
        "run_id": review.run_id, "tool_call_id": request.tool_call.get("id"),
        "action_name": review.action_name, "args": request.tool_call.get("args", {}),
        "table_revision": review.table_revision, "content_hash": review.content_hash,
        "conclusion": conclusion,
        "allowed_decisions": ["cancel", "retry_after_table_change"] if conclusion.get("conflict") != "none" else ["cancel", "approve"],
    })


class DecisionActionReviewMiddleware(AgentMiddleware):
    async def _prepare(self, request: ToolCallRequest):
        user_id, thread_id, run_id, actor_id, context = _identity(request)
        table = await service().current(user_id, thread_id, Actor(actor_id, "agent"))
        messages = list(request.state.get("messages", [])) if isinstance(request.state, dict) else []
        args = request.tool_call.get("args", {}) or {}
        binding = ActionBinding.create(
            request.tool_call["name"], args, actor_id, table.revision,
            context_fingerprint(messages, context),
        )
        review = await register_review(
            get_session, user_id=user_id, thread_id=thread_id,
            run_id=run_id, tool_call_id=request.tool_call["id"], binding=binding,
            content_hash=table.content_hash, action_args=args,
        )
        if review.status == "risk_pending":
            latest = await service().current(user_id, thread_id, Actor(actor_id, "agent"))
            if latest.version != table.version:
                await update_review(get_session, review.review_id, status="stale", error="风险判断前决策表版本已变化")
                return await get_review(get_session, review.review_id), table, "决策表已变化，需要重新判断"
            try:
                risk = await assess_risk(
                    messages, binding, args, table, context, get_session,
                    user_id=user_id, thread_id=thread_id, run_id=run_id,
                )
            except Exception as exc:
                risk = {"risk": "uncertain", "reason": f"轻量判断未完成：{type(exc).__name__}: {exc}"}
            next_status = "low_ready" if risk["risk"] == "low" else "review_pending"
            await transition_review(
                get_session, review.review_id, expected_status="risk_pending",
                status=next_status, risk_outcome=risk["risk"], risk_reason=risk["reason"],
            )
            review = await get_review(get_session, review.review_id)
        if review.status == "review_pending":
            try:
                latest = await service().current(user_id, thread_id, Actor(actor_id, "agent"))
                if latest.version != table.version:
                    await update_review(get_session, review.review_id, status="stale", error="完整复核前决策表版本已变化")
                    return await get_review(get_session, review.review_id), table, "决策表已变化，需要重新判断"
                conclusion = await review_action(
                    messages, binding, args, table,
                    {**context, "action_risk_reason": review.risk_reason}, get_session,
                    user_id=user_id, thread_id=thread_id, run_id=run_id,
                )
            except Exception as exc:
                await transition_review(
                    get_session, review.review_id, expected_status="review_pending",
                    status="review_pending", error=f"{type(exc).__name__}: {exc}",
                )
                return review, table, f"复核未完成：{exc}"
            await transition_review(
                get_session, review.review_id, expected_status="review_pending",
                status="reviewed", conclusion=conclusion,
            )
            review = await get_review(get_session, review.review_id)
        return review, table, None

    async def _resolve_conclusion(self, request: ToolCallRequest, review):
        if review.status in {"low_ready", "reviewed"}:
            if review.status == "low_ready":
                return None
            conclusion = review.conclusion
            if conclusion is None:
                return _not_executed(request, "缺少完整复核结论")
            decision = conclusion["decision"]
            if decision != "keep":
                status = {"modify": "modified", "cancel": "cancelled", "ask_human": "awaiting_human"}[decision]
                await update_review(get_session, review.review_id, status=status)
                if decision == "ask_human":
                    _human_choice(request, review, conclusion)
                detail = json.dumps(conclusion.get("replacement_args"), ensure_ascii=False) if decision == "modify" else conclusion["reason"]
                return _not_executed(request, f"{status}：{detail}")
            return None
        if review.status == "awaiting_human":
            choice = _human_choice(request, review, review.conclusion)
            if not isinstance(choice, dict) or choice.get("decision") not in {"cancel", "approve", "retry_after_table_change"}:
                return _not_executed(request, "人工结论无效，动作保持暂停")
            if choice["decision"] == "cancel":
                await update_review(get_session, review.review_id, status="cancelled", result={"human_decision": "cancel"})
                return _not_executed(request, "用户取消动作")
            if choice["decision"] == "retry_after_table_change":
                return _not_executed(request, "请先经授权改表，再重新提交动作")
            if review.conclusion.get("conflict") != "none":
                return _not_executed(request, "当前表冲突仍存在，不能直接批准动作")
            await update_review(get_session, review.review_id, status="reviewed", conclusion={**review.conclusion, "decision": "keep", "human_decision": "approve"})
            return None
        return _not_executed(request, review.error or f"动作状态为 {review.status}，保持未执行")

    async def awrap_tool_call(
        self, request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        try:
            review, table, error = await self._prepare(request)
        except Exception as exc:
            return _not_executed(request, f"动作闸门故障：{exc}")
        if error:
            return _not_executed(request, error)
        if review.status in {"executed", "execution_failed"}:
            return _restore_result(request, review.result)
        if review.status in {"executing", "indeterminate"}:
            return _restore_result(request, review.result)
        conclusion_result = await self._resolve_conclusion(request, review)
        if conclusion_result is not None:
            return conclusion_result
        user_id, thread_id, _, actor_id, context = _identity(request)
        args = request.tool_call.get("args", {}) or {}
        try:
            latest = await service().current(user_id, thread_id, Actor(actor_id, "agent"))
            messages = list(request.state.get("messages", [])) if isinstance(request.state, dict) else []
            binding = ActionBinding.create(request.tool_call["name"], args, actor_id, latest.revision, context_fingerprint(messages, context))
        except Exception as exc:
            return _not_executed(request, f"执行前状态校验失败：{exc}")
        if latest.version != table.version or binding.args_hash != review.args_hash or binding.context_hash != review.context_hash or binding.actor_id != review.actor_id:
            await update_review(get_session, review.review_id, status="stale", error="动作、参数、上下文或决策表已变化")
            return _not_executed(request, "执行前状态已变化，需要重新判断")
        disposition, row, guard = evaluate_guard(latest, review.action_name, args)
        if disposition == "block":
            await update_review(get_session, review.review_id, status="paused", error=f"硬层守卫拦截：{row.id}")
            return DecisionTableGuardMiddleware._make_block_message(request, row, guard)
        sandbox_level = evaluate_sandbox(review.action_name, args)
        if sandbox_level == "block":
            await update_review(get_session, review.review_id, status="paused", error="命中 shell 安全拦截规则")
            return SandboxAuditMiddleware._make_block_message(request)
        if not await claim_execution(get_session, review.review_id, review.status if review.status != "awaiting_human" else "reviewed"):
            current = await get_review(get_session, review.review_id)
            if current.status == "executed":
                return _restore_result(request, current.result)
            return _not_executed(request, current.error or "同一动作已被其他分支认领或表版本改变")
        try:
            result = await handler(request)
        except Exception as exc:
            await update_review(get_session, review.review_id, status="execution_failed", error=f"{type(exc).__name__}: {exc}")
            raise
        if disposition == "warn":
            result = DecisionTableGuardMiddleware._append_warning(result, row, guard)
        if SandboxAuditMiddleware._shell_type_from_name(review.action_name) is not None:
            command = args.get("command", "")
            if path_warning_required(command):
                result = SandboxAuditMiddleware._append_path_warning(result, command)
            if sandbox_level == "warn":
                result = SandboxAuditMiddleware._append_warning(result, review.action_name, command)
        stored = {"type": "tool_message", "content": result.content, "name": result.name, "status": result.status} if isinstance(result, ToolMessage) else {"type": "command"}
        final_status = "indeterminate" if not isinstance(result, ToolMessage) else "execution_failed" if result.status == "error" else "executed"
        try:
            await update_review(get_session, review.review_id, status=final_status, result=stored)
        except Exception:
            return _result_unconfirmed(request, "结果保存失败，未再次执行")
        return result

    def wrap_tool_call(
        self, request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        async def async_handler(value):
            return handler(value)
        return asyncio.run(self.awrap_tool_call(request, async_handler))
