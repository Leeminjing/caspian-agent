"""
本文件对外提供 ReplaceSystemStrategy 与 AppendSnapshotStrategy 两种小型更新策略。

输入:
    当前完整 SystemSnapshot、既有 effective record 与消息历史

输出:
    LangGraph 状态增量；Replace 只更新 record，Append 在内容变化时插入唯一完整 snapshot event

具体工作流:
    两种策略都以完整 fingerprint 去重；Append 把新 system 放在尚未发送的尾部 user batch 之前，
    保持已发送前缀不变；A→B→A 每次变化都创建新的 UUID event，连续相同内容零追加。

示例:
    update = AppendSnapshotStrategy().update(snapshot, previous, messages)
"""

from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from caspian.agents.system_prompt.metadata import (
    is_system_fragment,
    make_snapshot_message,
)
from caspian.agents.system_prompt.snapshot import SystemSnapshot
from caspian.config.model_config import SystemPromptUpdate


def snapshot_record(snapshot: SystemSnapshot, mode: SystemPromptUpdate) -> dict[str, Any]:
    return {
        "content": snapshot.content,
        "fingerprint": snapshot.fingerprint,
        "decision_table_version": snapshot.decision_table_version,
        "mode": mode.value,
    }


class ReplaceSystemStrategy:
    """只维护有效快照记录，wire 阶段替换 leading system。"""

    mode = SystemPromptUpdate.REPLACE

    def update(
        self,
        snapshot: SystemSnapshot,
        previous: dict[str, Any] | None,
        messages: list[BaseMessage],
    ) -> dict[str, Any] | None:
        record = snapshot_record(snapshot, self.mode)
        return None if previous == record else {"effective_system_snapshot": record}


class AppendSnapshotStrategy:
    """内容变化时追加唯一完整 snapshot event。"""

    mode = SystemPromptUpdate.IN_HISTORY

    def update(
        self,
        snapshot: SystemSnapshot,
        previous: dict[str, Any] | None,
        messages: list[BaseMessage],
    ) -> dict[str, Any] | None:
        record = snapshot_record(snapshot, self.mode)
        if previous and previous.get("fingerprint") == snapshot.fingerprint:
            return None if previous == record else {"effective_system_snapshot": record}
        cleaned = [message for message in messages if not is_system_fragment(message)]
        event = make_snapshot_message(snapshot, self.mode)
        insertion = len(cleaned)
        while insertion > 0 and isinstance(cleaned[insertion - 1], HumanMessage):
            insertion -= 1
        rebuilt = [*cleaned[:insertion], event, *cleaned[insertion:]]
        return {
            "effective_system_snapshot": record,
            "messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *rebuilt],
        }
