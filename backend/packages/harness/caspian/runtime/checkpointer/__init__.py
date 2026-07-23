"""
本文件为 lead_agent.runtime.checkpointer 包的入口，负责重导出 async_provider 子模块的公开 API。

对外提供:
    create_checkpointer — 基于 AppConfig 创建 BaseCheckpointSaver 实例的异步工厂函数
    dispose_checkpointer — 释放 checkpointer 持有的资源
"""

from caspian.runtime.checkpointer.async_provider import create_checkpointer, dispose_checkpointer

__all__ = [
    "create_checkpointer",
    "dispose_checkpointer",
]
