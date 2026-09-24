"""
本文件对外提供 SystemSnapshotMiddleware，统一编排完整 system snapshot 的生命周期。

输入:
    static_prompt、显式 SystemPromptUpdate capability、plan policy；运行时 state 与 thread/user identity

输出:
    before_agent/before_model 的 checkpoint-safe 状态增量，以及已按 Replace/AppendSnapshot 准备的模型请求

具体工作流:
    每个 agent/model 边界重读 authoritative Decision Table，收集 plan、delegations 与标记 fragments，
    builder 生成完整快照，strategy 去重并更新历史；wrapper 为 Replace 发送单一 leading system，
    为 Append 保留静态 seed 与托管快照。检测到未标记 SystemMessage 时记录诊断并按本次 Replace fail closed。

示例:
    middleware = SystemSnapshotMiddleware("base", SystemPromptUpdate.REPLACE)
"""

import logging
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain_core.messages import BaseMessage, SystemMessage

from caspian.agents.commitment.decision_table import read_decision_table
from caspian.agents.system_prompt.metadata import (
    collect_system_fragments,
    is_legacy_decision_table,
    is_managed_snapshot,
    is_system_control_message,
    unmanaged_system_messages,
)
from caspian.agents.system_prompt.snapshot import SystemSnapshot, SystemSnapshotBuilder
from caspian.agents.system_prompt.strategy import (
    AppendSnapshotStrategy,
    ReplaceSystemStrategy,
    snapshot_record,
)
from caspian.config.model_config import SystemPromptUpdate

logger = logging.getLogger(__name__)


class SystemSnapshotMiddleware(AgentMiddleware):
    """在生命周期边界构建快照，并把 wire 语义委托给小策略。"""

    def __init__(
        self,
        static_prompt: str,
        update_mode: SystemPromptUpdate = SystemPromptUpdate.REPLACE,
        *,
        plan_section: str = "",
    ) -> None:
        super().__init__()
        self._builder = SystemSnapshotBuilder(static_prompt, plan_section=plan_section)
        self._configured_mode = update_mode
        self._replace = ReplaceSystemStrategy()
        self._append = AppendSnapshotStrategy()

    def _identity(self, runtime: Any) -> tuple[str | None, str | None]:
        thread_id = getattr(getattr(runtime, "execution_info", None), "thread_id", None)
        context = getattr(runtime, "context", None)
        user_id = context.get("user_id") if isinstance(context, dict) else None
        return (
            str(thread_id) if thread_id is not None else None,
            str(user_id) if user_id else None,
        )

    def _build_snapshot(self, state: dict, runtime: Any) -> tuple[SystemSnapshot, bool]:
        messages = list(state.get("messages") or [])
        thread_id, user_id = self._identity(runtime)
        table = read_decision_table(thread_id, user_id=user_id) if thread_id else None
        snapshot = self._builder.build(
            plan_active=bool(state.get("plan_active")),
            delegations=list(state.get("delegations") or []),
            fragments=collect_system_fragments(messages),
            decision_table=table,
        )
        unsafe = bool(unmanaged_system_messages(messages))
        return snapshot, unsafe

    def _refresh(self, state: dict, runtime: Any) -> dict | None:
        snapshot, unsafe = self._build_snapshot(state, runtime)
        messages = list(state.get("messages") or [])
        previous = state.get("effective_system_snapshot")
        if unsafe and self._configured_mode is SystemPromptUpdate.IN_HISTORY:
            logger.warning(
                "SystemSnapshotMiddleware: 检测到未注册 SystemMessage，当前调用降级为 replace"
            )
            record = snapshot_record(snapshot, SystemPromptUpdate.REPLACE)
            return None if previous == record else {"effective_system_snapshot": record}
        if (
            self._configured_mode is SystemPromptUpdate.IN_HISTORY
            and previous is None
            and snapshot.content == self._builder.static_prompt
            and not any(
                is_managed_snapshot(message) or is_legacy_decision_table(message)
                for message in messages
            )
        ):
            return {
                "effective_system_snapshot": snapshot_record(
                    snapshot, SystemPromptUpdate.IN_HISTORY
                )
            }
        strategy = (
            self._append
            if self._configured_mode is SystemPromptUpdate.IN_HISTORY
            else self._replace
        )
        return strategy.update(snapshot, previous, messages)

    def before_agent(self, state, runtime):
        return self._refresh(dict(state), runtime)

    async def abefore_agent(self, state, runtime):
        return self._refresh(dict(state), runtime)

    def before_model(self, state, runtime):
        return self._refresh(dict(state), runtime)

    async def abefore_model(self, state, runtime):
        return self._refresh(dict(state), runtime)

    def _effective_snapshot(self, request: ModelRequest) -> SystemSnapshot:
        record = (request.state or {}).get("effective_system_snapshot") or {}
        content = str(record.get("content") or self._builder.static_prompt)
        return SystemSnapshot.from_content(
            content,
            decision_table_version=record.get("decision_table_version"),
        )

    def _prepare_request(self, request: ModelRequest) -> ModelRequest:
        snapshot = self._effective_snapshot(request)
        record = (request.state or {}).get("effective_system_snapshot") or {}
        actual_mode = SystemPromptUpdate(record.get("mode", SystemPromptUpdate.REPLACE.value))
        messages: list[BaseMessage] = list(request.messages)
        unmanaged = unmanaged_system_messages(messages)
        if unmanaged or actual_mode is SystemPromptUpdate.REPLACE:
            if unmanaged:
                logger.warning(
                    "SystemSnapshotMiddleware: wire 中存在未注册 SystemMessage，已拒绝该注入并使用 replace"
                )
            ordinary = [message for message in messages if not isinstance(message, SystemMessage)]
            return request.override(
                system_message=SystemMessage(content=snapshot.content),
                messages=ordinary,
            )
        visible = [
            message
            for message in messages
            if is_managed_snapshot(message) or not is_system_control_message(message)
        ]
        return request.override(messages=visible)

    def wrap_model_call(self, request, handler):
        return handler(self._prepare_request(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._prepare_request(request))
