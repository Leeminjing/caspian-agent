"""
本文件对外提供认证安全相关工具函数，包括密码哈希、JWT 签发/验证、登录限流、CSRF token 生成。

对外提供:
    hash_password — SHA-256 prehash + bcrypt，带 $dfv2$ 版本前缀
    verify_password — 识别版本前缀并验证密码
    create_access_token — 签发 JWT access token
    decode_access_token — 验证并解码 JWT
    check_login_attempt_limit — 进程内滑动窗口登录尝试限流，IP 粒度
    generate_csrf_token — 生成随机 CSRF token

输入:
    hash_password: raw_password: str → str
    verify_password: raw_password: str, stored_hash: str → bool
    create_access_token: user (含 id, token_version), config: AuthConfig → str
    decode_access_token: token: str, config: AuthConfig → dict
    check_login_attempt_limit: ip: str, config: AuthConfig → bool
    generate_csrf_token: 无 → str

输出: 如上

具体工作流:
    hash_password:
    (1) 对 raw_password 做 SHA-256 得到 hex digest
    (2) 对 hex digest 做 bcrypt 哈希
    (3) 返回 "$dfv2$" + bcrypt 结果

    verify_password:
    (1) 检查 stored_hash 是否以 "$dfv2$" 开头，不是则返回 False
    (2) 去掉前缀得到 bcrypt 哈希
    (3) 对 raw_password 做 SHA-256
    (4) 用 bcrypt.checkpw 验证

    create_access_token:
    (1) 构造 payload: sub=str(user.id), ver=user.token_version, exp=now+days, iat=now
    (2) 用 config.jwt_secret 以 HS256 签发 JWT

    decode_access_token:
    (1) 用 config.jwt_secret 解码 JWT
    (2) 验证签名和 exp
    (3) 返回 payload dict；异常抛出 jwt.InvalidTokenError / jwt.ExpiredSignatureError

    check_login_attempt_limit:
    (1) 取 _rate_limit_store[ip] 的尝试时间戳列表
    (2) 剔除超过 config.login_limit_window 的旧记录
    (3) 若剩余记录数 >= config.login_limit_max_attempts → 返回 False
    (4) 否则追加当前时间戳 → 返回 True

    generate_csrf_token:
    (1) 用 secrets.token_urlsafe(32) 生成随机字符串

示例:
    from backend.app.gateway.auth.security import hash_password, verify_password

    hashed = hash_password("my-password")
    assert verify_password("my-password", hashed)
"""

import hashlib
import secrets
import time

import bcrypt
import jwt


# ---------------------------------------------------------------------------
# 密码哈希
# ---------------------------------------------------------------------------

def hash_password(raw_password: str) -> str:
    """SHA-256 prehash + bcrypt，带 $dfv2$ 版本前缀。

    输入:
        raw_password: str — 原始密码

    输出:
        str — "$dfv2$" + bcrypt 哈希结果

    工作流:
        (1) SHA-256(raw_password) → hex digest
        (2) bcrypt.hashpw(hex_digest, gensalt())
        (3) 拼接版本前缀 "$dfv2$" 返回
    """
    sha256_digest = hashlib.sha256(raw_password.encode()).hexdigest()
    bcrypt_hash = bcrypt.hashpw(sha256_digest.encode(), bcrypt.gensalt()).decode()
    return f"$dfv2${bcrypt_hash}"


def verify_password(raw_password: str, stored_hash: str) -> bool:
    """识别版本前缀并验证密码。

    输入:
        raw_password: str — 原始密码
        stored_hash: str — 存储的哈希值（可能带版本前缀）

    输出:
        bool — 密码是否匹配

    工作流:
        (1) 检查 "$dfv2$" 前缀，没有则返回 False
        (2) 去掉前缀得到 bcrypt 哈希
        (3) SHA-256(raw_password)
        (4) bcrypt.checkpw 验证
    """
    PREFIX = "$dfv2$"
    if not stored_hash.startswith(PREFIX):
        return False

    bcrypt_hash = stored_hash[len(PREFIX):]
    sha256_digest = hashlib.sha256(raw_password.encode()).hexdigest()
    return bcrypt.checkpw(sha256_digest.encode(), bcrypt_hash.encode())


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------

def create_access_token(user, config) -> str:
    """签发 JWT access token。

    输入:
        user — User ORM 实例（需有 id, token_version 属性）
        config: AuthConfig — 认证配置

    输出:
        str — JWT 字符串

    工作流:
        (1) 构造 payload: sub=str(user.id), ver=user.token_version, iat=now, exp=now+days
        (2) HS256 签发
    """
    now = int(time.time())
    payload = {
        "sub": str(user.id),
        "ver": user.token_version,
        "iat": now,
        "exp": now + config.token_expiry_days * 86400,
    }
    return jwt.encode(payload, config.jwt_secret, algorithm="HS256")


def decode_access_token(token: str, config) -> dict:
    """验证并解码 JWT access token。

    输入:
        token: str — JWT 字符串
        config: AuthConfig — 认证配置

    输出:
        dict — JWT payload (sub, ver, exp, iat)

    工作流:
        (1) jwt.decode(token, secret, algorithms=["HS256"])
        (2) 自动验证签名和 exp

    异常:
        jwt.ExpiredSignatureError — token 已过期
        jwt.InvalidTokenError — 签名无效或 token 格式错误
    """
    return jwt.decode(token, config.jwt_secret, algorithms=["HS256"])


# ---------------------------------------------------------------------------
# 登录限流
# ---------------------------------------------------------------------------

# 进程内限流存储: {ip: [attempt_timestamps]}
_rate_limit_store: dict[str, list[float]] = {}


def check_login_attempt_limit(ip: str, config) -> bool:
    """进程内滑动窗口登录尝试限流，IP 粒度。对所有登录尝试（含成功和失败）计数。

    输入:
        ip: str — 客户端 IP 地址
        config: AuthConfig — 认证配置

    输出:
        bool — True 表示允许尝试，False 表示被限流

    工作流:
        (1) 取 _rate_limit_store[ip] 的时间戳列表
        (2) 剔除超过 config.login_limit_window 秒的旧记录
        (3) 若剩余记录数 >= config.login_limit_max_attempts → 返回 False
        (4) 追加当前时间戳 → 返回 True
    """
    now = time.monotonic()
    attempts = _rate_limit_store.get(ip, [])
    # 剔除过期记录
    window = config.login_limit_window
    attempts = [t for t in attempts if now - t < window]
    if len(attempts) >= config.login_limit_max_attempts:
        _rate_limit_store[ip] = attempts  # ponytail: 保留已清理的列表，控制内存
        return False
    attempts.append(now)
    _rate_limit_store[ip] = attempts
    return True


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------

def generate_csrf_token() -> str:
    """生成随机 CSRF token。

    输入: 无

    输出:
        str — URL-safe 随机字符串（32 字节 base64 编码）

    工作流:
        (1) secrets.token_urlsafe(32)
    """
    return secrets.token_urlsafe(32)
