"""
本文件对外提供 SKILL.md 解析函数，将磁盘上的 SKILL.md 文件转换为 Skill 对象。

对外提供:
    parse_skill_file — 读取单个 SKILL.md，解析 YAML frontmatter，构造并返回 Skill 对象
    parse_allowed_tools — 解析 allowed-tools 字段，返回 list[str] | None

输入:
    parse_skill_file:
        skill_file: Path — SKILL.md 文件路径
        category: SkillCategory — skill 类别(public / custom)
        relative_path: Path | None — 从类别根目录算的相对路径，None 时从 skill_file 父目录名推导

    parse_allowed_tools:
        raw: Any — YAML 解析后的 allowed-tools 原始值
        skill_file: str — skill 文件名(仅用于日志)

输出:
    parse_skill_file → Skill | None — 解析成功返回 Skill，校验失败返回 None
    parse_allowed_tools → list[str] | None — 解析成功返回工具名列表或 None

具体工作流:
    parse_skill_file:
    (1) 读取 SKILL.md 文件内容
    (2) 分离 YAML frontmatter(--- 包裹) 和正文
    (3) YAML 解析 frontmatter
    (4) 调用 _validate_skill_frontmatter 校验
    (5) 提取 name、description、license、allowed-tools
    (6) 构造 Skill 对象并返回
    (7) 任何步骤失败返回 None

    parse_allowed_tools:
    (1) raw 为 None → 返回 None
    (2) raw 为 list → 逐项校验为 str，返回 list[str]
    (3) 其他类型 → 抛 ValueError

错误处理: 校验失败统一返回 None 不抛异常，严重错误(parse_allowed_tools 内部抛 ValueError
被 parse_skill_file 捕获转 None)记录日志。

示例:
    from pathlib import Path
    from focus.skills.parser import parse_skill_file
    from focus.skills.types import SkillCategory

    skill = parse_skill_file(
        Path("/skills/public/pdf/SKILL.md"),
        SkillCategory.PUBLIC,
    )
    # → Skill(name="pdf", description="PDF tools", ...) 或 None
"""

import logging
from pathlib import Path

import yaml

from focus.skills.types import Skill, SkillCategory
from focus.skills.validation import _validate_skill_frontmatter

logger = logging.getLogger(__name__)


def parse_allowed_tools(raw, skill_file: str) -> list[str] | None:
    if raw is None:
        return None

    if isinstance(raw, list):
        result: list[str] = []
        for i, item in enumerate(raw):
            if not isinstance(item, str):
                raise ValueError(
                    f"allowed-tools[{i}] must be a string, got {type(item).__name__}"
                )
            result.append(item)
        return result

    raise ValueError(
        f"allowed-tools must be a list or null, got {type(raw).__name__}"
    )


def parse_skill_file(
    skill_file: Path,
    category: SkillCategory,
    relative_path: Path | None = None,
) -> Skill | None:
    try:
        with open(skill_file, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        logger.error("读取 SKILL.md 失败: %s", skill_file, exc_info=True)
        return None

    if not content.startswith("---"):
        return None

    parts = content.split("---", 2)
    if len(parts) < 3:
        return None

    frontmatter_raw = parts[1].strip()
    try:
        frontmatter = yaml.safe_load(frontmatter_raw)
    except yaml.YAMLError:
        logger.error("SKILL.md YAML 解析失败: %s", skill_file, exc_info=True)
        return None

    if not isinstance(frontmatter, dict):
        return None

    ok, err = _validate_skill_frontmatter(frontmatter)
    if not ok:
        if "must" in err:  # 严重错误: name/description 格式问题
            logger.error("SKILL.md 校验失败: %s — %s", skill_file, err)
        return None

    name: str = frontmatter["name"]
    description: str = frontmatter["description"]

    raw_license = frontmatter.get("license")
    license_val: str | None = raw_license if isinstance(raw_license, str) else None

    try:
        allowed_tools = parse_allowed_tools(
            frontmatter.get("allowed-tools"), str(skill_file)
        )
    except ValueError as e:
        logger.error("SKILL.md allowed-tools 解析失败: %s — %s", skill_file, e)
        return None

    if relative_path is None:
        relative_path = Path(skill_file.parent.name)

    return Skill(
        name=name,
        description=description,
        license=license_val,
        skill_dir=skill_file.parent,
        skill_file=skill_file,
        relative_path=relative_path,
        category=category,
        allowed_tools=allowed_tools,
        enabled=False,  # 启停状态由 extensions_config.json 决定,此处默认 False
    )
