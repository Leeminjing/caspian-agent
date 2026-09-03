"""
本文件对外提供 config_router，作为「打开配置文件」功能的后端配置查看与编辑端点。

对外提供:
    config_router: APIRouter — 已注册 GET /api/config 配置查看路由与 PUT /api/config 配置保存路由的 FastAPI Router，供 app 挂载

输入:
    GET /api/config 请求，经 AuthMiddleware 校验 JWT Cookie 后进入本端点
    PUT /api/config 请求，经 AuthMiddleware（JWT）+ CSRFMiddleware（X-CSRF-Token）双重校验后进入本端点

输出:
    GET → PlainTextResponse — 仓库根 config.yaml 的脱敏后文本；文件缺失时返回 404
    PUT → JSONResponse — 保存结果；YAML 非法时返回 400

具体工作流:
    GET:
    (1) 通过 _load_config_text() 读取仓库根 config.yaml 原始文本
    (2) 通过 _redact_config() 对敏感字段脱敏（API 密钥、database.url 凭据段、JWT 密钥等）
    (3) 以 PlainTextResponse 返回脱敏文本

    PUT:
    (1) 读取 request.body() 的提交文本
    (2) 通过 _merge_redacted_submit() 将敏感键隔离回写为磁盘原值（不落 ***）
    (3) 通过 _validate_yaml() 校验合并后 YAML 合法；非法返回 400
    (4) 写入磁盘；成功后调用 reload_app_config() 刷新 AppConfig 单例
    (5) 返回保存结果

示例:
    GET /api/config
    => text/plain: "models:\n  - name: deepseek-v4-flash\n    api_key: ***\n..."
"""

import re
from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from caspian.config.app_config import reload_app_config

router = APIRouter(prefix="/api")

# 仓库根目录（routers/ -> gateway -> app -> backend -> 仓库根）
_PROJECT_ROOT = Path(__file__).resolve().parents[4]
_CONFIG_PATH = _PROJECT_ROOT / "config.yaml"

# 敏感键子串（大小写不敏感：命中即掩码该键的值）
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(?:key|secret|token|password|jwt|credential|api_key)", re.IGNORECASE
)
# 匹配 YAML 行首的 key: value 形式（key 可为带 . 的点分路径，如 database.url）
_YAML_KV_PATTERN = re.compile(r"^(\s*[\w.\-]+\s*:\s*)(.*)$")
# 匹配 URL 中的 user:pass@ 凭据段（如 postgresql://caspian:pass@host/db）
_URL_CREDENTIAL_PATTERN = re.compile(r"(://[^:/@\s]+):([^@/\s]+)@")
# 匹配 $ENV 占位符形式的机密值
_ENV_VAR_PATTERN = re.compile(r"^\$[A-Za-z_][A-Za-z0-9_]*$")

_MASK = "***"

# 非机密值形态：整数、布尔、null、空值、数值型 YAML
_NON_SECRET_VALUE_PATTERN = re.compile(
    r"^(?:-?\d+(?:\.\d+)?|true|false|null|~|)\s*$", re.IGNORECASE
)


def _mask_url_credentials(value: str) -> str:
    """将 URL 字符串中的 user:pass@ 凭据段掩码为 user:***@。

    输入:
        value: str — 可能含凭据段的 URL 字符串

    输出:
        str — 凭据段被掩码后的 URL；不含凭据段时原样返回
    """
    return _URL_CREDENTIAL_PATTERN.sub(r"\1:" + _MASK + "@", value)


def _redact_scalar_value(key: str, raw_value: str) -> str:
    """脱敏单个 key 的标量值。

    输入:
        key: str — 配置键（可能为点分路径，如 database.url）
        raw_value: str — 原始值文本

    输出:
        str — 脱敏后的值文本；非机密值保持原样
    """
    stripped_value = raw_value.strip()

    # $ENV 占位符一律掩码
    if _ENV_VAR_PATTERN.match(stripped_value):
        return _MASK

    # 命中敏感键（含点分路径的末段）且值不是纯数值/布尔/null → 掩码
    # 收窄为排除非机密值，避免把 token_expiry_days / trigger_tokens 这类数值配置误掩码
    if _SENSITIVE_KEY_PATTERN.search(key) and not _NON_SECRET_VALUE_PATTERN.match(
        stripped_value
    ):
        return _MASK

    # database.url 这类包含凭据段的连接串 → 掩码 user:pass@ 段
    if "://" in stripped_value and "@" in stripped_value:
        return _mask_url_credentials(stripped_value)

    return raw_value


def _redact_config(text: str) -> str:
    """对 config.yaml 原始文本做行级敏感字段脱敏（受保护 helper）。

    输入:
        text: str — config.yaml 原始文本

    输出:
        str — 脱敏后的文本，非敏感行保持原样
    """
    lines = []
    for line in text.splitlines():
        match = _YAML_KV_PATTERN.match(line)
        if match is None:
            lines.append(line)
            continue
        indent_key, raw_value = match.group(1), match.group(2)
        # 键名从缩进前缀提取（'database.url:' -> 'database.url'）
        key_name = indent_key.rstrip(": ").strip()
        redacted_value = _redact_scalar_value(key_name, raw_value)
        lines.append(f"{indent_key}{redacted_value}")
    return "\n".join(lines)


def _load_config_text() -> str:
    """读取仓库根 config.yaml 的原始文本。

    输入: 无

    输出:
        str — config.yaml 原始文本

    异常:
        HTTPException(404) — 文件不存在时抛出
    """
    if not _CONFIG_PATH.exists():
        raise HTTPException(status_code=404, detail="config.yaml 不存在")
    return _CONFIG_PATH.read_text(encoding="utf-8")


def _is_redaction_placeholder(value: str) -> bool:
    """判断一个值是否为脱敏占位符（受保护 helper）。

    输入:
        value: str — 值文本

    输出:
        bool — 为 *** / $ENV 占位符 / 含掩码凭据段的 URL 时返回 True
    """
    stripped = value.strip()
    if stripped == _MASK:
        return True
    if _ENV_VAR_PATTERN.match(stripped):
        return True
    # URL 连接串中凭据段被掩码（如 :***@ 或 user:***@）也视为脱敏占位符
    if "://" in stripped and _MASK in stripped:
        return True
    return False


def _build_disk_key_value_map(disk_text: str) -> dict[str, str]:
    """把磁盘 config.yaml 逐行解析为 {key: 原始值文本} 映射（受保护 helper）。

    输入:
        disk_text: str — 磁盘 config.yaml 原始文本

    输出:
        dict[str, str] — 每个带 key 的行的真实值文本；同 key 后者覆盖前者
    """
    result: dict[str, str] = {}
    for line in disk_text.splitlines():
        match = _YAML_KV_PATTERN.match(line)
        if match is None:
            continue
        key = match.group(1).rstrip(": ").strip()
        result[key] = match.group(2)
    return result


def _merge_redacted_submit(disk_text: str, submitted_text: str) -> str:
    """把用户提交的编辑文本与磁盘真实配置隔离合并（受保护 helper）。

    对提交文本中命中敏感键的行，若其值为脱敏占位符，则回写为磁盘真实值；
    用户对非敏感行的编辑被保留。此函数保证不把 *** / $ENV 占位符写回磁盘。

    输入:
        disk_text: str — 磁盘 config.yaml 原始文本（真实值的权威来源）
        submitted_text: str — 用户从脱敏界面提交的编辑文本

    输出:
        str — 合并后的 config.yaml 文本（敏感键已还原为磁盘真实值）
    """
    disk_map = _build_disk_key_value_map(disk_text)
    merged_lines = []
    for line in submitted_text.splitlines():
        match = _YAML_KV_PATTERN.match(line)
        if match is None:
            # 注释/空行/非 key 行：保留提交内容
            merged_lines.append(line)
            continue
        indent_key, submitted_value = match.group(1), match.group(2)
        key = indent_key.rstrip(": ").strip()

        # 还原条件：值为脱敏占位符（*** / $ENV / 含掩码凭据段的 URL），且磁盘存在该 key 的真实值
        if _is_redaction_placeholder(submitted_value.strip()):
            real_value = disk_map.get(key)
            if real_value is not None:
                merged_lines.append(f"{indent_key}{real_value}")
                continue
        # 若非占位符，或磁盘无对应 key（用户新增）→ 保留提交值
        merged_lines.append(line)
    return "\n".join(merged_lines) + ("\n" if submitted_text.endswith("\n") else "")


def _validate_yaml(text: str) -> None:
    """校验合并后的 config.yaml 文本是否为合法 YAML（受保护 helper）。

    输入:
        text: str — 待校验文本

    输出:
        None — 合法时通过

    异常:
        HTTPException(400) — YAML 非法时抛出
    """
    try:
        yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=400, detail=f"config.yaml 格式非法: {exc}") from exc


def _write_config_text(text: str) -> None:
    """写入仓库根 config.yaml（受保护 helper）。

    输入:
        text: str — 合并后的 config.yaml 文本

    输出:
        None
    """
    _CONFIG_PATH.write_text(text, encoding="utf-8")


@router.get("/config", response_class=PlainTextResponse)
async def read_config() -> PlainTextResponse:
    """GET /api/config — 返回仓库根 config.yaml 的脱敏后文本。

    输入: 无（鉴权由 AuthMiddleware 在 /api/ 前缀统一拦截）

    输出:
        PlainTextResponse — 脱敏后的 config.yaml 文本

    工作流:
        (1) 读取 config.yaml 原始文本
        (2) 对敏感键与 URL 凭据段脱敏
        (3) 返回脱敏文本
    """
    text = _load_config_text()
    return PlainTextResponse(_redact_config(text))


@router.put("/config")
async def update_config(request: Request) -> JSONResponse:
    """PUT /api/config — 保存用户编辑后的 config.yaml（敏感键隔离回写 + 热生效）。

    输入:
        request: Request — 请求体为编辑后的 config.yaml 文本（text/plain）
        （鉴权：AuthMiddleware 要求 JWT Cookie；CSRFMiddleware 要求 X-CSRF-Token 与 cookie 一致）

    输出:
        JSONResponse — {"ok": true, "detail": "...生效说明..."}

    异常:
        HTTPException(400) — YAML 非法或读取/合并失败

    工作流:
        (1) 读取提交文本（await request.body()）
        (2) 读取磁盘真实 config.yaml
        (3) 将敏感键隔离回写为磁盘原值（_merge_redacted_submit）
        (4) 校验合并后 YAML 合法（_validate_yaml）
        (5) 写入磁盘并调用 reload_app_config() 热生效
        (6) 返回保存结果
    """
    submitted_bytes = await request.body()
    submitted_text = submitted_bytes.decode("utf-8")
    try:
        disk_text = _load_config_text()
    except HTTPException:
        raise

    merged_text = _merge_redacted_submit(disk_text, submitted_text)
    _validate_yaml(merged_text)
    _write_config_text(merged_text)

    # 热生效：刷新 AppConfig 单例，使新配置对后续 run 生效
    try:
        reload_app_config(str(_CONFIG_PATH))
    except Exception as exc:
        return JSONResponse(
            status_code=200,
            content={"ok": True, "detail": f"配置已保存；运行期热加载警告: {exc}"},
        )

    return JSONResponse(
        status_code=200,
        content={"ok": True, "detail": "配置已保存，将在下一次 run 生效。"},
    )
