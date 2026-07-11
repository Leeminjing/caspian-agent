"""
本文件为 lead_agent.persistence 包的入口，负责重导出 engine 子模块的公开 API，并注册 checkpoint ORM 模型到 Base.metadata。

对外提供:
    init_engine — 创建（或返回已有）AsyncEngine 全局单例
    dispose_engine — 释放 AsyncEngine 连接池并重置全局单例
    get_session — 通过全局 session factory 创建新的 AsyncSession 实例
    checkpoint_models — 四张 checkpointer 表的 ORM 模型（隐式注册到 Base.metadata 供 Alembic autogenerate 发现）
"""

from lead_agent.persistence.engine import dispose_engine, get_session, init_engine

# 导入 checkpoint ORM 模型以注册到 Base.metadata（供 Alembic autogenerate 发现）
import lead_agent.persistence.checkpoint_models  # noqa: F401

# 导入 store ORM 模型以注册到 Base.metadata（供 Alembic autogenerate 发现）
import lead_agent.persistence.store_models  # noqa: F401


__all__ = [
    "dispose_engine",
    "get_session",
    "init_engine",
]
