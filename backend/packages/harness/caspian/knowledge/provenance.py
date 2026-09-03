"""
本文件对外提供 classify_level 纯函数与 extract_domain 辅助函数，作为知识等级来源派生的唯一入口。

对外提供:
    extract_domain(source_url) — 从 URL 提取小写域名（host），缺失或不可解析时返回 None
    classify_level(source_url, domains) — 依据域名→等级策略表确定性派生离散权威等级

输入:
    extract_domain:
        source_url: str | None — 来源链接
    classify_level:
        source_url: str | None — 来源链接
        domains: Mapping[str, int] — 域名→等级策略表（3=官方/2=官方博客/1=社区/0=低质量）

输出:
    extract_domain → str | None — 小写 host；缺失/不可解析返回 None
    classify_level → tuple[int | None, str, str | None] — (level, source_type, matched_domain)；
        level=None 表示"未评级"，source_type="unknown" 表示未命中策略表

具体工作流:
    (1) extract_domain 用 urlparse 取 hostname 并转小写
    (2) classify_level 调 extract_domain 取域名；域名为空 → 未评级
    (3) 域名不在 domains 中 → 未评级（source_type=unknown，matched_domain=该域名）
    (4) 域名命中且等级合法(0-3) → 返回 (level, 等级标签, 域名)
    (5) 域名命中但等级非法 → 按未评级处理

示例:
    classify_level("https://docs.example.com/x", {"docs.example.com": 3})
    → (3, "official", "docs.example.com")
    classify_level("https://blog.example.com/x", {"docs.example.com": 3})
    → (None, "unknown", "blog.example.com")
    classify_level(None, {"docs.example.com": 3})
    → (None, "unknown", None)
"""

from collections.abc import Mapping
from urllib.parse import urlparse

_LEVEL_SOURCE_TYPES: dict[int, str] = {
    3: "official",
    2: "official_blog",
    1: "community",
    0: "low_quality",
}

_VALID_LEVELS = frozenset(_LEVEL_SOURCE_TYPES)


def extract_domain(source_url: str | None) -> str | None:
    if not source_url:
        return None
    try:
        host = urlparse(str(source_url)).hostname
    except ValueError:
        return None
    return host.lower() if host else None


def classify_level(
    source_url: str | None,
    domains: Mapping[str, int],
) -> tuple[int | None, str, str | None]:
    domain = extract_domain(source_url)
    if domain is None:
        return None, "unknown", None
    if domain not in domains:
        return None, "unknown", domain
    level = domains[domain]
    if level not in _VALID_LEVELS:
        return None, "unknown", domain
    return level, _LEVEL_SOURCE_TYPES[level], domain
