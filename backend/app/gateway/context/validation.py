"""
本文件对外提供 Context 执行投影的消息结构校验与反序列化函数。

对外提供:
    validate_messages — 校验消息列表结构合法（tool call 关联完整性）
    deserialize_messages — 消息 dict 列表 → LangChain BaseMessage 列表（先校验结构）

输入:
    validate_messages: messages — 消息 dict 列表（含 role/content/id/tool_calls/tool_call_id/files）
    deserialize_messages: messages — 同上

输出:
    validate_messages → None（非法时抛 ValueError，含消息序号）
    deserialize_messages → list[BaseMessage]

具体工作流:
    (1) validate_messages 逐条校验角色、content 类型、tool_calls 归属与 ID 唯一、
        ToolMessage 调用方完整性；悬空 tool call 或孤立 ToolMessage 均抛 ValueError
    (2) deserialize_messages 先校验结构，再按角色还原 Human/AI/System/Tool Message，
        保留 id / files / tool_calls / tool_call_id / name 字段

示例:
    messages = deserialize_messages([{"role": "human", "content": "你好"}])
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage


def serialize_message(message: BaseMessage) -> dict[str, Any]:
    """LangChain BaseMessage → 前端消息 dict（role/content/id/tool_calls/locked/files）。

    输入:
        message: BaseMessage — 消息实例

    输出:
        dict — 与 authored 定义同构的 role 风格消息 dict
    """
    if isinstance(message, HumanMessage):
        role = "human"
    elif isinstance(message, AIMessage):
        role = "ai"
    elif isinstance(message, SystemMessage):
        role = "system"
    elif isinstance(message, ToolMessage):
        role = "tool"
    else:
        role = message.type
    result: dict[str, Any] = {"role": role, "content": message.content}
    if message.id:
        result["id"] = message.id
    if isinstance(message, AIMessage) and message.tool_calls:
        result["tool_calls"] = message.tool_calls
        result["locked"] = True
    if isinstance(message, ToolMessage):
        result["tool_call_id"] = message.tool_call_id
        result["name"] = message.name
        result["locked"] = True
    files = message.additional_kwargs.get("files") if message.additional_kwargs else None
    if files:
        result["files"] = files
    return result


def validate_messages(messages: list[dict[str, Any]]) -> None:
    """校验消息列表结构合法：角色有效、tool call 与 ToolMessage 关联完整。

    输入:
        messages: list[dict] — 消息列表

    输出:
        None — 结构合法；非法时抛 ValueError（含消息序号）
    """
    pending_calls: dict[str, int] = {}
    resolved_calls: set[str] = set()
    for index, message in enumerate(messages):
        role = message.get("role")
        content = message.get("content", "")
        unresolved = set(pending_calls) - resolved_calls
        if unresolved and role != "tool":
            raise ValueError(
                f"消息 {index + 1} 之前必须紧跟完成工具结果: {', '.join(sorted(unresolved))}"
            )
        if role not in {"human", "user", "ai", "assistant", "system", "tool"}:
            raise ValueError(f"消息 {index + 1} 的角色无效")
        if not isinstance(content, (str, list)):
            raise ValueError(f"消息 {index + 1} 的 content 必须是文本或内容块")
        tool_calls = message.get("tool_calls", [])
        if tool_calls:
            if role not in {"ai", "assistant"}:
                raise ValueError(f"消息 {index + 1} 的 tool_calls 只能属于 AIMessage")
            for call in tool_calls:
                call_id = call.get("id") if isinstance(call, dict) else None
                if not call_id or call_id in pending_calls:
                    raise ValueError(f"消息 {index + 1} 包含无效或重复 tool call id")
                pending_calls[call_id] = index
        if role == "tool":
            call_id = message.get("tool_call_id")
            if not call_id or call_id not in pending_calls or call_id in resolved_calls:
                raise ValueError(f"消息 {index + 1} 的 ToolMessage 没有合法调用方")
            resolved_calls.add(call_id)
    unresolved = set(pending_calls) - resolved_calls
    if unresolved:
        raise ValueError(f"工具调用缺少结果: {', '.join(sorted(unresolved))}")


def deserialize_messages(messages: list[dict[str, Any]]) -> list[BaseMessage]:
    """消息 dict 列表 → LangChain BaseMessage 列表（先校验结构）。

    输入:
        messages: list[dict] — 消息（role/content/id/files/tool_calls/tool_call_id）

    输出:
        list[BaseMessage] — 按角色还原的消息实例

    工作流:
        (1) validate_messages 校验 tool call 关联完整性
        (2) human/user → HumanMessage；ai/assistant → AIMessage（含 tool_calls）；
            system → SystemMessage；tool → ToolMessage
    """
    validate_messages(messages)
    result: list[BaseMessage] = []
    for message in messages:
        role = message.get("role")
        kwargs: dict[str, Any] = {}
        if message.get("id"):
            kwargs["id"] = message["id"]
        if message.get("files"):
            kwargs["additional_kwargs"] = {"files": message["files"]}
        if role in {"human", "user"}:
            result.append(HumanMessage(content=message.get("content", ""), **kwargs))
        elif role in {"ai", "assistant"}:
            if message.get("tool_calls"):
                kwargs["tool_calls"] = message["tool_calls"]
            result.append(AIMessage(content=message.get("content", ""), **kwargs))
        elif role == "system":
            result.append(SystemMessage(content=message.get("content", ""), **kwargs))
        else:
            result.append(
                ToolMessage(
                    content=message.get("content", ""),
                    tool_call_id=message["tool_call_id"],
                    name=message.get("name"),
                    **kwargs,
                )
            )
    return result
