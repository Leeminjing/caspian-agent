"""本文件对外提供模型调用使用的决策表修订记录与查询。

输入为 Run、模型请求、调用主体和权威快照；输出为持久 call_id、修订、调用终态及可按主体统计的轮次。
工作流在发送模型请求前写入已验证版本，模型返回或失败后更新同一条记录；内部风险与复核调用不增加主 Agent 周期计数。
示例：`call_id = await begin_model_call(factory, ...)`。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from uuid import uuid4

from langchain.agents.middleware import ModelRequest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from caspian.decision_governance.domain import TableSnapshot
from caspian.decision_governance.models import TableModelCall
from caspian.decision_governance.operations import digest


async def begin_model_call(
    factory: Callable[[], AsyncSession], *, user_id: str, thread_id: str,
    run_id: str, actor_id: str, table: TableSnapshot, request: ModelRequest,
    reminder_reason: str | None = None,
) -> str:
    call_id = str(uuid4())
    request_hash = digest({
        "system": request.system_message.model_dump(mode="json") if request.system_message else None,
        "messages": [message.model_dump(mode="json") for message in request.messages],
    })
    async with factory() as session, session.begin():
        session.add(TableModelCall(
            call_id=call_id, user_id=user_id, thread_id=thread_id,
            run_id=run_id, actor_id=actor_id, table_revision=table.revision,
            content_hash=table.content_hash, request_hash=request_hash,
            status="prepared", reminder_reason=reminder_reason,
        ))
    return call_id


async def count_model_calls(
    factory: Callable[[], AsyncSession], user_id: str, thread_id: str, run_id: str,
    actor_id: str | None = None,
) -> int:
    async with factory() as session:
        query = select(func.count()).select_from(TableModelCall).where(
            TableModelCall.user_id == user_id,
            TableModelCall.thread_id == thread_id,
            TableModelCall.run_id == run_id,
            TableModelCall.status != "prepared",
        )
        if actor_id is not None:
            query = query.where(TableModelCall.actor_id == actor_id)
        return int(await session.scalar(query) or 0)


async def mark_model_call_sent(factory: Callable[[], AsyncSession], call_id: str) -> None:
    async with factory() as session, session.begin():
        call = await session.scalar(select(TableModelCall).where(TableModelCall.call_id == call_id))
        if call is None or call.status != "prepared":
            raise RuntimeError("模型调用审计准备记录缺失或状态无效")
        call.status = "sent"


async def finish_model_call(
    factory: Callable[[], AsyncSession], call_id: str, status: str, error: str | None = None,
) -> None:
    async with factory() as session, session.begin():
        call = await session.scalar(select(TableModelCall).where(TableModelCall.call_id == call_id))
        if call is None:
            raise RuntimeError("模型调用审计记录缺失")
        call.status = status
        call.error = error
        call.completed_at = datetime.now(timezone.utc)


async def read_model_calls(
    factory: Callable[[], AsyncSession], user_id: str, thread_id: str,
) -> list[TableModelCall]:
    async with factory() as session:
        return list((await session.scalars(select(TableModelCall).where(
            TableModelCall.user_id == user_id,
            TableModelCall.thread_id == thread_id,
        ).order_by(TableModelCall.sequence))).all())
