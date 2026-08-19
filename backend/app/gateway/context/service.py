"""
本文件对外提供 ContextService，负责 Web Context 的快照、派生、lineage、执行投影审批
与独立 checkpoint 初始化，以及主运行闸门、线程注册与 usage 聚合。

输入为 LangGraph checkpointer 以及 Context 请求模型；输出为可直接返回给 Context API 的
数据。具体工作流为校验同用户来源和已提交 checkpoint，保存用户 authored messages，
调用 context_projection 编译执行投影，在无需降级或用户批准后以新 thread_id 调用
aupdate_state 创建独立 checkpoint。示例：`context = await service.derive(user_id, body)`。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any
import uuid

from fastapi import HTTPException
from sqlalchemy import select

from backend.app.gateway.context.models import (
    ContextDefinitionUpdate,
    ContextDeriveCreate,
    ContextProjectionDecision,
    WebContextDefinition,
    WebContextSource,
    WebThread,
)
from backend.app.gateway.context.projection import ContextProjection, compile_context_messages
from backend.app.gateway.context.validation import (
    deserialize_messages,
    serialize_message,
    validate_messages,
)
from langchain_core.messages import RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from caspian.persistence.engine import get_session as _default_session_factory


_RUNNABLE_STATUSES = frozenset({"valid", "repaired", "approved"})


def _new_id() -> str:
    return uuid.uuid4().hex


class ContextService:
    def __init__(self, checkpointer: Any, session_factory=None) -> None:
        self.checkpointer = checkpointer
        # 默认用全局 session factory；测试可注入 sqlite 内存 session factory
        self.session_factory = session_factory or _default_session_factory

    async def snapshot(self, user_id: str, context_id: str, checkpoint_id: str | None = None) -> dict[str, Any]:
        async with self.session_factory() as session:
            task = await session.get(WebThread, context_id)
            if not task or task.user_id != user_id:
                raise HTTPException(404, "Context 不存在")
            definition = await session.get(WebContextDefinition, context_id)
        if definition and definition.initial_checkpoint_id is None and checkpoint_id is None:
            return {
                "context_id": context_id,
                "checkpoint_id": None,
                "messages": deepcopy(definition.authored_messages),
            }
        config = {"configurable": {"thread_id": task.thread_id}}
        if checkpoint_id:
            config["configurable"]["checkpoint_id"] = checkpoint_id
        checkpoint = await self.checkpointer.aget_tuple(config)
        if checkpoint is None:
            if checkpoint_id:
                raise HTTPException(404, "Context checkpoint 不存在")
            messages = deepcopy(definition.authored_messages) if definition else []
            return {"context_id": context_id, "checkpoint_id": None, "messages": messages}
        actual_id = checkpoint.config.get("configurable", {}).get("checkpoint_id")
        if checkpoint_id and actual_id != checkpoint_id:
            raise HTTPException(404, "Context checkpoint 不属于该 Context")
        values = checkpoint.checkpoint.get("channel_values", {})
        runtime_messages = [serialize_message(message) for message in values.get("messages", [])]
        messages = (
            self._display_messages(definition, runtime_messages)
            if definition and (not checkpoint_id or checkpoint_id == definition.initial_checkpoint_id)
            else runtime_messages
        )
        return {"context_id": context_id, "checkpoint_id": actual_id, "messages": messages}

    async def derive(self, user_id: str, body: ContextDeriveCreate) -> dict[str, Any]:
        refs = [(source.context_id, source.checkpoint_id) for source in body.sources]
        if len(refs) != len(set(refs)):
            raise HTTPException(422, "Context 来源不能重复")
        async with self.session_factory() as session:
            parents = [await session.get(WebThread, context_id) for context_id, _ in refs]
            if any(parent is None or parent.user_id != user_id for parent in parents):
                raise HTTPException(404, "来源 Context 不存在")
        for parent, (_, checkpoint_id) in zip(parents, refs):
            await self._require_checkpoint(parent.thread_id, checkpoint_id)

        projection = compile_context_messages(body.messages)
        context_id = _new_id()
        thread_id = _new_id()
        async with self.session_factory() as session:
            task = WebThread(
                thread_id=context_id,
                user_id=user_id,
                title=body.title,
            )
            session.add(task)
            await session.flush()
            definition = WebContextDefinition(
                context_id=context_id,
                authored_messages=projection.authored_messages,
                execution_messages=projection.execution_messages,
                repair_manifest=projection.repair_manifest,
                issues=projection.issues,
                definition_hash=projection.definition_hash,
                projection_hash=projection.projection_hash,
                projection_status=projection.status,
                initial_message_ids=[],
            )
            sources = [
                WebContextSource(
                    source_id=_new_id(),
                    context_id=context_id,
                    parent_context_id=source.context_id,
                    source_checkpoint_id=source.checkpoint_id,
                    position=index,
                )
                for index, source in enumerate(body.sources)
            ]
            session.add_all([definition, *sources])
            await session.commit()
        if projection.status != "approval_required":
            await self._initialize(context_id, projection.status)
        return await self.get(user_id, context_id)

    async def update_definition(
        self, user_id: str, context_id: str, body: ContextDefinitionUpdate
    ) -> dict[str, Any]:
        projection = compile_context_messages(body.messages)
        async with self.session_factory() as session:
            definition = await session.scalar(
                select(WebContextDefinition)
                .where(WebContextDefinition.context_id == context_id)
                .with_for_update()
            )
            if not definition:
                raise HTTPException(404, "派生 Context 不存在")
            task = await session.scalar(
                select(WebThread).where(WebThread.thread_id == context_id).with_for_update()
            )
            if not task or task.user_id != user_id:
                raise HTTPException(404, "派生 Context 不存在")
            if task.main_run_started_at is not None:
                raise HTTPException(409, "Context 已开始运行；请从当前快照继续派生")
            self._apply_projection(definition, projection)
            if projection.status != "approval_required":
                definition.projection_status = "initializing"
            definition.decision = None
            definition.decided_definition_hash = None
            definition.decided_projection_hash = None
            definition.decided_at = None
            await session.commit()
        if projection.status != "approval_required":
            await self._initialize(context_id, projection.status)
        return await self.get(user_id, context_id)

    async def decide(
        self, user_id: str, context_id: str, body: ContextProjectionDecision
    ) -> dict[str, Any]:
        async with self.session_factory() as session:
            definition = await session.get(WebContextDefinition, context_id)
            if not definition:
                raise HTTPException(404, "派生 Context 不存在")
            task = await session.get(WebThread, context_id)
            if not task or task.user_id != user_id:
                raise HTTPException(404, "派生 Context 不存在")
            if (
                body.definition_hash != definition.definition_hash
                or body.projection_hash != definition.projection_hash
            ):
                raise HTTPException(409, "Context 定义或拟议投影已经变化，请重新确认")
            if definition.initial_checkpoint_id:
                raise HTTPException(409, "Context 执行 checkpoint 已初始化")
            definition.decision = body.decision
            definition.decided_definition_hash = body.definition_hash
            definition.decided_projection_hash = body.projection_hash
            definition.decided_at = datetime.now(timezone.utc)
            if body.decision == "reject":
                definition.projection_status = "rejected"
            else:
                try:
                    validate_messages(definition.execution_messages)
                except ValueError as exc:
                    raise HTTPException(422, f"拟议投影仍不合法，不能批准：{exc}") from exc
                definition.projection_status = "initializing"
            await session.commit()
        if body.decision == "accept":
            await self._initialize(context_id, "approved")
        return await self.get(user_id, context_id)

    async def get(self, user_id: str, context_id: str) -> dict[str, Any]:
        async with self.session_factory() as session:
            task = await session.get(WebThread, context_id)
            if not task or task.user_id != user_id:
                raise HTTPException(404, "Context 不存在")
            definition = await session.get(WebContextDefinition, context_id)
            sources = (
                await session.scalars(
                    select(WebContextSource)
                    .where(WebContextSource.context_id == context_id)
                    .order_by(WebContextSource.position)
                )
            ).all()
            editable = bool(definition) and task.main_run_started_at is None
            return self._payload(task, definition, sources, editable=editable)

    async def lineage(self, user_id: str, context_id: str) -> dict[str, Any]:
        payload = await self.get(user_id, context_id)
        async with self.session_factory() as session:
            depth = await self._depth(session, context_id, {})
        return {"context_id": context_id, "depth": depth, "sources": payload["sources"]}

    async def tree(self, user_id: str) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            tasks = (
                await session.scalars(
                    select(WebThread)
                    .where(WebThread.user_id == user_id)
                    .order_by(WebThread.created_at)
                )
            ).all()
            source_rows = (
                await session.scalars(
                    select(WebContextSource)
                    .where(WebContextSource.context_id.in_([task.thread_id for task in tasks]))
                    .order_by(WebContextSource.context_id, WebContextSource.position)
                )
            ).all() if tasks else []
            definitions = {
                item.context_id: item
                for item in (
                    await session.scalars(
                        select(WebContextDefinition).where(
                            WebContextDefinition.context_id.in_([task.thread_id for task in tasks])
                        )
                    )
                ).all()
            } if tasks else {}
            by_child: dict[str, list[WebContextSource]] = {}
            for source in source_rows:
                by_child.setdefault(source.context_id, []).append(source)
            memo: dict[str, int] = {}

            result = []
            for task in tasks:
                input_tokens = task.prompt_input_tokens
                hit_tokens = task.prompt_cache_hit_tokens
                result.append({
                    "context_id": task.thread_id,
                    "task_id": task.thread_id,
                    "thread_id": task.thread_id,
                    "title": task.title,
                    "depth": self._depth_from_sources(task.thread_id, by_child, memo),
                    "projection_status": (
                        definitions[task.thread_id].projection_status
                        if task.thread_id in definitions else "root"
                    ),
                    "editable": task.thread_id in definitions and task.main_run_started_at is None,
                    "cache_input_tokens": input_tokens,
                    "cache_hit_tokens": hit_tokens,
                    "cache_hit_rate": hit_tokens / input_tokens if input_tokens else None,
                    "parents": [self._source_payload(source) for source in by_child.get(task.thread_id, [])],
                })
            return result

    async def ensure_runnable(self, user_id: str, context_id: str) -> None:
        async with self.session_factory() as session:
            definition = await session.scalar(
                select(WebContextDefinition)
                .where(WebContextDefinition.context_id == context_id)
                .with_for_update()
            )
            if definition is None:
                return
            task = await session.get(WebThread, context_id)
            if task is None or task.user_id != user_id:
                raise HTTPException(
                    409,
                    {
                        "code": "context_projection_blocked",
                        "message": "Context 不属于当前用户",
                    },
                )
            if definition.projection_status not in _RUNNABLE_STATUSES:
                raise HTTPException(
                    409,
                    {
                        "code": "context_projection_blocked",
                        "message": "Context 执行投影尚未得到安全确认",
                        "context": self._definition_payload(definition),
                    },
                )

    async def register_main_run(self, user_id: str, context_id: str) -> None:
        """注册线程并首写 main_run_started_at（行锁串行化定义更新的竞态）。

        线程不存在时 lazy 创建；属于其他用户的同名线程不触碰（thread_id 为
        客户端随机 UUID，正常不会冲突）。"""
        async with self.session_factory() as session:
            task = await session.scalar(
                select(WebThread).where(WebThread.thread_id == context_id).with_for_update()
            )
            if task is None:
                session.add(
                    WebThread(
                        thread_id=context_id,
                        user_id=user_id,
                        title=None,
                        main_run_started_at=datetime.now(timezone.utc),
                    )
                )
            elif task.user_id == user_id and task.main_run_started_at is None:
                task.main_run_started_at = datetime.now(timezone.utc)
            await session.commit()

    async def accumulate_usage(
        self, context_id: str, input_tokens: int, cache_hit_tokens: int
    ) -> None:
        # ponytail: 无 runs 表，聚合列足够展示命中率；行锁保护同线程并发 run 的 read-modify-write
        if not input_tokens and not cache_hit_tokens:
            return
        async with self.session_factory() as session:
            task = await session.scalar(
                select(WebThread).where(WebThread.thread_id == context_id).with_for_update()
            )
            if task is None:
                return
            task.prompt_input_tokens += input_tokens
            task.prompt_cache_hit_tokens += cache_hit_tokens
            await session.commit()

    async def _initialize(self, context_id: str, ready_status: str) -> None:
        async with self.session_factory() as session:
            task = await session.get(WebThread, context_id)
            definition = await session.get(WebContextDefinition, context_id)
            if not task or not definition:
                raise HTTPException(404, "Context 不存在")
            config = {"configurable": {"thread_id": task.thread_id}}
            existing = await self.checkpointer.aget_tuple(config)
            execution_messages = deepcopy(definition.execution_messages)
            replace_existing = False
            if existing is not None:
                values = existing.checkpoint.get("channel_values", {})
                stored = [serialize_message(message) for message in values.get("messages", [])]
                if self._comparable_messages(stored) != self._comparable_messages(execution_messages):
                    replace_existing = True
            definition.projection_status = "initializing"
            await session.commit()
        try:
            if existing is None or replace_existing:
                graph = await self._make_state_graph()
                messages = deserialize_messages(execution_messages)
                for index, (raw, message) in enumerate(zip(execution_messages, messages)):
                    if raw.get("curation_synthetic"):
                        message.additional_kwargs["curation_synthetic"] = True
                    if message.id is None:
                        # ponytail: 为用户手写无 id 消息分配稳定 id，展示投影据此过滤初始消息，
                        # 否则无 id 消息会在 suffix 中重复显示
                        message.id = f"{context_id}-init-{index}"
                update = (
                    [RemoveMessage(id=REMOVE_ALL_MESSAGES), *messages]
                    if replace_existing
                    else messages
                )
                updated_config = await graph.aupdate_state(
                    config,
                    {"messages": update},
                    as_node="model" if replace_existing else None,
                )
                state = await graph.aget_state(updated_config)
                checkpoint_id = state.config.get("configurable", {}).get("checkpoint_id")
                initial_ids = [message.id for message in state.values.get("messages", []) if message.id]
            else:
                checkpoint_id = existing.config.get("configurable", {}).get("checkpoint_id")
                values = existing.checkpoint.get("channel_values", {})
                initial_ids = [message.id for message in values.get("messages", []) if message.id]
        except Exception as exc:
            async with self.session_factory() as session:
                definition = await session.get(WebContextDefinition, context_id)
                if definition:
                    definition.projection_status = "initialization_failed"
                    definition.issues = [*definition.issues, {"index": None, "reason": str(exc)}]
                    await session.commit()
            return
        async with self.session_factory() as session:
            definition = await session.get(WebContextDefinition, context_id)
            if not definition:
                return
            definition.initial_checkpoint_id = checkpoint_id
            definition.initial_message_ids = initial_ids
            definition.projection_status = ready_status
            await session.commit()

    async def _make_state_graph(self):
        from langchain.agents import create_agent

        from caspian.agents.lead_agent_state import LeadAgentState
        from caspian.models import create_chat_model

        graph = create_agent(
            model=create_chat_model(),
            tools=[],
            middleware=[],
            system_prompt="",
            state_schema=LeadAgentState,
        )
        graph.checkpointer = self.checkpointer
        return graph

    async def _require_checkpoint(self, thread_id: str, checkpoint_id: str) -> None:
        checkpoint = await self.checkpointer.aget_tuple(
            {"configurable": {"thread_id": thread_id, "checkpoint_id": checkpoint_id}}
        )
        actual = checkpoint.config.get("configurable", {}).get("checkpoint_id") if checkpoint else None
        if actual != checkpoint_id:
            raise HTTPException(404, "来源 checkpoint 不存在或不属于指定 Context")

    async def _depth(self, session, context_id: str, memo: dict[str, int]) -> int:
        """沿第一个父来源计算父链深度（受保护 helper）。

        输入:
            session: AsyncSession — 数据库会话
            context_id: str — 目标 Context
            memo: dict[str, int] — 深度缓存

        输出:
            int — 父链深度，无来源为 0

        具体工作流:
            (1) 命中缓存直接返回
            (2) 加载该 Context 所属用户的全部 WebContextSource 行构建 by_child
                （与 tree() 同量级的两条查询，换取与 tree 共享同一计算实现）
            (3) 调用 _depth_from_sources 纯函数计算并缓存
        """
        if context_id in memo:
            return memo[context_id]
        task = await session.get(WebThread, context_id)
        if task is None:
            memo[context_id] = 0
            return 0
        task_ids = [
            item.thread_id
            for item in (
                await session.scalars(
                    select(WebThread).where(WebThread.user_id == task.user_id)
                )
            ).all()
        ]
        source_rows = (
            await session.scalars(
                select(WebContextSource)
                .where(WebContextSource.context_id.in_(task_ids))
                .order_by(WebContextSource.context_id, WebContextSource.position)
            )
        ).all()
        by_child: dict[str, list[WebContextSource]] = {}
        for source in source_rows:
            by_child.setdefault(source.context_id, []).append(source)
        memo[context_id] = self._depth_from_sources(context_id, by_child, memo)
        return memo[context_id]

    @staticmethod
    def _depth_from_sources(
        context_id: str,
        by_child: dict[str, list[WebContextSource]],
        memo: dict[str, int],
    ) -> int:
        """按第一个父来源沿父链计算深度（受保护 helper，纯函数）。

        输入:
            context_id: str — 目标 Context
            by_child: dict[str, list[WebContextSource]] — context_id → 其来源行列表
            memo: dict[str, int] — 深度缓存

        输出:
            int — 父链深度，无来源为 0

        具体工作流:
            (1) 命中缓存直接返回
            (2) 无来源 → 0
            (3) 取第一个来源的父 Context 递归加一
        """
        if context_id in memo:
            return memo[context_id]
        sources = by_child.get(context_id, [])
        memo[context_id] = (
            0
            if not sources
            else ContextService._depth_from_sources(
                sources[0].parent_context_id, by_child, memo
            )
            + 1
        )
        return memo[context_id]

    @staticmethod
    def _apply_projection(definition: WebContextDefinition, projection: ContextProjection) -> None:
        definition.authored_messages = projection.authored_messages
        definition.execution_messages = projection.execution_messages
        definition.repair_manifest = projection.repair_manifest
        definition.issues = projection.issues
        definition.definition_hash = projection.definition_hash
        definition.projection_hash = projection.projection_hash
        definition.projection_status = projection.status
        definition.initial_message_ids = []
        definition.initial_checkpoint_id = None

    @staticmethod
    def _display_messages(
        definition: WebContextDefinition | None,
        runtime_messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if definition is None:
            return runtime_messages
        initial_ids = set(definition.initial_message_ids or [])
        suffix = [message for message in runtime_messages if message.get("id") not in initial_ids]
        return [*deepcopy(definition.authored_messages), *suffix]

    @staticmethod
    def _comparable_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = deepcopy(messages)
        for message in result:
            message.pop("id", None)
            message.pop("locked", None)
            message.pop("curation_synthetic", None)
        return result

    @classmethod
    def _payload(
        cls,
        task: WebThread,
        definition: WebContextDefinition | None,
        sources: list[WebContextSource],
        *,
        editable: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "context_id": task.thread_id,
            "task_id": task.thread_id,
            "thread_id": task.thread_id,
            "title": task.title,
            "projection_status": "root",
            "sources": [cls._source_payload(source) for source in sources],
            "editable": editable,
        }
        if definition:
            payload.update(cls._definition_payload(definition))
        return payload

    @staticmethod
    def _definition_payload(definition: WebContextDefinition) -> dict[str, Any]:
        return {
            "projection_status": definition.projection_status,
            "authored_messages": deepcopy(definition.authored_messages),
            "execution_messages": deepcopy(definition.execution_messages),
            "repair_manifest": deepcopy(definition.repair_manifest),
            "issues": deepcopy(definition.issues),
            "definition_hash": definition.definition_hash,
            "projection_hash": definition.projection_hash,
            "initial_checkpoint_id": definition.initial_checkpoint_id,
            "decision": definition.decision,
        }

    @staticmethod
    def _source_payload(source: WebContextSource) -> dict[str, Any]:
        return {
            "context_id": source.parent_context_id,
            "checkpoint_id": source.source_checkpoint_id,
            "position": source.position,
        }
