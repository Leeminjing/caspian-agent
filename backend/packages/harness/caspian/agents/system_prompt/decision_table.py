"""
本文件对外提供 render_decision_table_section，把 authoritative DecisionTable 渲染为模型可见完整段落。

输入:
    table: DecisionTable — 已提交的决策等级表

输出:
    str — 带版本、表格、硬守卫摘要和冲突仲裁规则的 system 段落

具体工作流:
    先稳定渲染全部 rows，再渲染 priority-3 guards，最后附加等级仲裁规则；本模块不读写磁盘。

示例:
    section = render_decision_table_section(table)
"""

from caspian.agents.commitment.decision_table import DecisionTable


ARBITRATION_RULES = """<decision_table_instructions>
This is the thread's decision LEVEL TABLE (决策等级表). It contains human-approved decisions, and each decision carries a LEVEL (等级): 3=必须 must, 2=可协商 negotiable, 1=可选 optional. The LEVEL is the governing mechanism for decision conflicts — when decisions clash, the LEVEL decides which one wins.

Before proposing any new requirement or decision, scan ALL entries in this table for conflicts. Conflicts include semantic ones (wording changes, technology substitutions, and other surface-unrelated clashes) - not just exact text matches.

Conflict governance by LEVEL:
- New decision conflicts with an entry, and its level is LOWER than the entry's level → you MUST abandon the new decision and follow the existing entry. Do not execute, propose, or argue for it.
- New decision conflicts with an entry, and its level is EQUAL or HIGHER, or its level cannot be determined → you MUST stop and ask the user to confirm before proceeding.

LEVEL comparison is numeric: 3 > 2 > 1. Compare with code-like rigor, never guess the comparison result.
</decision_table_instructions>"""


def render_decision_table_section(table: DecisionTable) -> str:
    rows = ["| id | requirement | decision | priority |", "|---|---|---|---|"]
    rows.extend(
        f"| {row.id} | {row.requirement} | {row.decision} | {row.priority} |"
        for row in table.rows
    )
    guards = [
        f'- 条目 {row.id}（等级 {row.priority}）：{guard.kind} {guard.target} '
        f'{guard.operator} "{guard.pattern}"'
        for row in table.hard_entries()
        for guard in row.guards
    ]
    guard_text = "\n\n守卫规则：\n" + "\n".join(guards) if guards else ""
    return (
        f'<decision_table version="{table.version}" updated="{table.updated}">\n'
        f"{'\n'.join(rows)}{guard_text}\n"
        f"</decision_table>\n\n{ARBITRATION_RULES}"
    )
