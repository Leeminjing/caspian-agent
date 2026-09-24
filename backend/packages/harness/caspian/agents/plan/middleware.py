"""
本文件对外提供 PlanModeMiddleware，作为计划模式命令与状态的 AgentMiddleware 入口。

输入:
    config: PlanModeConfig — 计划模式配置（enabled / section 策略段文本）
    skill_names: frozenset[str] | None — 当前用户 enabled skill 名称集合（用于剥离 /plan 前导 skill token）

输出:
    before_agent / abefore_agent → dict | None（拦截 /plan 命令时返回 {"plan_active", "messages"} 状态更新）

工作流:
    (1) before_agent: 取最后一条 HumanMessage，剥离前导 skill token（/name）后匹配 /plan 命令
        - /plan       → plan_active=True，触发消息替换为进入通知
        - /plan <msg> → plan_active=True，触发消息替换为普通用户消息 <msg>
        - /plan off   → plan_active=False，触发消息替换为退出通知；携带图片附件时拒绝（不改状态）
        - 其他        → 不拦截（返回 None）
    (2) plan policy 由 SystemSnapshotBuilder 根据 plan_active 合成；本中间件不再修改模型请求

示例:
    middleware = PlanModeMiddleware(app_config.plan_mode)
"""

import asyncio
import re
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import AgentState
from langchain.messages import HumanMessage

from caspian.config.plan_mode_config import PlanModeConfig


def _strip_leading_skill_tokens(text: str, skill_names: frozenset[str] | None) -> str:
    """剥离文本前导的 /<skill-name> token 序列，返回剩余文本（受保护 helper）。

    输入:
        text: str — 去除外围空白后的消息文本
        skill_names: frozenset[str] | None — 当前用户 enabled 技能名集合，None/空则不剥离

    输出:
        str — 剥离前导 skill token 后的文本；无 token 时原样返回
    """
    if not skill_names:
        return text
    rest = text
    while True:
        match = re.match(r"/(\S+)(?:\s+|$)", rest)
        if not match or match.group(1) not in skill_names:
            return rest
        rest = rest[match.end():].lstrip()


def _match_plan_command(
    text: str, skill_names: frozenset[str] | None
) -> tuple[str, str | None] | None:
    """识别 /plan 命令形态，返回 (action, message) 或 None。

    输入:
        text: str — 去除外围空白后的消息文本
        skill_names: frozenset[str] | None — 前导 skill token 集合

    输出:
        tuple[str, str | None] | None — ("on", None) 裸 /plan；( "on", <msg>) 带消息；
        ("off", None) /plan off；非命令返回 None
    """
    stripped = _strip_leading_skill_tokens(text, skill_names).strip()
    if re.fullmatch(r"/plan", stripped):
        return ("on", None)
    if re.fullmatch(r"/plan off", stripped):
        return ("off", None)
    match = re.match(r"/plan\s+(.+)", stripped, re.DOTALL)
    if match and match.group(1).strip():
        return ("on", match.group(1).strip())
    return None


class PlanModeMiddleware(AgentMiddleware):
    """计划模式中间件：只拦截 /plan 命令并维护 plan_active。"""

    def __init__(
        self, config: PlanModeConfig, skill_names: frozenset[str] | None = None
    ) -> None:
        super().__init__()
        self.config = config
        self._enabled_skill_names = skill_names if skill_names is not None else frozenset()

    async def _before_agent(
        self, state: AgentState, runtime: Any
    ) -> dict[str, Any] | None:
        messages = state.get("messages", [])
        if not messages:
            return None
        trigger = messages[-1]
        if not isinstance(trigger, HumanMessage) or not isinstance(trigger.content, str):
            return None
        command = _match_plan_command(
            trigger.content.strip(),
            self._enabled_skill_names,
        )
        if command is None:
            return None
        action, message = command
        if action == "off":
            if trigger.additional_kwargs.get("files"):
                return {
                    "messages": [
                        HumanMessage(
                            content="Image attachments cannot accompany /plan off.",
                            id=trigger.id,
                        )
                    ]
                }
            return {
                "plan_active": False,
                "messages": [
                    HumanMessage(content="Plan mode off.", id=trigger.id)
                ],
            }
        if message is not None:
            return {
                "plan_active": True,
                "messages": [HumanMessage(content=message, id=trigger.id)],
            }
        return {
            "plan_active": True,
            "messages": [
                HumanMessage(
                    content="Plan mode on. Use /plan off to leave.", id=trigger.id
                )
            ],
        }

    def before_agent(self, state: AgentState, runtime: Any) -> dict[str, Any] | None:
        return asyncio.run(self._before_agent(state, runtime))

    async def abefore_agent(
        self, state: AgentState, runtime: Any
    ) -> dict[str, Any] | None:
        return await self._before_agent(state, runtime)
