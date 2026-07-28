"""
本文件对外提供 SkillCatalog 只读检索目录，作为已启用 Skill 的内存索引。

对外提供:
    SkillCatalog — 不可变(frozen)数据类，持有 Skill 元组，提供 names 属性和 search 方法

输入:
    构造 SkillCatalog(skills: Iterable[Skill]) — 传入 Skill 可迭代对象

输出:
    SkillCatalog 实例:
        names: frozenset[str] — 所有 Skill 名称，用于注入 <skill_index>
        search(query: str) -> list[Skill] — 按名称和描述匹配，用于 describe_skill_tool

具体工作流:
    names:
    (1) 遍历 self.skills 收集所有 name 字段
    (2) 返回 frozenset，保证不可变性

    search:
    (1) 将 query 转为小写
    (2) 匹配 name 精确相等 → 最高优先级
    (3) 匹配 description 包含 query(大小写不敏感) → 次优先级
    (4) 返回匹配的 Skill 列表，name 精确匹配排最前

示例:
    from focus.skills.catalog import SkillCatalog
    from focus.skills.types import Skill, SkillCategory

    skills = [Skill(name="pdf", description="PDF tools", ...), ...]
    catalog = SkillCatalog(skills)

    catalog.names  # frozenset({"pdf", "code-review", "openspec-explore"})
    catalog.search("pdf")  # [Skill(name="pdf", ...)]
"""

from dataclasses import dataclass
from typing import Iterable

from focus.skills.types import Skill


@dataclass(frozen=True)
class SkillCatalog:
    skills: tuple[Skill, ...]

    def __init__(self, skills: Iterable[Skill] = ()) -> None:
        # frozen dataclass 不允许直接设属性,用 object.__setattr__ 绕过
        object.__setattr__(self, "skills", tuple(skills))

    @property
    def names(self) -> frozenset[str]:
        return frozenset(s.name for s in self.skills)

    def search(self, query: str) -> list[Skill]:
        q = query.strip().lower()
        if not q:
            return list(self.skills)

        exact: list[Skill] = []
        fuzzy: list[Skill] = []

        for s in self.skills:
            if s.name.lower() == q:
                exact.append(s)
            elif q in s.description.lower():
                fuzzy.append(s)

        return exact + fuzzy
