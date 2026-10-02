"""本文件对外提供关键工具执行前的结构化模型复核步骤。

输入为 Agent 当前消息、待执行工具与参数、风险原因、认证主体和当前表；输出为绑定动作、上下文及版本的 keep/modify/cancel/ask_human 结论。
工作流以有超时限制的独立模型调用展示完整动作、升级原因与表，记录该调用版本，再验证返回 JSON 的因果绑定；内部调用不计主 Agent 周期轮次。
示例：`conclusion = await review_action(messages, binding, args, table, context, factory)`。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from langchain.agents.middleware import ModelRequest
from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy.ext.asyncio import AsyncSession

from caspian.decision_governance.domain import TableSnapshot
from caspian.decision_governance.model_calls import begin_model_call, finish_model_call, mark_model_call_sent
from caspian.decision_governance.review_protocol import ActionBinding, parse_conclusion
from caspian.decision_governance.view import TableView, bind_model_request
from caspian.models import create_chat_model

_REVIEW_PROMPT = (
    "你是 Agent 在关键工具执行前的复核步骤。只输出一个 JSON 对象。"
    "字段：action_name、args_hash、actor_id、table_revision、context_hash、examined_args（原样列出实际检查的参数）、related_rows（相关行 ID 列表）、"
    "conflict（none/conflict/uncertain）、decision（keep/modify/cancel/ask_human）、reason，"
    "modify 时还需 replacement_args 对象。风险升级原因也在待执行动作中。"
    "具体检查待执行动作及参数与当前有效决策表的关系；keep 理由须引用具体参数值或相关决策行 ID。冲突或无法确认不得 keep。"
    "需要推翻旧决策时选择 ask_human 并说明应先走授权改表流程。"
)


async def review_action(
    messages: list[Any], binding: ActionBinding, args: dict,
    table: TableSnapshot, context: dict, factory: Callable[[], AsyncSession],
    *, user_id: str, thread_id: str, run_id: str,
) -> dict:
    model = create_chat_model(
        name=context.get("model_name"), app_config=context.get("app_config"),
    )
    transcript = [message.model_dump(mode="json") if hasattr(message, "model_dump") else str(message) for message in messages]
    payload = {
        "conversation": transcript,
        "pending_action": {
            "action_name": binding.action_name,
            "args": args,
            "args_hash": binding.args_hash,
            "actor_id": binding.actor_id,
            "table_revision": binding.table_revision,
            "context_hash": binding.context_hash,
            "risk_reason": context.get("action_risk_reason"),
        },
    }
    request = ModelRequest(
        model=model,
        messages=[HumanMessage(content=json.dumps(payload, ensure_ascii=False, default=str))],
        system_message=SystemMessage(content=_REVIEW_PROMPT),
    )
    bound = bind_model_request(request, TableView.from_snapshot(table))
    reminder_reason = "critical_action"
    call_id = await begin_model_call(
        factory, user_id=user_id, thread_id=thread_id, run_id=run_id,
        actor_id="action_review", table=table, request=bound,
        reminder_reason=reminder_reason,
    )
    try:
        await mark_model_call_sent(factory, call_id)
        config = getattr(context.get("app_config"), "decision_review", None)
        timeout = getattr(config, "review_timeout_seconds", 60)
        response = await asyncio.wait_for(model.ainvoke([bound.system_message, *bound.messages]), timeout=timeout)
        conclusion = parse_conclusion(response.text, binding, {row.id for row in table.rows}, args)
    except Exception as exc:
        await finish_model_call(factory, call_id, "failed", f"{type(exc).__name__}: {exc}")
        raise
    await finish_model_call(factory, call_id, "completed")
    return conclusion
