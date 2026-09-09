"""
本文件为 backend.app.gateway.auth 包的公开导出。

对外提供:
    ensure_local_user / get_local_user / set_local_user — 本地单用户身份
"""

from backend.app.gateway.auth.local import (
    ensure_local_user,
    get_local_user,
    set_local_user,
)

__all__ = [
    "ensure_local_user",
    "get_local_user",
    "set_local_user",
]
