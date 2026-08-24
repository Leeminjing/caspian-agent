"""
本文件对外提供 GoalModeMiddleware：目标模式的人类命令拦截（/goal 前缀）与（可选）策略段注入。

输入:
    config: GoalModeConfig — 目标模式配置
    skill_names: frozenset[str] | None — 当前用户 enabled skill 名称集合（用于剥离 /goal 前导 skill token）

输出:
    before_agent / abefore_agent → dict | None（拦截 /goal 命令时返回 {"messages": [...]} 状态更新）

工作流:
    (1) before_agent: 取最后一条 HumanMessage，剥离前导 skill token 后匹配 /goal 命令
        - /goal                          → 查看
        - /goal <objective>              → 创建
        - /goal edit <objective>         → 编辑
        - /goal pause / resume / clear   → 暂停 / 恢复 / 清除
        - 其他                           → 不拦截（返回 None）
    (2) 命中时经 GoalService（runtime.store + thread_id + user_id）执行，并把触发消息替换为确认/结果

示例:
    middleware = GoalModeMiddleware(app_config.goal_mode)
"""

import asyncio
import re
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import AgentState
from langchain.messages import HumanMessage

from caspian.config.goal_mode_config import GoalModeConfig
from caspian.goal.domain import GoalError
from caspian.goal.service import GoalService

_USAGE = "Usage: /goal [<objective>|clear|edit <objective>|pause|resume]"


def _strip_leading_skill_tokens(text: str, skill_names: frozenset[str] | None) -> str:
    if not skill_names:
        return text
    rest = text
    while True:
        match = re.match(r"/(\S+)(?:\s+|$)", rest)
        if not match or match.group(1) not in skill_names:
            return rest
        rest = rest[match.end():].lstrip()


def _parse_goal_command(text: str, skill_names: frozenset[str] | None = None) -> tuple[str, str | None] | None:
    """识别 /goal 命令形态，返回 (action, arg) 或 None。"""
    stripped = _strip_leading_skill_tokens(text, skill_names).strip()
    if not stripped.startswith("/goal"):
        return None
    body = stripped[len("/goal"):].strip()
    if not body:
        return ("show", None)
    control = body.lower()
    if control == "pause":
        return ("pause", None)
    if control == "resume":
        return ("resume", None)
    if control == "clear":
        return ("clear", None)
    if control == "edit":
        return ("invalid-edit", None)
    if re.match(r"^edit\s", body, re.IGNORECASE):
        return ("edit", body[4:].strip())
    return ("create", body)


def _goal_service(runtime: Any, config: GoalModeConfig) -> GoalService:
    store = getattr(runtime, "store", None)
    if store is None:
        raise GoalError("目标模式需要 LangGraph store", "GOAL_TOOL_AUTHORITY_REQUIRED")
    # thread_id：中间件 Runtime 走 execution_info.thread_id（镜像 CommitmentMiddleware）；
    # 回退 config.configurable.thread_id（工具/测试桩）。
    exec_info = getattr(runtime, "execution_info", None)
    thread_id = getattr(exec_info, "thread_id", None)
    if thread_id is None:
        config_dict = getattr(runtime, "config", None) or {}
        thread_id = (config_dict.get("configurable") or {}).get("thread_id")
    context = getattr(runtime, "context", None)
    if not isinstance(context, dict):
        context = {}
    user_id = context.get("user_id")
    if not user_id or not thread_id:
        raise GoalError("目标命令缺少 user_id 或 thread_id", "GOAL_TOOL_AUTHORITY_REQUIRED")
    return GoalService(store=store, user_id=str(user_id), thread_id=str(thread_id), default_max_goal_rounds=config.default_max_goal_rounds)


def _render_goal_summary(goal) -> str:
    if goal is None:
        return "No goal is currently set."
    reason = goal.blocked_reason.to_dict() if goal.blocked_reason is not None else None
    blocker = f"\nBlocker: {reason['code']}: {reason['message']}" if reason else ""
    state = "active" if goal.armed else goal.phase
    return (
        f"Status: {state}"
        f"{blocker}"
        f"\nObjective: {goal.objective}"
        f"\nRounds: {goal.rounds_started}/{goal.max_goal_rounds}"
        f"\nActivation: {'armed' if goal.armed else 'disarmed'}"
        f"\n\nCommands: {_USAGE}"
    )


class GoalModeMiddleware(AgentMiddleware):
    """目标模式中间件：拦截 /goal 命令。未启用时不装配（由 make_lead_agent 判断）。"""

    def __init__(self, config: GoalModeConfig, skill_names: frozenset[str] | None = None) -> None:
        super().__init__()
        self.config = config
        self._enabled_skill_names = skill_names if skill_names is not None else frozenset()

    async def _before_agent(self, state: AgentState, runtime: Any) -> dict[str, Any] | None:
        messages = state.get("messages", [])
        if not messages:
            return None
        trigger = messages[-1]
        if not isinstance(trigger, HumanMessage) or not isinstance(trigger.content, str):
            return None
        command = _parse_goal_command(trigger.content.strip(), self._enabled_skill_names)
        if command is None:
            return None
        action, arg = command
        service = _goal_service(runtime, self.config)
        current = await service.get()
        files = trigger.additional_kwargs.get("files")

        try:
            if action == "show":
                return self._replace(trigger, _render_goal_summary(current), files)
            if action == "clear":
                if current is None:
                    text = "No goal to clear."
                else:
                    await service.clear({"id": current.id, "revision": current.revision})
                    text = "Goal cleared."
                return self._replace(trigger, text, files=None)
            if action == "pause":
                if current is None:
                    return self._replace(trigger, "No goal is currently set.", files=None)
                await service.pause({"id": current.id, "revision": current.revision})
                return self._replace(trigger, self._result("Goal paused", await service.get()), files=None)
            if action == "resume":
                if current is None:
                    return self._replace(trigger, "No goal is currently set.", files=None)
                await service.resume({"id": current.id, "revision": current.revision})
                return self._replace(trigger, self._result("Goal resumed", await service.get()), files=None)
            if action in ("invalid-edit",):
                return self._replace(trigger, "Goal editing requires a replacement objective.", files=None)
            if action == "edit":
                if current is None:
                    return self._replace(trigger, f"No goal is currently set; /goal edit requires one.", files=None)
                if not arg:
                    return self._replace(trigger, "Goal editing requires a replacement objective.", files=None)
                await service.edit({"id": current.id, "revision": current.revision}, objective=arg)
                return self._replace(trigger, self._result("Goal updated", await service.get()), files)
            # create
            if current is not None and current.phase != "complete":
                return self._replace(
                    trigger,
                    f"A goal is already {current.phase}. Use /goal edit <objective> or /goal clear before replacing.",
                    files=None,
                )
            record = await service.create(arg or "")
            return self._replace(trigger, self._result("Goal created", record), files)
        except GoalError as exc:
            return self._replace(trigger, f"Goal command is not valid for the current state: {exc.code}", files=None)

    @staticmethod
    def _replace(trigger: HumanMessage, content: str, files: Any) -> dict[str, Any]:
        kwargs = {"id": trigger.id}
        if files is not None:
            kwargs["additional_kwargs"] = {"files": files}
        return {"messages": [HumanMessage(content=content, **kwargs)]}

    @staticmethod
    def _result(action_label: str, goal) -> str:
        """渲染 /goal 动作结果：动作词 + 完整摘要（Status/Objective/Rounds/Activation/Commands）。"""
        return f"{action_label}\n{_render_goal_summary(goal)}"

    def before_agent(self, state: AgentState, runtime: Any) -> dict[str, Any] | None:
        return asyncio.run(self._before_agent(state, runtime))

    async def abefore_agent(self, state: AgentState, runtime: Any) -> dict[str, Any] | None:
        return await self._before_agent(state, runtime)
