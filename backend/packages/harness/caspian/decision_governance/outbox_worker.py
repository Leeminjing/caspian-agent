"""本文件对外提供治理 outbox 的进程生命周期投递任务。

输入为数据库会话工厂与轮询间隔；输出为重启后仍可交付的规范会话事件。
工作流启动时先补投递历史积压，再周期批量投递；关闭时取消任务并等待退出。
示例：`async with run_outbox(get_session): ...`。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

from caspian.decision_governance.events import deliver_pending
from caspian.decision_governance.convergence_store import project_all
from caspian.decision_governance.shared_checkpoint import SharedCheckpointWriter

logger = logging.getLogger(__name__)


@asynccontextmanager
async def run_outbox(
    factory: Callable[[], AsyncSession], interval_seconds: float = 2.0,
    checkpoint_writer: SharedCheckpointWriter | None = None,
) -> AsyncIterator[None]:
    await deliver_pending(factory)
    if checkpoint_writer is not None:
        await project_all(factory, lambda user_id, thread_id: (
            lambda messages, revision, cursors: checkpoint_writer.write(
                user_id, thread_id, messages, revision, cursors,
            )
        ))

    async def worker() -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                await deliver_pending(factory)
                if checkpoint_writer is not None:
                    await project_all(factory, lambda user_id, thread_id: (
                        lambda messages, revision, cursors: checkpoint_writer.write(
                            user_id, thread_id, messages, revision, cursors,
                        )
                    ))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("决策表 outbox 投递失败，下一轮重试")

    task = asyncio.create_task(worker(), name="decision-table-outbox")
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
