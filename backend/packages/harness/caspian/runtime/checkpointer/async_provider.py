"""
本文件对外提供 create_checkpointer 异步工厂函数，基于 AppConfig 创建 BaseCheckpointSaver 实例。

对外提供:
    create_checkpointer(config: AppConfig) -> BaseCheckpointSaver

输入:
    config: AppConfig — 应用配置对象，内部从 config.checkpointer 提取 CheckpointerConfig

输出:
    BaseCheckpointSaver — AsyncPostgresSaver 或 InMemorySaver 实例

具体工作流:
    (1) 读取 config.checkpointer.type
    (2) type == "postgres" → 从 config.database.url 创建 psycopg AsyncConnection
        （DSN 附 connect_timeout，规避 Windows 上 libpq 非阻塞连接的等待问题）
        → 创建 AsyncPostgresSaver(conn) 实例并返回；不调用 setup()（表已由 Alembic 迁移管理）
    (3) type == "memory" → 创建 InMemorySaver 实例并返回
    (4) 其他 → 抛 ValueError

示例:
    from caspian.runtime.checkpointer import create_checkpointer
    from caspian.config import get_app_config

    app_config = get_app_config("config.yaml")
    checkpointer = await create_checkpointer(app_config)
"""

import logging

from langgraph.checkpoint.base import BaseCheckpointSaver

from caspian.config.app_config import AppConfig

logger = logging.getLogger(__name__)


async def create_checkpointer(config: AppConfig) -> BaseCheckpointSaver:
    checkpointer_type = config.checkpointer.type

    if checkpointer_type == "postgres":
        if config.database is None:
            raise RuntimeError("AppConfig.database 为空，无法创建 PostgresSaver")

        # ponytail: 去掉 SQLAlchemy 的 +asyncpg 驱动前缀，psycopg 只需要 postgresql://
        dsn = config.database.url.replace("postgresql+asyncpg://", "postgresql://")
        # ponytail: Windows 上 libpq 非阻塞连接默认无限等待 socket 事件，
        # connect_timeout 强制总超时，避免连接阶段永久挂起
        if "connect_timeout" not in dsn:
            dsn += "?connect_timeout=5" if "?" not in dsn else "&connect_timeout=5"
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg import AsyncConnection
        from psycopg.rows import dict_row

        conn = await AsyncConnection.connect(
            dsn, autocommit=True, prepare_threshold=0, row_factory=dict_row
        )
        checkpointer = AsyncPostgresSaver(conn=conn)
        # ponytail: 不调用 setup()，表结构已由 Alembic 迁移管理
        logger.info("PostgresSaver 已创建 (url=%s)", config.database.url)
        return checkpointer

    if checkpointer_type == "memory":
        from langgraph.checkpoint.memory import InMemorySaver

        logger.info("InMemorySaver 已创建")
        return InMemorySaver()

    raise ValueError(f"不支持的 checkpointer 类型: '{checkpointer_type}'，仅支持 'postgres' 或 'memory'")


async def dispose_checkpointer(checkpointer: BaseCheckpointSaver) -> None:
    """释放 checkpointer 持有的资源。

    输入:
        checkpointer: BaseCheckpointSaver — 要释放的 checkpointer 实例

    输出:
        None

    具体工作流:
        (1) 若为 AsyncPostgresSaver → 关闭底层 psycopg 连接
        (2) 若为 InMemorySaver → 无需操作
    """
    from langgraph.checkpoint.memory import InMemorySaver

    if isinstance(checkpointer, InMemorySaver):
        return

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    if isinstance(checkpointer, AsyncPostgresSaver):
        conn = getattr(checkpointer, "conn", None)
        if conn is not None:
            await conn.close()
            logger.info("PostgresSaver psycopg 连接已关闭")
