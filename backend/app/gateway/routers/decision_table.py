"""本文件对外提供决策表当前快照、操作历史和权限策略的 HTTP 路由。

输入为认证用户、线程 ID、操作 ID 或权限配置；输出为权威修订、差异、审批和明确读取错误。
工作流先核对线程所有权，再经治理服务读取或修改；空表与故障以不同状态返回。
示例：`GET /api/threads/{thread_id}/decision-table/operations`。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from backend.app.gateway.context.models import WebThread
from caspian.decision_governance.adapter import service
from caspian.decision_governance.model_calls import read_model_calls
from caspian.decision_governance.policy import Actor
from caspian.decision_governance.review_store import read_reviews
from caspian.decision_governance.run_audit import get_run_audit, serialize_run_audit
from caspian.persistence.engine import get_session

router = APIRouter()


async def _owner(thread_id: str, request: Request) -> str:
    user_id = str(request.state.current_user.id)
    async with get_session() as session:
        thread = await session.scalar(select(WebThread).where(WebThread.thread_id == thread_id))
    if thread is not None and thread.user_id != user_id:
        raise HTTPException(status_code=403, detail="无权访问该会话的决策等级表")
    return user_id


def _operation(value) -> dict:
    return {
        "operation_id": value.operation_id,
        "run_id": value.run_id,
        "actor": {"id": value.actor_id, "kind": value.actor_kind},
        "source": value.source,
        "base_revision": value.base_revision,
        "permission_revision": value.permission_revision,
        "candidate_hash": value.candidate_hash,
        "candidate_rows": value.candidate_rows,
        "changes": value.changes,
        "reason": value.reason,
        "checks": value.checks,
        "status": value.status,
        "result_revision": value.result_revision,
        "error": value.error,
        "created_at": value.created_at.isoformat() if value.created_at else None,
    }


@router.get("/{thread_id}/decision-table")
async def get_decision_table(thread_id: str, request: Request) -> dict:
    user_id = await _owner(thread_id, request)
    try:
        table = await service().current(user_id, thread_id, Actor(user_id, "user"))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"决策表读取或校验失败: {exc}") from exc
    return {
        "read_status": "verified_empty" if not table.rows else "verified_current",
        "exists": bool(table.rows),
        "revision": table.revision,
        "version": table.version,
        "content_hash": table.content_hash,
        "rows": [row.to_dict() for row in table.rows],
    }


@router.get("/{thread_id}/decision-table/operations")
async def get_decision_table_operations(thread_id: str, request: Request) -> dict:
    user_id = await _owner(thread_id, request)
    try:
        await service().current(user_id, thread_id, Actor(user_id, "user"))
        operations = await service().history(user_id, thread_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"operations": [_operation(item) for item in operations]}


@router.get("/{thread_id}/decision-table/operations/{operation_id}")
async def get_decision_table_operation(thread_id: str, operation_id: str, request: Request) -> dict:
    user_id = await _owner(thread_id, request)
    try:
        await service().current(user_id, thread_id, Actor(user_id, "user"))
        detail = await service().operation_detail(user_id, thread_id, operation_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    run = await get_run_audit(
        get_session, user_id=user_id, thread_id=thread_id,
        run_id=detail["operation"].run_id,
    )
    return {
        "operation": _operation(detail["operation"]),
        "run": serialize_run_audit(run) if run is not None else None,
        "approvals": [{
            "approval_id": item.approval_id, "approver_id": item.approver_id,
            "decision": item.decision, "candidate_hash": item.candidate_hash,
            "base_revision": item.base_revision,
            "permission_revision": item.permission_revision,
            "allowed_ops": item.allowed_ops, "reason": item.reason,
        } for item in detail["approvals"]],
        "events": [{
            "event_id": item.event_id, "ordinal": item.ordinal,
            "event_type": item.event_type, "payload": item.payload,
        } for item in detail["events"]],
        "before": detail["before"].rows if detail["before"] else None,
        "after": detail["after"].rows if detail["after"] else None,
    }


@router.get("/{thread_id}/decision-table/permissions")
async def get_decision_table_permissions(thread_id: str, request: Request) -> dict:
    user_id = await _owner(thread_id, request)
    return await service().policy(user_id, thread_id)


@router.get("/{thread_id}/decision-table/model-calls")
async def get_decision_table_model_calls(thread_id: str, request: Request) -> dict:
    user_id = await _owner(thread_id, request)
    await service().current(user_id, thread_id, Actor(user_id, "user"))
    calls = await read_model_calls(get_session, user_id, thread_id)
    return {"calls": [{
        "call_id": call.call_id, "run_id": call.run_id,
        "actor_id": call.actor_id, "table_revision": call.table_revision,
        "content_hash": call.content_hash, "request_hash": call.request_hash,
        "reminder_reason": call.reminder_reason,
        "status": call.status, "error": call.error,
        "checked_at": call.checked_at.isoformat() if call.checked_at else None,
    } for call in calls]}


@router.get("/{thread_id}/decision-table/action-reviews")
async def get_decision_table_action_reviews(thread_id: str, request: Request) -> dict:
    user_id = await _owner(thread_id, request)
    await service().current(user_id, thread_id, Actor(user_id, "user"))
    reviews = await read_reviews(get_session, user_id, thread_id)
    return {"reviews": [{
        "review_id": review.review_id, "run_id": review.run_id,
        "tool_call_id": review.tool_call_id,
        "action_name": review.action_name, "args_hash": review.args_hash,
        "action_args": review.action_args,
        "actor_id": review.actor_id,
        "table_revision": review.table_revision,
        "content_hash": review.content_hash,
        "trigger_reason": review.trigger_reason,
        "status": review.status, "conclusion": review.conclusion,
        "result": review.result, "error": review.error,
        "created_at": review.created_at.isoformat() if review.created_at else None,
    } for review in reviews]}


@router.put("/{thread_id}/decision-table/permissions")
async def put_decision_table_permissions(thread_id: str, request: Request) -> dict:
    user_id = await _owner(thread_id, request)
    body = await request.json()
    policy = body.get("policy") if isinstance(body, dict) else None
    try:
        revision = await service().set_policy(
            user_id=user_id, thread_id=thread_id,
            actor=Actor(user_id, "user"), policy=policy,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"revision": revision}
