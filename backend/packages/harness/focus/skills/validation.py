"""
本文件对外提供 _validate_skill_frontmatter 函数，对 SKILL.md 解析出的 frontmatter 字典进行字段级校验。

对外提供:
    _validate_skill_frontmatter — 校验 frontmatter 字典，返回 (is_valid, error_message) 元组

输入:
    frontmatter: dict — 从 SKILL.md YAML frontmatter 解析出的字典

输出:
    tuple[bool, str] — (是否通过校验, 错误信息) 通过时为 (True, "")

具体工作流:
    (1) 校验 frontmatter 必须是字典
    (2) 校验字段白名单(name/description/license/allowed-tools/metadata/compatibility/version/author)
    (3) 校验 name 必填、非空、hyphen-case、不超过 64 字符
    (4) 校验 description 必填、不含尖括号、不超过 1024 字符
    (5) allowed-tools 的具体格式由 parser.parse_allowed_tools 负责，此处不做深度校验

示例:
    from focus.skills.validation import _validate_skill_frontmatter

    ok, err = _validate_skill_frontmatter({"name": "my-skill", "description": "A skill"})
    # → (True, "")

    ok, err = _validate_skill_frontmatter({"name": "bad name!", "description": ""})
    # → (False, "name must be hyphen-case...")
"""

import logging
import re

logger = logging.getLogger(__name__)

_ALLOWED_FIELDS: frozenset[str] = frozenset({
    "name",
    "description",
    "license",
    "allowed-tools",
    "metadata",
    "compatibility",
    "version",
    "author",
})

_NAME_PATTERN: re.Pattern = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_MAX_NAME_LENGTH = 64
_MAX_DESC_LENGTH = 1024


def _validate_skill_frontmatter(frontmatter: dict) -> tuple[bool, str]:
    if not isinstance(frontmatter, dict):
        return False, "frontmatter must be a YAML dictionary"

    unknown = set(frontmatter.keys()) - _ALLOWED_FIELDS
    if unknown:
        return False, f"unknown fields: {sorted(unknown)}"

    name = frontmatter.get("name")
    if name is None:
        return False, "name is required"
    if not isinstance(name, str):
        return False, "name must be a string"
    if not name.strip():
        return False, "name must not be empty"
    if len(name) > _MAX_NAME_LENGTH:
        return False, f"name must not exceed {_MAX_NAME_LENGTH} characters"
    if not _NAME_PATTERN.match(name):
        return False, "name must be hyphen-case (lowercase letters, digits, and hyphens only)"

    description = frontmatter.get("description")
    if description is None:
        return False, "description is required"
    if not isinstance(description, str):
        return False, "description must be a string"
    if "<" in description or ">" in description:
        return False, "description must not contain angle brackets (< or >)"
    if len(description) > _MAX_DESC_LENGTH:
        return False, f"description must not exceed {_MAX_DESC_LENGTH} characters"

    return True, ""
