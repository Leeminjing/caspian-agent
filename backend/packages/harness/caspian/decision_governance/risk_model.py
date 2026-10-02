"""本文件对外提供调用模型进行轻量动作风险自省的 assess_risk 函数。

输入为当前表、工具动作与参数、工作上下文和审计身份；输出为绑定动作的结构化风险结论。
工作流只询问补偿成本或决策固化，发送前注入有效表并记录版本；异常交由闸门升级完整复核。
示例：`risk = await assess_risk(messages, binding, args, table, context, factory, user_id="u", thread_id="t", run_id="r")`。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from langchain.agents.middleware import ModelRequest
from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy.ext.asyncio import AsyncSession

from caspian.decision_governance.action_risk import parse_risk
from caspian.decision_governance.domain import TableSnapshot
from caspian.decision_governance.model_calls import begin_model_call, finish_model_call, mark_model_call_sent
from caspian.decision_governance.review_protocol import ActionBinding
from caspian.decision_governance.view import TableView, bind_model_request
from caspian.models import create_chat_model

_PROMPT = (
    "你只做一次轻量动作风险判断，不做决策表冲突裁决。只输出 JSON："
    "action_name、args_hash、context_hash、examined_args（实际参数原样）、risk（low/high/uncertain）、reason。"
    "只回答：动作一旦执行错误，是否有较高补偿成本或会固化重要决策？"
    "读文件、测试、查询和易撤销局部编辑通常为 low；持久资源、迁移、部署、基础架构、核心依赖或重大技术选择为 high。"
    "依据实际参数与上下文，理由必须指出具体参数。无法可靠判断时为 uncertain。"
)


async def assess_risk(
    messages: list[Any], binding: ActionBinding, args: dict, table: TableSnapshot,
    context: dict, factory: Callable[[], AsyncSession], *, user_id: str, thread_id: str, run_id: str,
) -> dict:
    model = create_chat_model(name=context.get("model_name"), app_config=context.get("app_config"))
    transcript = [message.model_dump(mode="json") if hasattr(message, "model_dump") else str(message) for message in messages]
    payload = {"conversation": transcript, "pending_action": {
        "action_name": binding.action_name, "args": args, "args_hash": binding.args_hash,
        "context_hash": binding.context_hash,
    }}
    request = ModelRequest(model=model, messages=[HumanMessage(content=json.dumps(payload, ensure_ascii=False, default=str))], system_message=SystemMessage(content=_PROMPT))
    bound = bind_model_request(request, TableView.from_snapshot(table))
    call_id = await begin_model_call(factory, user_id=user_id, thread_id=thread_id, run_id=run_id, actor_id="action_risk", table=table, request=bound, reminder_reason="action_risk")
    try:
        await mark_model_call_sent(factory, call_id)
        config = getattr(context.get("app_config"), "decision_review", None)
        timeout = getattr(config, "risk_timeout_seconds", 15)
        response = await asyncio.wait_for(model.ainvoke([bound.system_message, *bound.messages]), timeout=timeout)
        result = parse_risk(response.text, binding, args)
    except Exception as exc:
        await finish_model_call(factory, call_id, "failed", f"{type(exc).__name__}: {exc}")
        raise
    await finish_model_call(factory, call_id, "completed")
    return result
