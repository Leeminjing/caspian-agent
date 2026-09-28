"""本文件对外提供共享会话合流与执行分支初始化函数。

输入为会话身份、Run 身份、checkpoint 写入者及执行消息；输出为当前共享消息、待并入记录或投影游标。
工作流先投递持久化操作事件，再单写入共享 checkpoint；每个 Run 保留独立执行 checkpoint，并在安全边界补入其他分支的已完成记录。
示例：`missing = await missing_shared_messages(get_session, "u", "t", local_messages, writer)`。
"""

from __future__ import annotations

from collections.abc import Callable

from langchain_core.messages import BaseMessage, message_to_dict
from sqlalchemy.ext.asyncio import AsyncSession

from caspian.decision_governance.convergence_store import append_work_messages, project_thread, shared_messages
from caspian.decision_governance.events import deliver_pending
from caspian.decision_governance.shared_checkpoint import SharedCheckpointWriter
from caspian.decision_governance.operations import digest


def branch_config(thread_id: str, origin_run_id: str) -> dict:
    return {"configurable": {"thread_id": f"decision-run:{thread_id}:{origin_run_id}", "logical_thread_id": thread_id}}


def hash_messages(messages: list[BaseMessage]) -> dict[str, str]:
    return {message.id: digest(message_to_dict(message)) for message in messages if message.id}


async def synchronize(
    factory: Callable[[], AsyncSession], user_id: str, thread_id: str,
    writer: SharedCheckpointWriter,
) -> list[BaseMessage]:
    await deliver_pending(factory)

    async def write(messages: list[BaseMessage], revision: int, cursors: dict[str, int]) -> str:
        return await writer.write(user_id, thread_id, messages, revision, cursors)

    await project_thread(factory, user_id, thread_id, write)
    return await shared_messages(factory, user_id, thread_id)


async def record_work_progress(
    factory: Callable[[], AsyncSession], user_id: str, thread_id: str,
    run_id: str, messages: list[BaseMessage], writer: SharedCheckpointWriter,
) -> None:
    await append_work_messages(factory, user_id, thread_id, run_id, messages)
    await synchronize(factory, user_id, thread_id, writer)


async def missing_shared_messages(
    factory: Callable[[], AsyncSession], user_id: str, thread_id: str,
    local_messages: list[BaseMessage], writer: SharedCheckpointWriter,
    known_hashes: dict[str, str] | None = None,
    operation_only: bool = False,
) -> list[BaseMessage]:
    shared = await synchronize(factory, user_id, thread_id, writer)
    if operation_only:
        shared = [message for message in shared if (message.additional_kwargs or {}).get("decision_table_operation_event")]
    known = dict(known_hashes or {})
    known.update(hash_messages(local_messages))
    return [
        message for message in shared
        if not message.id or known.get(message.id) != digest(message_to_dict(message))
    ]
