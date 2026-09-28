"""本文件对外提供 DecisionTableService 的提案、审批、查询和权限配置接口。

输入为认证主体、Run、基准修订、完整候选及理由；输出为带状态的操作或权威快照。
工作流把幂等、差异、权限、检查和审批在表头锁保护下组合，并在同一数据库事务提交表与成功事件。
示例：`operation = await service.submit(user_id="u", thread_id="t", actor=Actor("u", "user"), run_id="r", source="ui", idempotency_key="k", base_revision=0, rows=(), reason="清空")`。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from caspian.decision_governance.checks import check_candidate
from caspian.decision_governance.domain import Row, TableSnapshot, diff_rows, snapshot
from caspian.decision_governance.models import TableApproval, TableHead, TableOperation, TableOperationEvent, TableOutbox, TablePermission, TableRevision
from caspian.decision_governance.operations import ApprovalBinding, digest, transition
from caspian.decision_governance.policy import Actor, authorize, validate_policy
from caspian.decision_governance.repository import commit_candidate, locked_current, record_status


class IdempotencyConflict(ValueError):
    pass


class DecisionTableService:
    def __init__(
        self, session_factory: Callable[[], AsyncSession],
        checker: Callable[[tuple[Row, ...], tuple[Row, ...]], Awaitable[tuple[dict, ...]]] = check_candidate,
    ):
        self._session_factory = session_factory
        self._checker = checker

    async def current(self, user_id: str, thread_id: str, actor: Actor) -> TableSnapshot:
        async with self._session_factory() as session, session.begin():
            _, current, policy = await locked_current(session, user_id, thread_id)
            if not authorize(policy, actor, "view", ())[0]:
                raise PermissionError("无权查看决策表")
            return current

    async def current_for_submission(
        self, user_id: str, thread_id: str
    ) -> TableSnapshot:
        async with self._session_factory() as session, session.begin():
            _, current, _ = await locked_current(session, user_id, thread_id)
            return current

    async def submit(
        self, *, user_id: str, thread_id: str, actor: Actor, run_id: str,
        source: str, idempotency_key: str, base_revision: int,
        rows: tuple[Row, ...], reason: str,
    ) -> TableOperation:
        if not run_id or not idempotency_key or not reason.strip():
            raise ValueError("改表请求必须包含 Run、幂等键和理由")
        candidate = snapshot(base_revision, rows)
        request_hash = digest({
            "user_id": user_id, "thread_id": thread_id, "actor": actor.subject,
            "run_id": run_id, "source": source, "base_revision": base_revision,
            "rows": [row.to_dict() for row in rows], "reason": reason,
        })
        check_failure = None
        try:
            async with self._session_factory() as check_session, check_session.begin():
                _, checked_current, _ = await locked_current(check_session, user_id, thread_id)
            checks = await self._checker(rows, checked_current.rows) if base_revision == checked_current.revision else ()
        except Exception as exc:
            checks = ()
            check_failure = f"候选检查失败: {type(exc).__name__}: {exc}"
        async with self._session_factory() as session, session.begin():
            head, current, policy = await locked_current(session, user_id, thread_id)
            existing = await session.scalar(select(TableOperation).where(
                TableOperation.user_id == user_id,
                TableOperation.thread_id == thread_id,
                TableOperation.idempotency_key == idempotency_key,
            ))
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise IdempotencyConflict("同一幂等键对应不同请求")
                return existing
            base = await session.get(TableRevision, {
                "user_id": user_id, "thread_id": thread_id, "revision": base_revision,
            })
            base_rows = tuple(Row.from_dict(row) for row in base.rows) if base else current.rows
            changes = diff_rows(base_rows, rows)
            operation = TableOperation(
                operation_id=str(uuid4()), user_id=user_id, thread_id=thread_id,
                run_id=run_id, actor_id=actor.id, actor_kind=actor.kind,
                source=source, idempotency_key=idempotency_key,
                request_hash=request_hash, base_revision=base_revision,
                permission_revision=head.permission_revision,
                candidate_hash=candidate.content_hash,
                candidate_rows=[row.to_dict() for row in rows],
                changes=[change.to_dict() for change in changes],
                reason=reason, checks=list(checks), status="processing",
            )
            session.add(operation)
            await session.flush()
            await record_status(session, operation, "processing", {
                "request": {
                    "actor_id": actor.id,
                    "actor_kind": actor.kind,
                    "source": source,
                    "base_revision": base_revision,
                    "candidate_hash": candidate.content_hash,
                    "candidate_rows": operation.candidate_rows,
                    "reason": reason,
                },
                "changes": operation.changes,
            })
            kinds = tuple(change.kind for change in changes)
            direct, _ = authorize(policy, actor, "direct_commit", kinds)
            propose, missing = authorize(policy, actor, "propose", kinds)
            if not direct and not propose:
                operation.error = "权限不足"
                transition(operation.status, "rejected")
                await record_status(session, operation, "rejected", {
                    "reason": "权限不足", "missing": list(missing),
                })
                return operation
            if check_failure is not None:
                operation.error = check_failure
                transition(operation.status, "failed")
                await record_status(session, operation, "failed", {"reason": check_failure})
                return operation
            if base_revision != current.revision:
                transition(operation.status, "version_conflict")
                await record_status(session, operation, "version_conflict", {
                    "current_revision": current.revision,
                    "intervening_changes": [change.to_dict() for change in diff_rows(base_rows, current.rows)],
                })
                return operation
            if not changes:
                transition(operation.status, "cancelled")
                await record_status(session, operation, "cancelled", {"reason": "候选与当前表相同"})
            elif direct and not any(check.get("requires_approval") for check in checks):
                transition(operation.status, "committed")
                await commit_candidate(session, head, operation)
            elif propose or direct:
                transition(operation.status, "awaiting_approval")
                await record_status(session, operation, "awaiting_approval", {
                    "checks": list(checks), "changes": operation.changes,
                })
            return operation

    async def decide(
        self, *, operation_id: str, actor: Actor, decision: str, reason: str,
    ) -> TableOperation:
        if decision not in {"approve", "reject", "cancel"} or not reason.strip():
            raise ValueError("审批结论或理由无效")
        async with self._session_factory() as session, session.begin():
            operation = await session.scalar(select(TableOperation).where(
                TableOperation.operation_id == operation_id
            ).with_for_update())
            if operation is None:
                raise LookupError("改表操作不存在")
            head, current, policy = await locked_current(session, operation.user_id, operation.thread_id)
            if operation.status != "awaiting_approval":
                raise ValueError("操作当前不等待审批")
            kinds = tuple(change["kind"] for change in operation.changes)
            if decision == "cancel" and actor.id == operation.actor_id and actor.kind == operation.actor_kind:
                allowed = True
            else:
                allowed, missing = authorize(policy, actor, "approve", kinds)
                if not allowed:
                    raise PermissionError(f"无权批准这些改表类型: {', '.join(missing)}")
            binding = ApprovalBinding(
                operation_id=operation.operation_id, candidate_hash=operation.candidate_hash,
                base_revision=operation.base_revision, permission_revision=operation.permission_revision,
                approver_id=actor.id, allowed_ops=kinds,
            )
            session.add(TableApproval(
                approval_id=str(uuid4()), operation_id=operation.operation_id,
                approver_id=actor.id, decision=decision,
                candidate_hash=binding.candidate_hash, base_revision=binding.base_revision,
                permission_revision=binding.permission_revision,
                allowed_ops=list(binding.allowed_ops), reason=reason,
            ))
            if decision != "approve":
                target = "rejected" if decision == "reject" else "cancelled"
                transition(operation.status, target)
                await record_status(session, operation, target, {"approver_id": actor.id, "reason": reason})
            elif not binding.matches(
                operation_id=operation.operation_id,
                candidate_hash=operation.candidate_hash,
                base_revision=current.revision,
                permission_revision=head.permission_revision,
                allowed_ops=kinds,
            ):
                base = await session.get(TableRevision, {
                    "user_id": operation.user_id, "thread_id": operation.thread_id,
                    "revision": operation.base_revision,
                })
                if base is None:
                    raise RuntimeError("审批基准表修订缺失")
                base_rows = tuple(Row.from_dict(row) for row in base.rows)
                transition(operation.status, "version_conflict")
                await record_status(session, operation, "version_conflict", {
                    "reason": "基准或权限已变化，需重新检查和确认",
                    "current_revision": current.revision,
                    "permission_revision": head.permission_revision,
                    "intervening_changes": [change.to_dict() for change in diff_rows(base_rows, current.rows)],
                })
            else:
                transition(operation.status, "committed")
                await commit_candidate(session, head, operation, {
                    "approver_id": actor.id,
                    "decision": decision,
                    "reason": reason,
                    "candidate_hash": binding.candidate_hash,
                    "base_revision": binding.base_revision,
                    "permission_revision": binding.permission_revision,
                })
            return operation

    async def history(self, user_id: str, thread_id: str) -> list[TableOperation]:
        async with self._session_factory() as session:
            first_event = select(func.min(TableOutbox.sequence)).where(
                TableOutbox.operation_id == TableOperation.operation_id,
            ).correlate(TableOperation).scalar_subquery()
            return list((await session.scalars(
                select(TableOperation).where(
                    TableOperation.user_id == user_id, TableOperation.thread_id == thread_id
                ).order_by(first_event)
            )).all())

    async def find_by_key(
        self, user_id: str, thread_id: str, idempotency_key: str
    ) -> TableOperation | None:
        async with self._session_factory() as session:
            return await session.scalar(select(TableOperation).where(
                TableOperation.user_id == user_id,
                TableOperation.thread_id == thread_id,
                TableOperation.idempotency_key == idempotency_key,
            ))

    async def revision(
        self, user_id: str, thread_id: str, revision: int
    ) -> TableSnapshot:
        async with self._session_factory() as session:
            row = await session.get(TableRevision, {
                "user_id": user_id, "thread_id": thread_id, "revision": revision,
            })
            if row is None:
                raise LookupError("决策表修订不存在")
            table = snapshot(revision, tuple(Row.from_dict(value) for value in row.rows))
            if table.content_hash != row.content_hash:
                raise RuntimeError("历史决策表摘要校验失败")
            return table

    async def operation_events(self, operation_id: str) -> list[TableOperationEvent]:
        async with self._session_factory() as session:
            return list((await session.scalars(select(TableOperationEvent).where(
                TableOperationEvent.operation_id == operation_id
            ).order_by(TableOperationEvent.ordinal))).all())

    async def operation_detail(
        self, user_id: str, thread_id: str, operation_id: str
    ) -> dict[str, Any]:
        async with self._session_factory() as session:
            operation = await session.get(TableOperation, operation_id)
            if operation is None or operation.user_id != user_id or operation.thread_id != thread_id:
                raise LookupError("改表操作不存在")
            approvals = list((await session.scalars(select(TableApproval).where(
                TableApproval.operation_id == operation_id
            ).order_by(TableApproval.created_at, TableApproval.approval_id))).all())
            events = list((await session.scalars(select(TableOperationEvent).where(
                TableOperationEvent.operation_id == operation_id
            ).order_by(TableOperationEvent.ordinal))).all())
            before = await session.get(TableRevision, {
                "user_id": user_id, "thread_id": thread_id,
                "revision": operation.base_revision,
            })
            after = await session.get(TableRevision, {
                "user_id": user_id, "thread_id": thread_id,
                "revision": operation.result_revision,
            }) if operation.result_revision is not None else None
            return {
                "operation": operation,
                "approvals": approvals,
                "events": events,
                "before": before,
                "after": after,
            }

    async def set_policy(
        self, *, user_id: str, thread_id: str, actor: Actor, policy: dict[str, Any],
    ) -> int:
        validate_policy(policy)
        if actor.kind != "user" or actor.id != user_id:
            raise PermissionError("只有会话所有者可配置改表权限")
        async with self._session_factory() as session, session.begin():
            head, _, _ = await locked_current(session, user_id, thread_id)
            head.permission_revision += 1
            session.add(TablePermission(
                user_id=user_id, thread_id=thread_id, revision=head.permission_revision,
                policy=policy, actor_id=actor.id,
            ))
            pending = (await session.scalars(select(TableOperation).where(
                TableOperation.user_id == user_id,
                TableOperation.thread_id == thread_id,
                TableOperation.status == "awaiting_approval",
            ).with_for_update())).all()
            for operation in pending:
                transition(operation.status, "version_conflict")
                await record_status(session, operation, "version_conflict", {
                    "reason": "权限修订已变化，需重新检查和确认",
                    "permission_revision": head.permission_revision,
                })
            return head.permission_revision

    async def policy(self, user_id: str, thread_id: str) -> dict[str, Any]:
        async with self._session_factory() as session, session.begin():
            head, _, policy = await locked_current(session, user_id, thread_id)
            return {"revision": head.permission_revision, "policy": policy}
