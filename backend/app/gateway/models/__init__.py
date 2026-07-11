"""
本文件为 backend.app.gateway.models 包的入口，负责注册网关层 ORM 模型到 Base.metadata（供 Alembic autogenerate 发现）。
"""

# 导入 user ORM 模型以注册到 Base.metadata（供 Alembic autogenerate 发现）
import backend.app.gateway.models.user  # noqa: F401
