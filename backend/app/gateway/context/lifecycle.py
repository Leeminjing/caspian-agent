"""
本文件对外提供 ThreadLifecycleService 类，作为会话（thread/context）生命周期的唯一服务：
级联删除、级联归档、级联恢复与归档列表查询。

对外提供:
    ThreadLifecycleService(checkpointer, store, session_factory) — 会话生命周期服务

输入:
    checkpointer: BaseCheckpointSaver — LangGraph 检查点持久化器（app.state.checkpointer）
    store: BaseStore | None — LangGraph Store 长期记忆（app.state.store）
    session_factory: Callable — 异步 session 工厂，None 时取全局默认

输出:
    delete(user_id, thread_id) → {"deleted": [...]}
    archive(user_id, thread_id) → {"archived": [...]}
    restore(user_id, thread_id) → {"restored": [...]}
    list_archived(user_id) → [{"thread_id", "title", "archived_at"}, ...]

具体工作流:
    (1) _subtree(user_id, thread_id): 校验根会话归属（否则 404），并沿 web_context_sources
        的 parent→child 来源边做全后裔闭包 BFS。
    (2) delete: 对子树内每个会话逐个清理 checkpointer（含承诺层子图隔离线程）、goal store
        与文件系统（best-effort），再在单个事务中删除 web_context_sources（先移除子树作为
        父/子的全部来源行，规避 ON DELETE RESTRICT）与 web_threads 行（FK 级联清理 definitions）。
    (3) archive / restore: 对子树内行更新 archived_at（置时间/清空），数据保留。
    (4) list_archived: 按 archived_at 倒序返回当前用户全部已归档会话。

示例:
    service = ThreadLifecycleService(checkpointer, store)
    await service.delete("uuid-xxx", "thread-abc")
"""

from __future__ import annotations

import logging
import shutil
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException
from sqlalchemy import delete as sa_delete
from sqlalchemy import or_, select, update

from backend.app.gateway.context.models import WebContextSource, WebThread
from caspian.persistence.engine import get_session as _default_session_factory

logger = logging.getLogger(__name__)

_GOAL_NAMESPACE_PREFIX = "goal"
_GOAL_KEY = "goal"
_COMMITMENT_SUFFIX = ":commitment"


def _order(child_edges: dict[str, list[str]], root_set: set[str]) -> list[str]:
    """把闭包集合排成「子先于父」的删除顺序（受保护纯函数）。

    输入:
        child_edges: dict — parent → [children] 邻接表
        root_set: set — 待删线程集合（闭包）

    输出:
        list[str] — 删除顺序，保证任意子会话先于其父被处理

    工作流:
        (1) 反复选取集合内「不再作为任何剩余线程的父」的叶子节点先出队
        (2) 剩余为空即完成；若出现环（理论上不应发生），兜底一次性取出
    """
    remaining = set(root_set)
    order: list[str] = []
    while remaining:
        leaves = [
            node
            for node in remaining
            if not (set(child_edges.get(node, [])) & remaining)
        ]
        if not leaves:
            leaves = list(remaining)
        for node in leaves:
            remaining.discard(node)
            order.append(node)
    return order


class ThreadLifecycleService:
    """会话生命周期服务：级联删除/归档/恢复与归档列表。"""

    def __init__(
        self,
        checkpointer: Any,
        store: Any | None = None,
        session_factory: Callable | None = None,
    ) -> None:
        self.checkpointer = checkpointer
        self.store = store
        self.session_factory = session_factory or _default_session_factory

    async def _load_adjacency(self, user_id: str) -> dict[str, list[str]]:
        """加载当前用户全部派生来源，构建 parent → [children] 邻接表（受保护 helper）。

        输入:
            user_id: str — 认证用户标识

        输出:
            dict[str, list[str]] — parent_context_id → [context_id, ...]
        """
        async with self.session_factory() as session:
            user_thread_ids = list(
                (
                    await session.scalars(
                        select(WebThread.thread_id).where(WebThread.user_id == user_id)
                    )
                ).all()
            )
            if not user_thread_ids:
                return {}
            source_rows = (
                await session.scalars(
                    select(WebContextSource).where(
                        WebContextSource.context_id.in_(user_thread_ids)
                    )
                )
            ).all()
        adjacency: dict[str, list[str]] = defaultdict(list)
        for source in source_rows:
            adjacency[source.parent_context_id].append(source.context_id)
        return dict(adjacency)

    async def _subtree(self, user_id: str, thread_id: str) -> list[str]:
        """校验归属并返回会话的整棵派生子树闭包（受保护 helper）。

        输入:
            user_id: str — 认证用户标识
            thread_id: str — 目标会话 thread_id

        输出:
            list[str] — 包含根会话与其全部派生后裔的 thread_id 列表

        工作流:
            (1) 根会话不存在或不属于该用户 → HTTP 404
            (2) 加载用户全部来源构建邻接表
            (3) 从根会话 BFS 沿 parent→child 边收集可达集合
        """
        async with self.session_factory() as session:
            root = await session.get(WebThread, thread_id)
            if root is None or root.user_id != user_id:
                raise HTTPException(404, "会话不存在或不属于当前用户")

        adjacency = await self._load_adjacency(user_id)
        seen: set[str] = set()
        stack = [thread_id]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            for child in adjacency.get(current, []):
                if child not in seen:
                    stack.append(child)
        return list(seen)

    async def _purge_thread(self, user_id: str, thread_id: str) -> None:
        """清理单个会话的 checkpointer / goal / 文件系统（本方法为 best-effort）。

        输入:
            user_id / thread_id — 用户与线程标识

        输出:
            None — 各步失败仅记日志，不中断整体删除
        """
        # checkpointer：主线程 + 承诺层子图隔离线程
        for tid in (thread_id, f"{thread_id}{_COMMITMENT_SUFFIX}"):
            deleter = getattr(self.checkpointer, "adelete_thread", None)
            if deleter is None:
                continue
            try:
                await deleter(tid)
            except Exception:
                logger.warning(
                    "删除会话: checkpointer 清理失败 tid=%s", tid, exc_info=True
                )
        # store：goal 条目（知识按用户共享，不删）
        if self.store is not None:
            deleter = getattr(self.store, "adelete", None)
            if deleter is not None:
                try:
                    await deleter(
                        (_GOAL_NAMESPACE_PREFIX, str(user_id), str(thread_id)),
                        _GOAL_KEY,
                    )
                except Exception:
                    logger.warning(
                        "删除会话: store goal 清理失败 tid=%s", thread_id, exc_info=True
                    )
        # 文件系统：线程目录（含 user-data 与压缩存档）+ requirements 目录
        from caspian.sandbox.path_utils import REAL_ROOT

        thread_dir = Path(REAL_ROOT.format(user_id=str(user_id), thread_id=str(thread_id))).parent
        shutil.rmtree(thread_dir, ignore_errors=True)
        shutil.rmtree(Path("requirements") / str(thread_id), ignore_errors=True)

    async def delete(self, user_id: str, thread_id: str) -> dict[str, Any]:
        """级联硬删除会话及其全部派生后裔，不可恢复。

        输入:
            user_id / thread_id — 用户与目标会话

        输出:
            dict — {"deleted": [删除的 thread_id 列表]}
        """
        subtree = await self._subtree(user_id, thread_id)
        # 先清 checkpointer/store/文件系统（best-effort），子先于父
        adjacency = await self._load_adjacency(user_id)
        order = _order(adjacency, set(subtree))
        for tid in order:
            await self._purge_thread(user_id, tid)

        # 再在单个事务里删来源行与 web_threads 行（FK 级联清理 definitions）
        async with self.session_factory() as session:
            await session.execute(
                sa_delete(WebContextSource).where(
                    or_(
                        WebContextSource.context_id.in_(order),
                        WebContextSource.parent_context_id.in_(order),
                    )
                )
            )
            await session.execute(
                sa_delete(WebThread).where(WebThread.thread_id.in_(order))
            )
            await session.commit()
        logger.info("会话级联删除完成: root=%s, count=%s", thread_id, len(order))
        return {"deleted": order}

    async def archive(self, user_id: str, thread_id: str) -> dict[str, Any]:
        """级联归档会话及其派生后裔（软删除，数据保留）。

        输入:
            user_id / thread_id — 用户与目标会话

        输出:
            dict — {"archived": [置为已归档的 thread_id 列表]}
        """
        subtree = await self._subtree(user_id, thread_id)
        now = datetime.now(timezone.utc)
        async with self.session_factory() as session:
            await session.execute(
                update(WebThread)
                .where(WebThread.thread_id.in_(subtree), WebThread.archived_at.is_(None))
                .values(archived_at=now)
            )
            await session.commit()
        logger.info("会话级联归档完成: root=%s, count=%s", thread_id, len(subtree))
        return {"archived": subtree}

    async def restore(self, user_id: str, thread_id: str) -> dict[str, Any]:
        """级联恢复已归档会话及其派生后裔（清除归档标记）。

        输入:
            user_id / thread_id — 用户与目标会话

        输出:
            dict — {"restored": [清除归档标记的 thread_id 列表]}
        """
        subtree = await self._subtree(user_id, thread_id)
        async with self.session_factory() as session:
            await session.execute(
                update(WebThread)
                .where(
                    WebThread.thread_id.in_(subtree), WebThread.archived_at.is_not(None)
                )
                .values(archived_at=None)
            )
            await session.commit()
        logger.info("会话级联恢复完成: root=%s, count=%s", thread_id, len(subtree))
        return {"restored": subtree}

    async def list_archived(self, user_id: str) -> list[dict[str, Any]]:
        """返回当前用户全部已归档会话（按 archived_at 倒序）。

        输入:
            user_id: str — 认证用户标识

        输出:
            list[dict] — [{"thread_id", "title", "archived_at"}, ...]
        """
        async with self.session_factory() as session:
            rows = (
                await session.scalars(
                    select(WebThread)
                    .where(
                        WebThread.user_id == user_id,
                        WebThread.archived_at.is_not(None),
                    )
                    .order_by(WebThread.archived_at.desc())
                )
            ).all()
        return [
            {
                "thread_id": row.thread_id,
                "title": row.title,
                "archived_at": row.archived_at.isoformat() if row.archived_at else None,
            }
            for row in rows
        ]
