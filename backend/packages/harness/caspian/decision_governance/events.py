"""本文件对外提供 outbox 投递和会话事件查询函数。

输入为异步会话工厂及会话游标；输出为按序且去重的审计事件。
工作流锁定未投递记录，在同一事务追加共享事件并标记投递；重启后可安全重试。
示例：`count = await deliver_pending(session_factory)`。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from caspian.decision_governance.models import TableOutbox, TableSessionEvent


async def deliver_pending(session_factory: Callable[[], AsyncSession], limit: int = 100) -> int:
    async with session_factory() as session, session.begin():
        pending = list((await session.scalars(
            select(TableOutbox)
            .where(TableOutbox.delivered_at.is_(None))
            .order_by(TableOutbox.sequence)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )).all())
        for item in pending:
            existing = await session.scalar(select(TableSessionEvent.sequence).where(
                TableSessionEvent.event_id == item.event_id
            ))
            if existing is None:
                session.add(TableSessionEvent(
                    event_id=item.event_id, user_id=item.user_id,
                    thread_id=item.thread_id, run_id=item.run_id,
                    operation_id=item.operation_id,
                    event_type=item.event_type, payload=item.payload,
                ))
            item.delivered_at = datetime.now(timezone.utc)
        return len(pending)


async def read_events(
    session_factory: Callable[[], AsyncSession], user_id: str, thread_id: str,
    after_sequence: int = 0,
) -> list[TableSessionEvent]:
    async with session_factory() as session:
        return list((await session.scalars(
            select(TableSessionEvent).where(
                TableSessionEvent.user_id == user_id,
                TableSessionEvent.thread_id == thread_id,
                TableSessionEvent.sequence > after_sequence,
            ).order_by(TableSessionEvent.sequence)
        )).all())
