"""本文件对外提供 DecisionTableEditMiddleware，处理界面发起的改表 Run。

输入为带 decision_table_edit 的用户消息、认证上下文和 Run 配置；输出为包含操作状态的消息增量。
工作流保留原请求消息，解析基准、候选及理由，交给统一服务提交，并在本 Run 记录差异和结果。
示例：`create_agent(middleware=[DecisionTableEditMiddleware()])`。
"""

from __future__ import annotations

import asyncio
from uuid import NAMESPACE_URL, uuid5

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import AgentState
from langchain.messages import HumanMessage
from langgraph.prebuilt import ToolRuntime
from langgraph.types import interrupt
from langgraph.errors import GraphInterrupt

from caspian.decision_governance.adapter import detailed_result_text, service
from caspian.decision_governance.domain import Row
from caspian.decision_governance.identity import logical_thread_id
from caspian.decision_governance.policy import Actor


def _intent(state: AgentState) -> tuple[HumanMessage | None, dict | None]:
    messages = state.get("messages", [])
    acknowledged = {message.id for message in messages if isinstance(message, HumanMessage) and (message.additional_kwargs or {}).get("decision_table_edit_ack")}
    for message in reversed(messages):
        if not isinstance(message, HumanMessage):
            continue
        payload = (message.additional_kwargs or {}).get("decision_table_edit")
        if isinstance(payload, dict) and f"{message.id}-decision-result" not in acknowledged:
            return message, payload
    return None, None


def _rows(payload: dict, request_id: str) -> tuple[Row, ...]:
    raw = payload.get("rows")
    if not isinstance(raw, list):
        raise ValueError("编辑载荷必须包含 rows 列表")
    rows = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError("决策行必须是对象")
        value = dict(item)
        value["id"] = value.get("id") or str(uuid5(NAMESPACE_URL, f"{request_id}:{index}"))
        rows.append(Row.from_dict(value))
    return tuple(rows)


def _reply(trigger: HumanMessage, content: str, detail: dict | None = None) -> dict:
    return {
        "jump_to": "end",
        "messages": [HumanMessage(
            id=f"{trigger.id}-decision-result",
            content=content,
            additional_kwargs={"decision_table_edit_ack": detail or {"status": "failed"}},
        )],
    }


class DecisionTableEditMiddleware(AgentMiddleware):
    async def _run(self, state: AgentState, runtime: ToolRuntime) -> dict | None:
        trigger, payload = _intent(state)
        if trigger is None or payload is None:
            return None
        thread_id = logical_thread_id(runtime)
        context = runtime.context if isinstance(runtime.context, dict) else {}
        user_id = context.get("user_id")
        run_id = context.get("run_id")
        if not all((thread_id, user_id, run_id, trigger.id)):
            return _reply(trigger, "改表失败：缺少已验证的会话、Run 或请求 ID")
        try:
            base_revision = payload.get("base_revision")
            if isinstance(base_revision, bool) or not isinstance(base_revision, int):
                raise ValueError("编辑基准修订缺失，请刷新决策表")
            reason = payload.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("请填写修改理由")
            candidate = _rows(payload, trigger.id)
            table_service = service()
            previous = await table_service.find_by_key(str(user_id), str(thread_id), f"ui:{trigger.id}")
            operation = await table_service.submit(
                user_id=str(user_id), thread_id=str(thread_id),
                actor=Actor(str(user_id), "user"), run_id=previous.run_id if previous else str(run_id),
                source="ui_edit", idempotency_key=f"ui:{trigger.id}",
                base_revision=base_revision, rows=candidate, reason=reason,
            )
            if operation.status == "awaiting_approval":
                current = await table_service.revision(str(user_id), str(thread_id), operation.base_revision)
                choice = interrupt({
                    "type": "decision_table_adjudication",
                    "operation_id": operation.operation_id,
                    "candidate": operation.candidate_rows,
                    "existing": [row.to_dict() for row in current.rows],
                    "changes": operation.changes,
                    "conflicts": [reason for check in operation.checks for reason in check.get("reasons", [])],
                    "checks": operation.checks,
                    "reason": operation.reason,
                    "allowed_decisions": ["keep", "adopt"],
                })
                if not isinstance(choice, dict) or choice.get("decision") not in {"keep", "adopt"}:
                    raise ValueError("审批结论无效")
                operation = await table_service.decide(
                    operation_id=operation.operation_id,
                    actor=Actor(str(user_id), "user"),
                    decision="approve" if choice["decision"] == "adopt" else "reject",
                    reason="用户采纳候选表" if choice["decision"] == "adopt" else "用户保留旧表",
                )
            return _reply(trigger, await detailed_result_text(table_service, operation), {
                "operation_id": operation.operation_id, "status": operation.status,
                "base_revision": operation.base_revision,
                "result_revision": operation.result_revision,
                "changes": operation.changes, "checks": operation.checks,
            })
        except GraphInterrupt:
            raise
        except Exception as exc:
            return _reply(trigger, f"改表失败：{type(exc).__name__}: {exc}")

    def before_agent(self, state: AgentState, runtime: ToolRuntime) -> dict | None:
        return asyncio.run(self._run(state, runtime))

    async def abefore_agent(self, state: AgentState, runtime: ToolRuntime) -> dict | None:
        return await self._run(state, runtime)


DecisionTableEditMiddleware.before_agent.__can_jump_to__ = ("end",)
DecisionTableEditMiddleware.abefore_agent.__can_jump_to__ = ("end",)
