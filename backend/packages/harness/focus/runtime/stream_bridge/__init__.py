"""
本文件为 lead_agent.runtime.stream_bridge 包的入口，负责重导出各子模块的公开 API。

对外提供:
    StreamEvent — 单次流式事件的不可变载体
    HEARTBEAT_SENTINEL — 心跳哨兵
    END_SENTINEL — 结束哨兵
    StreamBridge — 流式桥接抽象基类
    MemoryStreamBridge — StreamBridge 的进程内内存实现
    create_stream_bridge — 异步上下文管理器工厂函数
"""

from focus.runtime.stream_bridge.async_provider import create_stream_bridge
from focus.runtime.stream_bridge.base import StreamBridge
from focus.runtime.stream_bridge.memory import MemoryStreamBridge
from focus.runtime.stream_bridge.schemas import (
    END_SENTINEL,
    HEARTBEAT_SENTINEL,
    StreamEvent,
)

__all__ = [
    "END_SENTINEL",
    "HEARTBEAT_SENTINEL",
    "MemoryStreamBridge",
    "StreamBridge",
    "StreamEvent",
    "create_stream_bridge",
]
