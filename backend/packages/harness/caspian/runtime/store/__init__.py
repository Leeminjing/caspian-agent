"""
本文件为 lead_agent.runtime.store 包的入口，负责重导出 async_provider 子模块的公开 API。

对外提供:
    create_store — 基于 AppConfig 创建 BaseStore 实例的异步工厂函数
    dispose_store — 释放 store 持有的资源
"""

from caspian.runtime.store.async_provider import create_store, dispose_store

__all__ = [
    "create_store",
    "dispose_store",
]
