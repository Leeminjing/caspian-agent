"""本文件对外提供 Run 审计的创建、状态写入、重启对账和按身份读取函数。

输入为已认证用户、逻辑会话和 Run 身份；输出为跨进程保留的 Run 状态及来源关系。
工作流在调度前持久登记 Run，每次状态变化写入同一记录；启动时把上次进程未完成的 Run 标为失败，历史操作可按 Run ID 反向读取。
示例：`await create_run_audit(factory, user_id="u", thread_id="t", run_id="r", origin_run_id="r", kind="decision_edit")`。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from caspian.decision_governance.models import TableRunAudit


async def create_run_audit(
    factory: Callable[[], AsyncSession], *, user_id: str, thread_id: str,
    run_id: str, origin_run_id: str, kind: str,
) -> TableRunAudit:
    async with factory() as session, session.begin():
        run = TableRunAudit(
            run_id=run_id, user_id=user_id, thread_id=thread_id,
            origin_run_id=origin_run_id, kind=kind, status="pending",
        )
        session.add(run)
        await session.flush()
        return run


async def update_run_audit(
    factory: Callable[[], AsyncSession], *, user_id: str, thread_id: str,
    run_id: str, status: str, error: str | None = None,
) -> TableRunAudit:
    async with factory() as session, session.begin():
        run = await session.get(TableRunAudit, run_id)
        if run is None or run.user_id != user_id or run.thread_id != thread_id:
            raise RuntimeError("Run 审计记录缺失或会话身份不匹配")
        run.status = status
        run.error = error
        run.updated_at = datetime.now(timezone.utc)
        await session.flush()
        return run


async def get_run_audit(
    factory: Callable[[], AsyncSession], *, user_id: str, thread_id: str, run_id: str,
) -> TableRunAudit | None:
    async with factory() as session:
        run = await session.get(TableRunAudit, run_id)
        if run is None or run.user_id != user_id or run.thread_id != thread_id:
            return None
        return run


async def reconcile_incomplete_run_audits(factory: Callable[[], AsyncSession]) -> int:
    async with factory() as session, session.begin():
        result = await session.execute(update(TableRunAudit).where(
            TableRunAudit.status.in_(("pending", "running")),
        ).values(
            status="error", error="进程重启时 Run 未完成；请核查对应操作结果后重试",
            updated_at=datetime.now(timezone.utc),
        ))
        return result.rowcount


def serialize_run_audit(run: TableRunAudit) -> dict:
    return {
        "run_id": run.run_id,
        "origin_run_id": run.origin_run_id,
        "user_id": run.user_id,
        "thread_id": run.thread_id,
        "kind": run.kind,
        "status": run.status,
        "error": run.error,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
    }
