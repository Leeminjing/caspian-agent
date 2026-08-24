"""
本文件对外提供 goal_guidance：模型可见的目标工具策略段文本（镜像 deepseek 的 tool:goal section）。

输入:
    blocked_after_consecutive_rounds: int — blocked 阈值（goal 回合）

输出:
    str — 策略段文本，供 make_lead_agent 追加到 system_prompt
"""


def goal_guidance(blocked_after_consecutive_rounds: int) -> str:
    return (
        "Use goal tools for one long-running completion objective in the current session. "
        "create_goal may infer goal intent from a direct human request in any language; do not "
        "create a goal for routine single-turn work. Call get_goal before update_goal and copy its "
        "exact goal_id and revision. After a new run starts, an existing active goal is disarmed: "
        "when a human asks to continue or resume in any wording or language, use update_goal action "
        "resume to rearm it (during a direct human turn). Mark complete only when the objective is "
        "actually achieved. Mark "
        + f"blocked only after the same blocking condition persists for at least {blocked_after_consecutive_rounds} "
        + "consecutive goal rounds, and report that concrete condition in blocked_reason; difficulty, "
        "uncertainty, or useful remaining work is not blocked. edit, pause, and resume require a direct "
        "human turn; during an automatic continuation round only complete and blocked are available."
    )
