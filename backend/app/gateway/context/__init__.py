"""
本文件为 backend.app.gateway.context 包的入口，负责注册 Context 派生相关的网关层 ORM 模型到
Base.metadata（供 Alembic autogenerate 发现），并重导出投影编译入口。

对外提供:
    compile_context_messages — 投影编译器入口（经 projection 子模块重导出）
    validate_messages / deserialize_messages — 消息结构校验与反序列化（经 validation 子模块重导出）
"""

import backend.app.gateway.context.models  # noqa: F401
from backend.app.gateway.context.projection import compile_context_messages  # noqa: F401
from backend.app.gateway.context.validation import (  # noqa: F401
    deserialize_messages,
    validate_messages,
)
