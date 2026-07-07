"""
本文件为 lead_agent.persistence 包的入口，负责重导出 engine 子模块的公开 API。

对外提供:
    init_engine — 创建（或返回已有）AsyncEngine 全局单例
    dispose_engine — 释放 AsyncEngine 连接池并重置全局单例
    get_session — 通过全局 session factory 创建新的 AsyncSession 实例
"""

from lead_agent.persistence.engine import dispose_engine, get_session, init_engine

__all__ = [
    "dispose_engine",
    "get_session",
    "init_engine",
]
