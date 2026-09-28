"""本文件对外提供 Run 消息登记、共享事件投影及共享消息读取。

输入为每个执行分支的完整消息快照和已交付会话事件；输出为去重共享历史、分支游标及可恢复投影。
工作流按 Run 保存消息指纹，按全局事件序号单写入投影；checkpoint 写入失败则回滚投影游标供重试。
示例：`await append_work_messages(factory, "u", "t", "run-a", messages)`。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import NAMESPACE_URL, uuid5

from langchain_core.messages import BaseMessage, SystemMessage, message_to_dict, messages_from_dict
from sqlalchemy import distinct, select
from sqlalchemy.ext.asyncio import AsyncSession

from caspian.decision_governance.convergence import merge_events
from caspian.decision_governance.models import TableHead, TableRunCursor, TableSessionEvent, TableSharedProjection
from caspian.decision_governance.operations import digest


async def append_work_messages(
    factory: Callable[[], AsyncSession], user_id: str, thread_id: str,
    run_id: str, messages: list[BaseMessage],
) -> int:
    async with factory() as session, session.begin():
        cursor = await session.scalar(select(TableRunCursor).where(
            TableRunCursor.run_id == run_id
        ).with_for_update())
        projection = await session.get(TableSharedProjection, {"user_id": user_id, "thread_id": thread_id})
        if cursor is None:
            known = {
                value.get("data", {}).get("id"): digest(value)
                for value in (projection.messages if projection else [])
                if value.get("data", {}).get("id")
            }
            cursor = TableRunCursor(
                run_id=run_id, user_id=user_id, thread_id=thread_id,
                last_sequence=0, message_fingerprints=known,
            )
            session.add(cursor)
            await session.flush()
        fingerprints = dict(cursor.message_fingerprints)
        for value in (projection.messages if projection else []):
            shared_id = value.get("data", {}).get("id")
            if shared_id and shared_id not in fingerprints:
                fingerprints[shared_id] = digest(value)
        appended = 0
        for index, message in enumerate(messages):
            if isinstance(message, SystemMessage):
                continue
            if (message.additional_kwargs or {}).get("decision_table_operation_event"):
                continue
            if not message.id:
                message = message.model_copy(update={"id": str(uuid5(NAMESPACE_URL, f"{run_id}:{index}"))})
            serialized = message_to_dict(message)
            fingerprint = digest(serialized)
            if fingerprints.get(message.id) == fingerprint:
                continue
            event_id = str(uuid5(NAMESPACE_URL, f"{run_id}:{message.id}:{fingerprint}"))
            existing = await session.scalar(select(TableSessionEvent.sequence).where(
                TableSessionEvent.event_id == event_id
            ))
            if existing is None:
                event = TableSessionEvent(
                    event_id=event_id, user_id=user_id, thread_id=thread_id,
                    run_id=run_id, operation_id=None,
                    parent_event_id=cursor.last_event_id,
                    tool_call_id=getattr(message, "tool_call_id", None),
                    event_type="work_message", payload={"message": serialized},
                )
                session.add(event)
                await session.flush()
                cursor.last_sequence = event.sequence
                appended += 1
            cursor.last_event_id = event_id
            fingerprints[message.id] = fingerprint
        cursor.message_fingerprints = fingerprints
        return appended


async def project_thread(
    factory: Callable[[], AsyncSession], user_id: str, thread_id: str,
    checkpoint_writer: Callable[[list[BaseMessage], int, dict[str, int]], Awaitable[str | None]] | None = None,
) -> TableSharedProjection:
    async with factory() as session, session.begin():
        values = {
            "user_id": user_id, "thread_id": thread_id,
            "last_sequence": 0, "table_revision": 0,
            "messages": [], "pending_messages": [],
        }
        dialect = session.bind.dialect.name if session.bind is not None else None
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
            await session.execute(insert(TableSharedProjection).values(**values).on_conflict_do_nothing())
        elif dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
            await session.execute(insert(TableSharedProjection).values(**values).on_conflict_do_nothing())
        projection = await session.scalar(select(TableSharedProjection).where(
            TableSharedProjection.user_id == user_id,
            TableSharedProjection.thread_id == thread_id,
        ).with_for_update())
        if projection is None:
            projection = TableSharedProjection(**values)
            session.add(projection)
            await session.flush()
        events = list((await session.scalars(select(TableSessionEvent).where(
            TableSessionEvent.user_id == user_id,
            TableSessionEvent.thread_id == thread_id,
            TableSessionEvent.sequence > projection.last_sequence,
        ).order_by(TableSessionEvent.sequence))).all())
        if not events:
            return projection
        values = [{
            "event_id": item.event_id,
            "event_type": item.event_type,
            "run_id": item.run_id,
            "operation_id": item.operation_id,
            "payload": item.payload,
        } for item in events]
        merged, pending = merge_events(projection.messages, projection.pending_messages, values)
        head = await session.get(TableHead, {"user_id": user_id, "thread_id": thread_id})
        if head is None:
            raise RuntimeError("会话事件存在但决策表头缺失")
        cursors = (await session.scalars(select(TableRunCursor).where(
            TableRunCursor.user_id == user_id,
            TableRunCursor.thread_id == thread_id,
        ))).all()
        cursor_values = {item.run_id: item.last_sequence for item in cursors}
        checkpoint_id = projection.checkpoint_id
        if checkpoint_writer is not None:
            checkpoint_id = await checkpoint_writer(messages_from_dict(merged), head.revision, cursor_values)
        projection.messages = merged
        projection.pending_messages = pending
        projection.last_sequence = events[-1].sequence
        projection.table_revision = head.revision
        projection.checkpoint_id = checkpoint_id
        return projection


async def project_all(
    factory: Callable[[], AsyncSession],
    writer_factory: Callable[[str, str], Callable[[list[BaseMessage], int, dict[str, int]], Awaitable[str | None]]] | None = None,
) -> int:
    async with factory() as session:
        keys = (await session.execute(select(distinct(TableSessionEvent.user_id), TableSessionEvent.thread_id))).all()
    count = 0
    for user_id, thread_id in keys:
        writer = writer_factory(user_id, thread_id) if writer_factory else None
        await project_thread(factory, user_id, thread_id, writer)
        count += 1
    return count


async def shared_messages(
    factory: Callable[[], AsyncSession], user_id: str, thread_id: str,
) -> list[BaseMessage]:
    async with factory() as session:
        projection = await session.get(TableSharedProjection, {"user_id": user_id, "thread_id": thread_id})
        return messages_from_dict(projection.messages) if projection else []
