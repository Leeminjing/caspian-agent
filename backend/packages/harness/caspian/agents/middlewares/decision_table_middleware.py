"""本文件对外提供 DecisionTableMiddleware，在模型发送边界注入唯一有效决策表。

输入为 ModelRequest、认证 Run 上下文和权威表；输出为含完整当前表的请求及调用修订记录。
工作流每轮重新读表、剔除旧注入、验证唯一性，先记调用版本再发送；任何读取或验证失败均阻止模型调用。
示例：`create_agent(middleware=[DecisionTableMiddleware(actor_id="lead")])`。
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain_core.messages import SystemMessage

from caspian.config.decision_review_config import DecisionReviewConfig
from caspian.decision_governance.adapter import service
from caspian.decision_governance.convergence_runtime import hash_messages, missing_shared_messages
from caspian.decision_governance.identity import logical_thread_id
from caspian.decision_governance.model_calls import begin_model_call, count_model_calls, finish_model_call, mark_model_call_sent
from caspian.decision_governance.policy import Actor
from caspian.decision_governance.view import TableView, bind_model_request
from caspian.persistence.engine import get_session


def _identity(request: ModelRequest) -> tuple[str, str, str]:
    runtime = request.runtime
    context = runtime.context if runtime and isinstance(runtime.context, dict) else {}
    user_id = context.get("user_id")
    run_id = context.get("run_id")
    thread_id = logical_thread_id(runtime)
    if not all((user_id, run_id, thread_id)):
        raise RuntimeError("模型调用缺少已验证的用户、Run 或会话 ID")
    return str(user_id), str(thread_id), str(run_id)


class DecisionTableMiddleware(AgentMiddleware):
    def __init__(self, actor_id: str = "lead"):
        self._actor_id = actor_id

    async def abefore_model(self, state: dict, runtime: Any) -> dict | None:
        context = runtime.context if runtime and isinstance(runtime.context, dict) else {}
        writer = context.get("shared_checkpoint_writer")
        if writer is None:
            return None
        user_id = context.get("user_id")
        thread_id = logical_thread_id(runtime)
        if not user_id or not thread_id:
            raise RuntimeError("合流缺少已验证的用户或会话 ID")
        missing = await missing_shared_messages(
            get_session, str(user_id), str(thread_id), list(state.get("messages") or []), writer,
            dict(state.get("shared_message_hashes") or {}),
            operation_only=bool(context.get("is_subagent")),
        )
        hashes = dict(state.get("shared_message_hashes") or {})
        hashes.update(hash_messages([*(state.get("messages") or []), *missing]))
        if missing or hashes != (state.get("shared_message_hashes") or {}):
            return {"messages": missing, "shared_message_hashes": hashes}
        return None

    async def _prepare(self, request: ModelRequest) -> tuple[ModelRequest, str]:
        user_id, thread_id, run_id = _identity(request)
        table = await service().current(user_id, thread_id, Actor(self._actor_id, "agent"))
        bound = bind_model_request(request, TableView.from_snapshot(table))
        context = request.runtime.context if request.runtime and isinstance(request.runtime.context, dict) else {}
        app_config = context.get("app_config")
        review = getattr(app_config, "decision_review", DecisionReviewConfig())
        reminder_reason = None
        if review.enabled:
            previous = await count_model_calls(get_session, user_id, thread_id, run_id, self._actor_id)
            if previous > 0 and previous % review.reminder_interval == 0:
                reminder_reason = "periodic"
                bound = bound.override(system_message=SystemMessage(content=(
                    f"{bound.system_message.text}\n\n"
                    f'<decision_review_reminder reason="periodic" round="{previous + 1}" table_revision="{table.revision}">'
                    "请对照当前有效决策表检查整体方向，指出涉及条目、冲突或无法确认之处，再继续。"
                    "</decision_review_reminder>"
                )))
        call_id = await begin_model_call(
            get_session, user_id=user_id, thread_id=thread_id, run_id=run_id,
            actor_id=self._actor_id, table=table, request=bound,
            reminder_reason=reminder_reason,
        )
        return bound, call_id

    async def awrap_model_call(self, request: ModelRequest, handler: Any) -> Any:
        bound, call_id = await self._prepare(request)
        try:
            await mark_model_call_sent(get_session, call_id)
            result = await handler(bound)
        except Exception as exc:
            await finish_model_call(get_session, call_id, "failed", f"{type(exc).__name__}: {exc}")
            raise
        await finish_model_call(get_session, call_id, "completed")
        return result

    def wrap_model_call(self, request: ModelRequest, handler: Any) -> Any:
        bound, call_id = asyncio.run(self._prepare(request))
        try:
            asyncio.run(mark_model_call_sent(get_session, call_id))
            result = handler(bound)
        except Exception as exc:
            asyncio.run(finish_model_call(get_session, call_id, "failed", f"{type(exc).__name__}: {exc}"))
            raise
        asyncio.run(finish_model_call(get_session, call_id, "completed"))
        return result
