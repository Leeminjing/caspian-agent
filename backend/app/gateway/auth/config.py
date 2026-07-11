"""
本文件对外提供 `AuthConfig` Pydantic 配置模型，作为 web 层认证相关的独立配置，不纳入 agent 的 `AppConfig` 组合根。

对外提供:
    AuthConfig(BaseModel) — 认证配置数据模型

输入: config.yaml 中 `auth` 段的原始数据
输出: AuthConfig 实例

字段:
    jwt_secret: str                — JWT 签名密钥（支持 $ENV_VAR 环境变量引用）
    token_expiry_days: int         — JWT 过期天数，默认 7
    cookie_name: str               — access token Cookie 名称，默认 "access_token"
    csrf_cookie_name: str          — CSRF token Cookie 名称，默认 "csrf_token"
    login_limit_window: int        — 登录限流时间窗口秒数，默认 60
    login_limit_max_attempts: int  — 窗口内最大登录尝试次数，默认 5

示例:
    from backend.app.gateway.auth.config import AuthConfig

    cfg = AuthConfig(
        jwt_secret="your-secret-key",
        token_expiry_days=7,
    )
"""

from pydantic import BaseModel


class AuthConfig(BaseModel):
    jwt_secret: str
    token_expiry_days: int = 7
    cookie_name: str = "access_token"
    csrf_cookie_name: str = "csrf_token"
    login_limit_window: int = 60
    login_limit_max_attempts: int = 5
