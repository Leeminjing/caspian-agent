"""
本文件对外提供 create_store、dispose_store 两个异步函数，作为 Store 的工厂与资源释放入口。

对外提供:
    create_store(config) — 基于 AppConfig 创建 BaseStore 实例的异步工厂函数
    dispose_store(store) — 释放 store 持有的资源

输入:
    create_store:
        config: AppConfig — 应用配置对象，从 config.langgraph_store 提取 Store 配置
        config.database.url — 数据库连接 URL（postgres 后端时使用）

    dispose_store:
        store: BaseStore — 要释放的 Store 实例

输出:
    create_store → BaseStore 实例（AsyncPostgresStore / PostgresStore / InMemoryStore）
    dispose_store → None

具体工作流:
    create_store:
    (1) 读取 config.langgraph_store.backend 选择实现类型
    (2) async_postgres → 从 config.database.url 提取原始 DSN，创建 AsyncPostgresStore
    (3) postgres → 同上，创建 PostgresStore
    (4) memory → 创建 InMemoryStore
    (5) 若 vector_enabled 为 true，构造 index 配置并传入
    (6) 返回 Store 实例

    dispose_store:
    (1) Postgres 后端 → 关闭连接池
    (2) Memory 后端 → 无操作

示例:
    from focus.runtime.store import create_store, dispose_store
    from focus.config import get_app_config

    app_config = get_app_config("config.yaml")
    store = await create_store(app_config)
    # ... 使用 store ...
    await dispose_store(store)
"""

import logging
from urllib.parse import urlparse, urlunparse

from langgraph.store.base import BaseStore

from focus.config.app_config import AppConfig

logger = logging.getLogger(__name__)


def _dsn_from_sqlalchemy_url(url: str) -> str:
    """从 SQLAlchemy URL（dialect+driver://...）提取原始 PostgreSQL DSN。

    输入:
        url: str — SQLAlchemy 连接 URL，如 postgresql+asyncpg://user:pass@host:port/db

    输出:
        str — PostgreSQL DSN，如 postgresql://user:pass@host:port/db

    工作流:
        (1) 解析 URL
        (2) 去掉 +driver 部分，将 scheme 替换为 postgresql
        (3) 重组并返回
    """
    parsed = urlparse(url)
    # 去掉 dialect+driver 前缀，保留 postgresql
    scheme = "postgresql"
    netloc = parsed.netloc
    path = parsed.path
    return urlunparse((scheme, netloc, path, "", "", ""))


async def create_store(config: AppConfig) -> BaseStore:
    backend = config.langgraph_store.backend
    vector_enabled = config.langgraph_store.vector_enabled

    index = None
    if vector_enabled:
        try:
            from langchain.embeddings import init_embeddings

            embed_model = init_embeddings(config.langgraph_store.embed)
            index = {
                "embed": embed_model,
                "dims": config.langgraph_store.dims,
                "fields": list(config.langgraph_store.fields),
            }
            logger.info("向量搜索已启用 (embed=%s, dims=%d, fields=%s)",
                        config.langgraph_store.embed, config.langgraph_store.dims, config.langgraph_store.fields)
        except Exception:
            logger.warning("嵌入模型 '%s' 初始化失败，向量搜索已关闭", config.langgraph_store.embed, exc_info=True)

    if backend == "async_postgres":
        dsn = _dsn_from_sqlalchemy_url(config.database.url)
        from langgraph.store.postgres import AsyncPostgresStore

        store = AsyncPostgresStore.from_conn_string(dsn, index=index)
        logger.info("AsyncPostgresStore 已创建 (dsn=%s)", dsn)
        return store

    if backend == "postgres":
        dsn = _dsn_from_sqlalchemy_url(config.database.url)
        from langgraph.store.postgres import PostgresStore

        store = PostgresStore.from_conn_string(dsn, index=index)
        logger.info("PostgresStore 已创建 (dsn=%s)", dsn)
        return store

    if backend == "memory":
        from langgraph.store.memory import InMemoryStore

        kwargs = {}
        if index is not None:
            kwargs["index"] = index
        store = InMemoryStore(**kwargs)
        logger.info("InMemoryStore 已创建")
        return store

    raise ValueError(f"不支持的 Store 后端: '{backend}'，仅支持 'async_postgres'、'postgres'、'memory'")


async def dispose_store(store: BaseStore) -> None:
    from langgraph.store.postgres import AsyncPostgresStore, PostgresStore

    if isinstance(store, (AsyncPostgresStore, PostgresStore)):
        try:
            # 关闭底层连接池或连接
            pool = getattr(store, "_pool", None)
            if pool is not None and hasattr(pool, "close"):
                await pool.close()
                logger.info("PostgresStore 连接池已关闭")
            else:
                conn = getattr(store, "_conn", None)
                if conn is not None and hasattr(conn, "close"):
                    await conn.close()
                    logger.info("PostgresStore 连接已关闭")
        except Exception:
            logger.error("关闭 PostgresStore 资源时出错", exc_info=True)
    else:
        # InMemoryStore 无需释放
        pass
