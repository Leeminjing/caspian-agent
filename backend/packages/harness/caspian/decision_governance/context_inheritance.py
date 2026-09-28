"""本文件对外提供派生 Context 的有效决策表继承函数。

输入为同一用户的来源线程和新 Context 身份；输出为目标线程中有来源记录的生效表修订。
工作流在创建目标 Context 的数据库事务中锁定来源表与权限、拒绝不一致来源，并以可审计操作复制已生效快照和权限。
示例：`await inherit_context_table(session, "u", "child", ["parent"])`。
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from caspian.decision_governance.domain import diff_rows
from caspian.decision_governance.models import TableOperation, TablePermission, TableRunAudit
from caspian.decision_governance.operations import digest
from caspian.decision_governance.policy import Actor, authorize
from caspian.decision_governance.repository import commit_candidate, locked_current, record_status


async def inherit_context_table(
    session: AsyncSession, user_id: str, target_thread_id: str,
    source_thread_ids: list[str],
) -> int:
    if not source_thread_ids:
        return 0
    sources = {}
    policies = {}
    permission_revisions = {}
    for source_thread_id in sorted(set(source_thread_ids)):
        source_head, table, policy = await locked_current(session, user_id, source_thread_id)
        if not authorize(policy, Actor(user_id, "user"), "view", ())[0]:
            raise PermissionError(f"无权查看来源 Context {source_thread_id} 的决策表")
        sources[source_thread_id] = table
        policies[source_thread_id] = policy
        permission_revisions[source_thread_id] = source_head.permission_revision
    hashes = {table.content_hash for table in sources.values()}
    if len(hashes) != 1:
        details = ", ".join(
            f"{thread}:修订{table.revision}" for thread, table in sources.items()
        )
        raise ValueError(f"来源 Context 的有效决策表不同，无法自动派生：{details}")
    if len({digest(policy) for policy in policies.values()}) != 1:
        raise ValueError("来源 Context 的改表权限不同，无法自动派生")
    source = sources[source_thread_ids[0]]
    head, current, _ = await locked_current(session, user_id, target_thread_id)
    if head.revision != 0 or current.rows:
        raise ValueError("目标 Context 已有决策表，不能重复继承")
    target_permission = await session.get(TablePermission, {
        "user_id": user_id, "thread_id": target_thread_id, "revision": 0,
    })
    if target_permission is None:
        raise RuntimeError("目标 Context 权限初始修订缺失")
    target_permission.policy = policies[source_thread_ids[0]]
    if not source.rows:
        return 0
    run_id = f"context-derive:{target_thread_id}"
    operation_id = str(uuid4())
    candidate_rows = [row.to_dict() for row in source.rows]
    changes = [change.to_dict() for change in diff_rows((), source.rows)]
    reason = f"继承来源 Context {source_thread_ids[0]} 的有效决策表修订 {source.revision}"
    operation = TableOperation(
        operation_id=operation_id, user_id=user_id, thread_id=target_thread_id,
        run_id=run_id, actor_id=user_id, actor_kind="system",
        source="context_inheritance", idempotency_key=run_id,
        request_hash=digest({"sources": source_thread_ids, "source_versions": {
            thread: table.version for thread, table in sources.items()
        }, "source_permission_revisions": permission_revisions}),
        base_revision=0, permission_revision=head.permission_revision,
        candidate_hash=source.content_hash, candidate_rows=candidate_rows,
        changes=changes, reason=reason, checks=[], status="processing",
    )
    session.add(operation)
    session.add(TableRunAudit(
        run_id=run_id, user_id=user_id, thread_id=target_thread_id,
        origin_run_id=run_id, kind="context_derivation", status="success",
    ))
    await session.flush()
    await record_status(session, operation, "processing", {
        "source_threads": source_thread_ids,
        "source_versions": {thread: table.version for thread, table in sources.items()},
        "source_permission_revisions": permission_revisions,
        "candidate_rows": candidate_rows,
        "changes": changes,
        "reason": reason,
    })
    await commit_candidate(session, head, operation)
    return 1
