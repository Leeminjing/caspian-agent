"""
本文件为 lead_agent.runtime 包的入口，负责重导出 runs、stream_bridge 和 checkpointer 子包的公开 API。

对外提供:
    RunStatus — run 生命周期状态枚举
    DisconnectMode — SSE 断开连接行为枚举
    RunRecord — 单次 run 的运行时档案
    RunManager — 进程内 run 状态管理注册表
    RunStore — run 元数据存储抽象基类
    MemoryRunStore — RunStore 的内存实现
    StreamEvent — 单次流式事件的不可变载体
    HEARTBEAT_SENTINEL — 心跳哨兵
    END_SENTINEL — 结束哨兵
    StreamBridge — 流式桥接抽象基类
    MemoryStreamBridge — StreamBridge 的进程内内存实现
    create_stream_bridge — 异步上下文管理器工厂函数
    create_checkpointer — 基于 AppConfig 创建 BaseCheckpointSaver 实例的异步工厂函数
    dispose_checkpointer — 释放 checkpointer 持有的资源
"""

from lead_agent.runtime.runs import (
    DisconnectMode,
    MemoryRunStore,
    RunManager,
    RunRecord,
    RunStatus,
    RunStore,
)
from lead_agent.runtime.stream_bridge import (
    END_SENTINEL,
    HEARTBEAT_SENTINEL,
    MemoryStreamBridge,
    StreamBridge,
    StreamEvent,
    create_stream_bridge,
)
from lead_agent.runtime.checkpointer import (
    create_checkpointer,
    dispose_checkpointer,
)

__all__ = [
    "create_checkpointer",
    "create_stream_bridge",
    "dispose_checkpointer",
    "DisconnectMode",
    "END_SENTINEL",
    "HEARTBEAT_SENTINEL",
    "MemoryRunStore",
    "MemoryStreamBridge",
    "RunManager",
    "RunRecord",
    "RunStatus",
    "RunStore",
    "StreamBridge",
    "StreamEvent",
    "create_stream_bridge",
]
