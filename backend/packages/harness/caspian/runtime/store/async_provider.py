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
    (2) 若 vector_enabled 为 true，用 OpenAIEmbeddings（OpenAI-compatible 端点）构造
        index 配置；构造失败直接抛出（无 index 时 store.search(query=...) 会静默退化
        为按时间排序，违反"绝不静默降级"约束）
    (3) async_postgres → 从 config.database.url 提取原始 DSN，直连 AsyncConnection 构造
        AsyncPostgresStore（from_conn_string 是 async generator，不适用于 lifespan
        长持有）并调用 setup()（官方迁移幂等，与 Alembic 已建表兼容）
    (4) postgres → 同上，创建 PostgresStore
    (5) memory → 创建 InMemoryStore
    (6) 返回 Store 实例

    dispose_store:
    (1) Postgres 后端 → 关闭 store.conn 连接
    (2) Memory 后端 → 无操作

示例:
    from caspian.runtime.store import create_store, dispose_store
    from caspian.config import get_app_config

    app_config = get_app_config("config.yaml")
    store = await create_store(app_config)
    # ... 使用 store ...
    await dispose_store(store)
"""

import logging
from urllib.parse import urlparse, urlunparse

from langgraph.store.base import BaseStore

from caspian.config.app_config import AppConfig

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
        # OpenAI-compatible 嵌入端点（langchain-openai 的 OpenAIEmbeddings）。
        # 构造失败直接抛出：无 index 时 store.search(query=...) 会静默退化为按
        # updated_at 排序的伪召回，与"绝不静默降级"的治理约束冲突。
        from langchain_openai import OpenAIEmbeddings

        embed_model = OpenAIEmbeddings(
            model=config.langgraph_store.embed,
            base_url=config.langgraph_store.embed_base_url,
            api_key=config.langgraph_store.embed_api_key,
            check_embedding_ctx_length=False,
        )
        index = {
            "embed": embed_model,
            "dims": config.langgraph_store.dims,
            "fields": list(config.langgraph_store.fields),
        }
        logger.info("向量搜索已启用 (embed=%s, dims=%d, fields=%s)",
                    config.langgraph_store.embed, config.langgraph_store.dims, config.langgraph_store.fields)

    if backend == "async_postgres":
        dsn = _dsn_from_sqlalchemy_url(config.database.url)
        from langgraph.store.postgres.aio import AsyncPostgresStore
        from psycopg import AsyncConnection
        from psycopg.rows import dict_row

        # from_conn_string 是 async generator（async with 语义），不适用于 lifespan
        # 长持有；改为直连构造，连接生命周期由 create_store/dispose_store 管理。
        conn = await AsyncConnection.connect(
            dsn, autocommit=True, prepare_threshold=0, row_factory=dict_row
        )
        store = AsyncPostgresStore(conn=conn, index=index)
        await store.setup()  # 官方迁移幂等（IF NOT EXISTS），与 Alembic 已建表兼容
        logger.info("AsyncPostgresStore 已创建 (dsn=%s, vector=%s)", dsn, vector_enabled)
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
    from langgraph.store.memory import InMemoryStore
    from langgraph.store.postgres import AsyncPostgresStore, PostgresStore

    if isinstance(store, InMemoryStore):
        return

    if isinstance(store, (AsyncPostgresStore, PostgresStore)):
        conn = getattr(store, "conn", None)
        if conn is not None and hasattr(conn, "close"):
            try:
                await conn.close()
                logger.info("PostgresStore 连接已关闭")
            except Exception:
                logger.error("关闭 PostgresStore 连接时出错", exc_info=True)
