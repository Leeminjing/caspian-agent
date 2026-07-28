"""
本文件定义 DatabaseConfig Pydantic 配置模型。

对外提供:
    DatabaseConfig(BaseModel) — 数据库连接配置的数据模型

输入: config.yaml 中 database 段的原始数据
输出: DatabaseConfig 实例

字段:
    backend: str          — 数据库后端标识（如 "postgres"、"sqlite"）
    url: str              — 数据库连接 URL（格式 dialect+driver://user:pass@host:port/db）
    echo: bool            — 是否打印 SQL 日志，默认 False
    pool_size: int        — 连接池保留连接数，默认 5
    max_overflow: int     — 超出 pool_size 后允许的额外连接数，默认 10
    pool_timeout: int     — 等待连接池可用连接的超时秒数，默认 30
    pool_pre_ping: bool   — 取连接前先测试连接是否存活，默认 True
    pool_recycle: int     — 连接使用多少秒后回收重建，默认 -1（不主动回收）
    isolation_level: str  — 事务隔离级别，默认 "READ COMMITTED"

示例:
    from focus.config.database_config import DatabaseConfig

    cfg = DatabaseConfig(
        backend="postgres",
        url="postgresql+asyncpg://user:pass@localhost:5432/db",
    )
"""

from pydantic import BaseModel


class DatabaseConfig(BaseModel):
    backend: str
    url: str
    echo: bool = False
    pool_size: int = 5
    max_overflow: int = 10
    pool_timeout: int = 30
    pool_pre_ping: bool = True
    pool_recycle: int = -1
    isolation_level: str = "READ COMMITTED"
