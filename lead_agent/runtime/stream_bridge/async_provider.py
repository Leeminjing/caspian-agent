"""
本文件对外提供 create_stream_bridge 异步上下文管理器工厂函数。

对外提供:
    create_stream_bridge(app_config) — 基于 AppConfig 依赖注入创建 StreamBridge 实例

输入:
    app_config: AppConfig — 应用配置对象，其 stream_bridge 字段决定实现类型

输出:
    StreamBridge 上下文管理器实例（async with ... as bridge:）

工作流:
    (1) 读取 app_config.stream_bridge.type，若为空默认 "memory"
    (2) type == "memory" → 创建 MemoryStreamBridge(queue_maxsize=<配置值>)
    (3) yield 返回 bridge 实例
    (4) finally 清理所有 _streams 中的 run 资源

示例:
    from lead_agent.runtime.stream_bridge.async_provider import create_stream_bridge

    async with create_stream_bridge(app_config) as bridge:
        bridge.publish("run-001", event)
        async for e in bridge.subscribe("run-001"):
            ...
"""

import contextlib
import logging
from collections.abc import AsyncIterator

from lead_agent.config.app_config import AppConfig

logger = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def create_stream_bridge(app_config: AppConfig) -> AsyncIterator:
    """异步上下文管理器，基于配置创建 StreamBridge 实例。

    输入:
        app_config: AppConfig — 应用配置对象

    输出:
        AsyncIterator → 进入上下文时返回 StreamBridge 实例
    """
    bridge_type = (app_config.stream_bridge.type or "memory") if app_config.stream_bridge else "memory"
    maxsize = app_config.stream_bridge.queue_maxsize if app_config.stream_bridge else 512

    if bridge_type == "memory":
        from lead_agent.runtime.stream_bridge.memory import MemoryStreamBridge

        bridge = MemoryStreamBridge(queue_maxsize=maxsize)
    else:
        raise ValueError(f"不支持的 StreamBridge 类型: '{bridge_type}'，仅支持 'memory'")

    logger.info("StreamBridge 已创建 (type=%s, queue_maxsize=%d)", bridge_type, maxsize)
    try:
        yield bridge
    finally:
        for run_id in list(bridge._streams.keys()):
            bridge.cleanup(run_id)
        logger.info("StreamBridge 已关闭，所有 run 资源已清理")
