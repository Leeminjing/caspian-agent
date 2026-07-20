"""
本文件对外提供 skill 工具白名单过滤策略函数。

对外提供:
    compute_allowed_tools — 基于已启用 Skill 集合，计算工具白名单

输入:
    skills: Iterable[Skill] — 已启用的 Skill 列表

输出:
    frozenset[str] | None — 允许的工具名集合，None 表示不限制(没有 Skill 声明 allowed_tools)

具体工作流:
    (1) 遍历所有 Skill，收集 allowed_tools 不为 None 的声明
    (2) 如果没有任何 Skill 声明 allowed_tools → 返回 None(不限制)
    (3) 如果至少一个 Skill 声明了 allowed_tools → 取所有声明的并集
    (4) 未声明 allowed_tools(None)的 Skill 不贡献工具名，也不扩大权限
    (5) 声明为 [] 的 Skill 触发白名单模式但不贡献工具名

示例:
    from lead_agent.skills.tool_policy import compute_allowed_tools

    skills = [
        Skill(name="a", allowed_tools=["read_file_tool", "bash_tool"]),
        Skill(name="b", allowed_tools=None),
        Skill(name="c", allowed_tools=["write_file_tool"]),
    ]
    result = compute_allowed_tools(skills)
    # → frozenset({"read_file_tool", "bash_tool", "write_file_tool"})
"""

from typing import Iterable

from lead_agent.skills.types import Skill


def compute_allowed_tools(skills: Iterable[Skill]) -> frozenset[str] | None:
    has_declaration = False
    union: set[str] = set()

    for skill in skills:
        if skill.allowed_tools is not None:
            has_declaration = True
            union.update(skill.allowed_tools)

    if not has_declaration:
        return None

    return frozenset(union)
