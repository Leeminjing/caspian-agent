"""
本文件为 backend.app.gateway.auth 包的入口，重导出认证配置与安全工具。

对外提供:
    AuthConfig — 认证配置 Pydantic 模型
    hash_password / verify_password — SHA-256 + bcrypt 密码哈希
    create_access_token / decode_access_token — JWT 签发与验证
    check_login_attempt_limit — 登录尝试限流（进程内滑动窗口）
    generate_csrf_token — CSRF token 生成
"""

from backend.app.gateway.auth.config import AuthConfig
from backend.app.gateway.auth.security import (
    check_login_attempt_limit,
    create_access_token,
    decode_access_token,
    generate_csrf_token,
    hash_password,
    verify_password,
)

__all__ = [
    "AuthConfig",
    "check_login_attempt_limit",
    "create_access_token",
    "decode_access_token",
    "generate_csrf_token",
    "hash_password",
    "verify_password",
]
