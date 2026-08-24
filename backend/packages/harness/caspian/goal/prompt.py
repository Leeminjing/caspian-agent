"""
本文件对外提供 render_goal_round_prompt：一次目标续跑回合注入模型的 <goal_round> 提示。

输入:
    objective: str — 目标对象
    round_no: int — 当前续跑回合号
    max_goal_rounds: int — 总回合上限

输出:
    str — <goal_round> 提示文本
"""


def render_goal_round_prompt(objective: str, round_no: int, max_goal_rounds: int) -> str:
    return (
        "<goal_round>\n"
        f"Objective: {objective}\n"
        f"Round: {round_no}/{max_goal_rounds}\n\n"
        "Continue working toward the objective in this same session. Treat the current workspace, "
        "tool results, and durable session state as authoritative; inspect them instead of assuming "
        "earlier narration is still current. Make concrete progress and verify the result. Before "
        "claiming completion, gather evidence that the whole objective is achieved, read the current "
        "goal, and mark it complete. If work remains, leave the goal active for the next round. Follow "
        "the configured goal-tool policy before reporting a blocker.\n"
        "</goal_round>"
    )
