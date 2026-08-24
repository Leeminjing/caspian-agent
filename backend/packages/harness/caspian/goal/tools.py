"""
本文件对外提供 build_goal_tools 工厂：注册模型可见的 get_goal / create_goal / update_goal 工具。

输入:
    config: GoalModeConfig — 目标模式配置（阈值）

输出:
    list[BaseTool] — 三个目标工具

工作流:
    (1) 工具经 ToolRuntime 现取 store/config/context，构造 GoalService
    (2) get_goal 只读；create_goal / update_goal(edit|pause|resume) 需直接人类回合
    (3) update_goal(complete|blocked) 允许直接人类或精确 goal 回合；blocked（goal 回合）需达阈值
    (4) goal 回合 complete/blocked 返回含收尾指令的结果文本
"""

import json
from typing import Annotated, Any

from langchain.tools import InjectedToolCallId, tool
from langgraph.prebuilt import ToolRuntime

from caspian.goal.authority import (
    completion_authority,
    is_direct_human,
)
from caspian.goal.domain import (
    GOAL_INVALID_BLOCK_REASON,
    GOAL_TOOL_AUTHORITY_REQUIRED,
    GOAL_TOOL_BLOCK_THRESHOLD,
    GoalError,
    GoalRecord,
)
from caspian.goal.service import GoalService
from caspian.goal.wrapup import render_wrapup_context

UPDATE_ACTIONS = ("edit", "pause", "resume", "complete", "blocked")


def _goal_view(record: GoalRecord | None) -> dict:
    if record is None:
        return {"goal": None}
    view: dict[str, Any] = {
        "id": record.id,
        "revision": record.revision,
        "objective": record.objective,
        "phase": record.phase,
        "rounds_started": record.rounds_started,
        "max_goal_rounds": record.max_goal_rounds,
    }
    if record.blocked_reason is not None:
        view["blocked_reason"] = record.blocked_reason.to_dict()
    return {"goal": view, "activation": "armed" if record.armed else "disarmed"}


def _service(runtime: ToolRuntime) -> GoalService:
    store = runtime.store
    if store is None:
        raise GoalError("目标模式需要 LangGraph store", GOAL_TOOL_AUTHORITY_REQUIRED)
    config_dict = runtime.config or {}
    thread_id = (config_dict.get("configurable") or {}).get("thread_id")
    context = runtime.context if isinstance(runtime.context, dict) else {}
    user_id = context.get("user_id")
    if not user_id or not thread_id:
        raise GoalError("目标工具缺少 user_id 或 thread_id", GOAL_TOOL_AUTHORITY_REQUIRED)
    return GoalService(store=store, user_id=str(user_id), thread_id=str(thread_id))


def _blocked_threshold(blocked_after_consecutive_rounds: int) -> int:
    return blocked_after_consecutive_rounds


def build_goal_tools(blocked_after_consecutive_rounds: int) -> list:
    """工厂：返回 [get_goal, create_goal, update_goal]。"""
    threshold = _blocked_threshold(blocked_after_consecutive_rounds)

    @tool("get_goal", parse_docstring=True)
    async def get_goal(runtime: ToolRuntime) -> str:
        """Read the current same-session goal, including its exact id/revision, objective, phase,
        completed continuation rounds, round limit, blocker reason when present, and whether another
        continuation is armed. Call this before updating a goal."""
        record = await _service(runtime).get()
        return json.dumps(_goal_view(record), ensure_ascii=False)

    @tool("create_goal", parse_docstring=True)
    async def create_goal(
        objective: str,
        runtime: ToolRuntime,
    ) -> str:
        """Create one persisted same-session completion goal when the current direct human request is
        a long-running objective that should continue across autonomous goal rounds. You may infer that
        intent without requiring the user to say 'create a goal'. Do not use this for trivial
        single-turn work. Execution rejects non-human and subagent authority.

        Args:
            objective: The concrete completion objective inferred from the direct human request.
        """
        if not is_direct_human(runtime.state if runtime.state is not None else {}):
            raise GoalError("create_goal requires a direct human turn", GOAL_TOOL_AUTHORITY_REQUIRED)
        service = _service(runtime)
        record = await service.create(objective)
        return json.dumps(_goal_view(record), ensure_ascii=False)

    @tool("update_goal", parse_docstring=True)
    async def update_goal(
        goal_id: str,
        revision: int,
        action: str,
        runtime: ToolRuntime,
        objective: Annotated[str | None, "Replacement objective; valid only with action edit."] = None,
        max_goal_rounds: Annotated[int | None, "Replacement cap; valid only with action edit."] = None,
        blocked_reason: Annotated[str | None, "Concrete blocking condition; required only with action blocked."] = None,
        tool_call_id: Annotated[str, InjectedToolCallId] = None,
    ) -> str:
        """Update the exact current goal revision. edit, pause, and resume require a direct top-level
        human request. During an automatic continuation of the current goal, complete and blocked are
        also allowed. blocked is rejected before the configured minimum round count; report the concrete
        condition in blocked_reason.

        Args:
            goal_id: Exact id returned by get_goal.
            revision: Exact positive revision returned by get_goal.
            action: edit | pause | resume | complete | blocked.
            objective: Replacement objective; valid only with action edit.
            max_goal_rounds: Replacement cap; valid only with action edit.
            blocked_reason: Concrete blocking condition; required only with action blocked.
        """
        if action not in UPDATE_ACTIONS:
            raise GoalError(f"action 只允许 {UPDATE_ACTIONS}", GOAL_TOOL_AUTHORITY_REQUIRED)
        state = runtime.state if runtime.state is not None else {}
        service = _service(runtime)
        ref = {"id": goal_id, "revision": revision}

        if action in ("edit", "pause", "resume"):
            if not is_direct_human(state):
                raise GoalError(f"update_goal {action} requires a direct human turn", GOAL_TOOL_AUTHORITY_REQUIRED)
            if action == "edit":
                record = await service.edit(ref, objective=objective, max_goal_rounds=max_goal_rounds)
                return json.dumps(_goal_view(record), ensure_ascii=False)
            record = await service.pause(ref) if action == "pause" else await service.resume(ref)
            return json.dumps(_goal_view(record), ensure_ascii=False)

        # complete / blocked
        goal = await service.get()
        authority = completion_authority(state, goal)
        if authority["kind"] == "unknown":
            raise GoalError(
                "complete and blocked require a direct human turn or the current goal round",
                GOAL_TOOL_AUTHORITY_REQUIRED,
            )
        if action == "complete":
            record = await service.complete(ref)
            result = json.dumps(_goal_view(record), ensure_ascii=False)
            if authority["kind"] == "goal-round":
                result = f"{result}\n\n{render_wrapup_context(record.objective)}"
            return result
        # blocked
        if not blocked_reason or not blocked_reason.strip():
            raise GoalError("blocked_reason 在 action=blocked 时必填", GOAL_INVALID_BLOCK_REASON)
        if authority["kind"] == "goal-round" and goal is not None and goal.rounds_started < threshold:
            raise GoalError(
                f"blocked requires at least {threshold} consecutive goal rounds; current round is {goal.rounds_started}",
                GOAL_TOOL_BLOCK_THRESHOLD,
            )
        record = await service.block(ref, code="model-reported", message=blocked_reason.strip())
        result = json.dumps(_goal_view(record), ensure_ascii=False)
        if authority["kind"] == "goal-round":
            result = f"{result}\n\n{render_wrapup_context(record.objective, blocked_reason.strip())}"
        return result

    return [get_goal, create_goal, update_goal]
