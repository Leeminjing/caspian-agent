"""本文件对外提供并行 Run 会话事件的纯投影与工具消息配对。

输入为既有共享消息、未完成工具对和新增规范事件；输出为去重后的共享历史与待配对消息。
工作流按事件序号处理，每个 Run 的消息保序；操作事件明确标记未生效或已生效，工具调用只有结果齐全才进入共享历史。
示例：`messages, pending = merge_events([], [], event_values)`。
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, message_to_dict, messages_from_dict


def _append_message(messages: list[dict], message: dict) -> None:
    message_id = message.get("data", {}).get("id")
    for index, existing in enumerate(messages):
        if message_id and existing.get("data", {}).get("id") == message_id:
            messages[index] = message
            return
    messages.append(message)


def merge_events(
    messages: list[dict], pending_messages: list[dict], events: list[dict[str, Any]],
) -> tuple[list[dict], list[dict]]:
    merged = list(messages)
    pending = [dict(item) for item in pending_messages]
    for event in events:
        if event["event_type"] == "work_message":
            serialized = event["payload"]["message"]
            message = messages_from_dict([serialized])[0]
            if isinstance(message, AIMessage) and message.tool_calls:
                pending.append({
                    "run_id": event["run_id"],
                    "ai": serialized,
                    "expected": [call["id"] for call in message.tool_calls],
                    "results": [],
                })
                continue
            if isinstance(message, ToolMessage):
                pair = next((item for item in pending if (
                    item.get("run_id") == event["run_id"]
                    and message.tool_call_id in item["expected"]
                )), None)
                if pair is None:
                    continue
                if not any(messages_from_dict([result])[0].tool_call_id == message.tool_call_id for result in pair["results"]):
                    pair["results"].append(serialized)
                received = {messages_from_dict([result])[0].tool_call_id for result in pair["results"]}
                if received >= set(pair["expected"]):
                    _append_message(merged, pair["ai"])
                    for result in pair["results"]:
                        _append_message(merged, result)
                    pending.remove(pair)
                continue
            _append_message(merged, serialized)
            continue
        if event["event_type"] in {
            "processing", "awaiting_approval", "committed", "rejected",
            "cancelled", "failed", "version_conflict",
        }:
            payload = event["payload"]
            status = event["event_type"]
            marker = "已生效" if status == "committed" else "未生效"
            message = HumanMessage(
                id=event["event_id"],
                content=(
                    f"[决策表操作事件 {marker}] "
                    + json.dumps({
                        "operation_id": event.get("operation_id"),
                        "run_id": event["run_id"],
                        "status": status,
                        "detail": payload,
                    }, ensure_ascii=False, sort_keys=True)
                ),
                additional_kwargs={
                    "decision_table_operation_event": True,
                    "effective": status == "committed",
                    "operation_id": event.get("operation_id"),
                },
            )
            _append_message(merged, message_to_dict(message))
    return merged, pending
