"""
本文件对外提供 render_wrapup_context：目标在 goal 回合 complete/blocked 时的收尾指令文本。

输入:
    objective: str — 目标对象
    blocked_reason: str | None — blocked 时的人类可读说明（complete 时省略）

输出:
    str — <goal_complete> 或 <goal_blocked> 收尾指令文本
"""

_GROUNDING = (
    "Report only what earlier rounds and tool results in this session actually establish; "
    "when a detail is not in the session, say so instead of inventing it. "
)


def render_wrapup_context(objective: str, blocked_reason: str | None = None) -> str:
    heading = f"Objective: {objective}\n"
    if blocked_reason is None:
        return (
            "<goal_complete>\n"
            f"{heading}"
            "The goal is marked complete and this autonomous run is ending. Write the closing "
            "message to the user now: state the outcome, summarize what was done and how it was "
            "verified, and point to the concrete results (files, commits, or other artifacts). "
            f"{_GROUNDING}"
            "Note anything the user should review or do next. Address the user directly. Do not "
            "call any more tools in this run; further work waits for the user's next instruction.\n"
            "</goal_complete>"
        )
    return (
        "<goal_blocked>\n"
        f"{heading}"
        f"Blocked: {blocked_reason}\n"
        "The goal is marked blocked and this autonomous run is ending. Write the closing "
        "message to the user now: state what has been completed so far, describe the concrete "
        "blocking condition and what you tried, and say exactly what you need from the user to "
        "continue. "
        f"{_GROUNDING}"
        "Address the user directly. Do not call any more tools in this run; further work "
        "waits for the user's next instruction.\n"
        "</goal_blocked>"
    )
