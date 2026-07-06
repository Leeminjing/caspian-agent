"""
本文件对外提供 StreamEvent 不可变数据类及两个哨兵常量。

对外提供:
    StreamEvent — 单次流式事件的不可变载体，用作 SSE 的数据单元
    HEARTBEAT_SENTINEL — 心跳哨兵，订阅者在 heartbeat_interval 秒内无新事件时产出
    END_SENTINEL — 结束哨兵，生产者调用 publish_end 后订阅者消费完所有缓存事件时产出

输入:
    StreamEvent(id, event, data):
        id: str    — 单调递增的事件 ID，用作 SSE 的 `id:` 字段，支持 Last-Event-ID 断线重连
        event: str — SSE 事件名称，例如 "metadata"、"updates"、"events"、"error"
        data: Any  — 可序列化为 JSON 的载荷数据

输出:
    StreamEvent 实例

工作流:
    StreamEvent 为 frozen dataclass，创建后不可变。哨兵常量 id 为空字符串，
    由 MemoryStreamBridge 在 publish 时为普通事件自动分配单调递增 ID。

示例:
    from lead_agent.runtime.stream_bridge.schemas import StreamEvent, HEARTBEAT_SENTINEL, END_SENTINEL

    event = StreamEvent(id="1", event="metadata", data={"run_id": "abc"})
    if event is HEARTBEAT_SENTINEL:
        ...  # 心跳，保持连接存活
    if event is END_SENTINEL:
        ...  # 流结束，关闭连接
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StreamEvent:
    """单次流式事件。

    属性:
        id:   单调递增的事件 ID，用作 SSE 的 `id:` 字段，支持通过 `Last-Event-ID` 断线重连。
        event: SSE 事件名称，例如 "metadata"、"updates"、"events"、"error"、"end"。
        data:  可序列化为 JSON 的载荷数据，即真正要传的事件本体。
              MUST 在 publish 前由 producer 确保为 JSON 可序列化（dict/list/str/int/float/bool/None 及其嵌套组合）。
    """

    id: str
    event: str
    data: Any


HEARTBEAT_SENTINEL = StreamEvent(id="", event="__heartbeat__", data=None)
END_SENTINEL = StreamEvent(id="", event="__end__", data=None)
