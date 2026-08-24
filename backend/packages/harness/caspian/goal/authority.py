"""
本文件对外提供目标工具的 authority 判定：direct-human vs goal-round。

输入:
    state: AgentState（图状态，含 messages） — 从 ToolRuntime.state 读取
    goal: GoalRecord | None — 当前目标（已由工具读取）

输出:
    GoalToolAuthority — {'kind': 'direct-human'} 或 {'kind': 'goal-round'}

工作流:
    (1) goal_round_marker(state) 反向扫描最后一条 HumanMessage，取 additional_kwargs.goal_round
    (2) is_direct_human(state) — 最后一条 HumanMessage 无 goal_round 标记
    (3) is_matching_goal_round(state, goal) — 标记与当前目标 id+revision+round 精确匹配
"""

from typing import Any

from langchain_core.messages import HumanMessage

from caspian.goal.domain import GoalRecord


def _as_messages(messages: Any) -> Any:
    """兼容传入图状态 dict（含 'messages' 键）或直接的消息列表。"""
    if isinstance(messages, dict):
        return messages.get("messages", [])
    return messages


def _last_human_message(messages: Any) -> HumanMessage | None:
    messages = _as_messages(messages)
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return message
    return None


def goal_round_marker(messages: Any) -> dict | None:
    """反向找最后一条 HumanMessage 的 goal_round 标记。"""
    human = _last_human_message(messages)
    if human is None:
        return None
    marker = getattr(human, "additional_kwargs", {}).get("goal_round")
    return marker if isinstance(marker, dict) else None


def is_direct_human(messages: Any) -> bool:
    """当前回合是否为直接人类回合（最后一条 HumanMessage 无 goal_round 标记）。"""
    return goal_round_marker(messages) is None


def is_matching_goal_round(messages: Any, goal: GoalRecord | None) -> bool:
    marker = goal_round_marker(messages)
    if marker is None or goal is None:
        return False
    try:
        return (
            str(marker.get("goal_id")) == goal.id
            and int(marker.get("revision")) == goal.revision
            and int(marker.get("round")) == goal.rounds_started
        )
    except (TypeError, ValueError):
        return False


def completion_authority(messages: Any, goal: GoalRecord | None) -> dict:
    """resolve complete/blocked 的 authority：direct-human 或精确 goal-round。"""
    if is_direct_human(messages):
        return {"kind": "direct-human"}
    if is_matching_goal_round(messages, goal):
        return {"kind": "goal-round"}
    return {"kind": "unknown"}
