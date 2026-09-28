"""本文件对外提供权威表头锁定、快照查询及原子提交所需的仓储函数。

输入为 SQLAlchemy AsyncSession、会话身份和已验证候选；输出为持久快照、操作事件与 outbox。
工作流在同一事务中锁表头、检查基准、写新修订和成功事件；调用方负责事务提交。
示例：`head, table, policy = await locked_current(session, "u", "t")`。
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from caspian.decision_governance.domain import Row, TableSnapshot, diff_rows, snapshot
from caspian.decision_governance.legacy import load_legacy
from caspian.decision_governance.models import (
    TableHead, TableOperation, TableOperationEvent, TableOutbox, TablePermission, TableRevision,
)
from caspian.decision_governance.policy import default_policy
from caspian.decision_governance.operations import digest


async def _insert_genesis_if_missing(session: AsyncSession, user_id: str, thread_id: str) -> None:
    key = {"user_id": user_id, "thread_id": thread_id}
    if await session.get(TableHead, key) is not None:
        return
    legacy = load_legacy(user_id, thread_id)
    genesis = snapshot(0, legacy.rows if legacy else ())
    values = {
        "user_id": user_id,
        "thread_id": thread_id,
        "revision": 0,
        "content_hash": genesis.content_hash,
        "permission_revision": 0,
    }
    dialect = session.bind.dialect.name if session.bind is not None else None
    inserted = False
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
        result = await session.execute(insert(TableHead).values(**values).on_conflict_do_nothing())
        inserted = result.rowcount == 1
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
        result = await session.execute(insert(TableHead).values(**values).on_conflict_do_nothing())
        inserted = result.rowcount == 1
    else:
        session.add(TableHead(**values))
        await session.flush()
        inserted = True
    if inserted:
        import_operation = str(uuid4()) if legacy else None
        session.add(TableRevision(
            user_id=user_id, thread_id=thread_id, revision=0,
            content_hash=genesis.content_hash,
            rows=[row.to_dict() for row in genesis.rows], operation_id=import_operation,
        ))
        session.add(TablePermission(
            user_id=user_id, thread_id=thread_id, revision=0,
            policy=default_policy(user_id), actor_id=user_id,
        ))
        if legacy is not None:
            operation = TableOperation(
                operation_id=import_operation, user_id=user_id, thread_id=thread_id,
                run_id=f"migration:{thread_id}", actor_id=user_id, actor_kind="system",
                source="legacy_import", idempotency_key=f"legacy:{legacy.file_hash}",
                request_hash=digest({"legacy_file_hash": legacy.file_hash}),
                base_revision=0, permission_revision=0,
                candidate_hash=genesis.content_hash,
                candidate_rows=[row.to_dict() for row in genesis.rows],
                changes=[change.to_dict() for change in diff_rows((), genesis.rows)],
                reason="旧决策表只读导入", checks=[{
                    "path": legacy.path, "file_hash": legacy.file_hash,
                    "legacy_version": legacy.legacy_version, "format": legacy.format,
                }], status="committed", result_revision=0,
            )
            session.add(operation)
            await session.flush()
            await record_status(session, operation, "committed", {
                "revision": 0, "legacy_file_hash": legacy.file_hash,
            })
    await session.flush()


async def locked_current(
    session: AsyncSession, user_id: str, thread_id: str
) -> tuple[TableHead, TableSnapshot, dict]:
    await _insert_genesis_if_missing(session, user_id, thread_id)
    head = await session.scalar(
        select(TableHead).where(
            TableHead.user_id == user_id, TableHead.thread_id == thread_id
        ).with_for_update()
    )
    if head is None:
        raise RuntimeError("决策表头读取失败")
    revision = await session.get(TableRevision, {
        "user_id": user_id, "thread_id": thread_id, "revision": head.revision,
    })
    permission = await session.get(TablePermission, {
        "user_id": user_id, "thread_id": thread_id, "revision": head.permission_revision,
    })
    if revision is None or permission is None:
        raise RuntimeError("决策表快照或权限修订缺失")
    rows = tuple(Row.from_dict(value) for value in revision.rows)
    current = snapshot(head.revision, rows)
    if current.content_hash != head.content_hash or current.content_hash != revision.content_hash:
        raise RuntimeError("决策表摘要校验失败")
    return head, current, permission.policy


async def record_status(
    session: AsyncSession,
    operation: TableOperation,
    status: str,
    payload: dict,
) -> None:
    ordinal = await session.scalar(
        select(TableOperationEvent.ordinal)
        .where(TableOperationEvent.operation_id == operation.operation_id)
        .order_by(TableOperationEvent.ordinal.desc())
        .limit(1)
    )
    event = TableOperationEvent(
        event_id=str(uuid4()), operation_id=operation.operation_id,
        ordinal=(ordinal or 0) + 1, event_type=status, payload=payload,
    )
    outbox = TableOutbox(
        event_id=event.event_id, operation_id=operation.operation_id,
        user_id=operation.user_id, thread_id=operation.thread_id,
        run_id=operation.run_id, event_type=status,
        payload={"operation_id": operation.operation_id, "status": status, **payload},
    )
    operation.status = status
    session.add_all((event, outbox))
    await session.flush()


async def commit_candidate(
    session: AsyncSession, head: TableHead, operation: TableOperation,
    approval: dict | None = None,
) -> TableSnapshot:
    if head.revision != operation.base_revision:
        raise ValueError("决策表基准版本已变化")
    rows = tuple(Row.from_dict(value) for value in operation.candidate_rows)
    updated = snapshot(head.revision + 1, rows)
    if updated.content_hash != operation.candidate_hash:
        raise ValueError("候选摘要不匹配")
    session.add(TableRevision(
        user_id=head.user_id, thread_id=head.thread_id,
        revision=updated.revision, content_hash=updated.content_hash,
        rows=[row.to_dict() for row in rows], operation_id=operation.operation_id,
    ))
    head.revision = updated.revision
    head.content_hash = updated.content_hash
    operation.result_revision = updated.revision
    await record_status(session, operation, "committed", {
        "revision": updated.revision,
        "content_hash": updated.content_hash,
        "changes": operation.changes,
        "submission_mode": "approved" if approval else "direct_authorized",
        "approval": approval,
    })
    return updated
