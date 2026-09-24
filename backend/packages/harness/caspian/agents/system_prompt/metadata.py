"""
本文件对外提供托管 system 消息元数据、fragment 收集与纯重基线函数。

输入:
    BaseMessage 序列，以及需要持久化为 append event 的 SystemSnapshot

输出:
    唯一 ID 的托管 SystemMessage、已注册 fragments、未托管 system 列表、RebasedSystemHistory

具体工作流:
    additional_kwargs 标记消息职责；append event 每次用 UUID 生成新 ID；fragment 按稳定 identity
    last-write-wins；重基线选择最后一份完整 snapshot，丢弃旧 snapshot、legacy table patch 与 fragment 载体。

示例:
    rebased = rebase_system_history(messages)
"""

from dataclasses import dataclass
from uuid import uuid4

from langchain_core.messages import BaseMessage, SystemMessage

from caspian.agents.system_prompt.snapshot import SystemFragment, SystemSnapshot
from caspian.config.model_config import SystemPromptUpdate

SYSTEM_SNAPSHOT_MARKER_KEY = "caspian_system_snapshot"
SYSTEM_FRAGMENT_MARKER_KEY = "caspian_system_fragment"
SNAPSHOT_FINGERPRINT_KEY = "caspian_system_fingerprint"
SNAPSHOT_TABLE_VERSION_KEY = "caspian_decision_table_version"
SNAPSHOT_MODE_KEY = "caspian_system_update_mode"
LEGACY_DECISION_TABLE_MESSAGE_ID = "decision-table"
SNAPSHOT_MESSAGE_ID_PREFIX = "caspian-system-snapshot-"


@dataclass(frozen=True, slots=True)
class RebasedSystemHistory:
    """重基线后的最新快照与普通对话。"""

    latest_snapshot: SystemMessage | None
    ordinary_messages: tuple[BaseMessage, ...]


def make_snapshot_message(
    snapshot: SystemSnapshot,
    mode: SystemPromptUpdate,
) -> SystemMessage:
    return SystemMessage(
        content=snapshot.content,
        id=f"{SNAPSHOT_MESSAGE_ID_PREFIX}{uuid4().hex}",
        additional_kwargs={
            SYSTEM_SNAPSHOT_MARKER_KEY: True,
            SNAPSHOT_FINGERPRINT_KEY: snapshot.fingerprint,
            SNAPSHOT_TABLE_VERSION_KEY: snapshot.decision_table_version,
            SNAPSHOT_MODE_KEY: mode.value,
        },
    )


def is_managed_snapshot(message: BaseMessage) -> bool:
    return isinstance(message, SystemMessage) and bool(
        (message.additional_kwargs or {}).get(SYSTEM_SNAPSHOT_MARKER_KEY)
    )


def is_system_fragment(message: BaseMessage) -> bool:
    return isinstance(message, SystemMessage) and isinstance(
        (message.additional_kwargs or {}).get(SYSTEM_FRAGMENT_MARKER_KEY), str
    )


def is_legacy_decision_table(message: BaseMessage) -> bool:
    return isinstance(message, SystemMessage) and message.id == LEGACY_DECISION_TABLE_MESSAGE_ID


def collect_system_fragments(messages: list[BaseMessage]) -> tuple[SystemFragment, ...]:
    by_identity: dict[str, SystemFragment] = {}
    order: list[str] = []
    for message in messages:
        if not is_system_fragment(message):
            continue
        identity = str(message.additional_kwargs[SYSTEM_FRAGMENT_MARKER_KEY]).strip()
        if not identity:
            continue
        if identity not in by_identity:
            order.append(identity)
        by_identity[identity] = SystemFragment(identity, str(message.content))
    return tuple(by_identity[identity] for identity in order)


def unmanaged_system_messages(messages: list[BaseMessage]) -> tuple[SystemMessage, ...]:
    return tuple(
        message
        for message in messages
        if isinstance(message, SystemMessage)
        and not is_managed_snapshot(message)
        and not is_system_fragment(message)
        and not is_legacy_decision_table(message)
    )


def is_system_control_message(message: BaseMessage) -> bool:
    return (
        is_managed_snapshot(message)
        or is_system_fragment(message)
        or is_legacy_decision_table(message)
    )


def rebase_system_history(messages: list[BaseMessage]) -> RebasedSystemHistory:
    latest = next(
        (message for message in reversed(messages) if is_managed_snapshot(message)),
        None,
    )
    ordinary = tuple(message for message in messages if not is_system_control_message(message))
    return RebasedSystemHistory(latest_snapshot=latest, ordinary_messages=ordinary)
