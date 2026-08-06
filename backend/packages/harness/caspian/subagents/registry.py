"""
本文件对外提供 subagent 注册表解析函数：内置类型、自定义类型与 per-agent 覆盖的统一入口。

对外提供:
    get_subagent_config — 按名称解析生效的 SubagentConfig（含覆盖应用）
    get_available_subagent_names — 列出全部可用类型名（内置 + 自定义）

输入:
    name: str — subagent 类型名
    app_config: AppConfig | None — 配置对象，None 时自动加载 config.yaml

输出:
    SubagentConfig | None — 解析结果；未注册类型返回 None
    list[str] — 可用类型名列表

具体工作流:
    (1) 查内置表 BUILTIN_SUBAGENTS，未命中则查 config.yaml custom_agents
    (2) 应用 per-agent 覆盖（timeout_seconds / max_turns / model / skills）
    (3) 全局默认（timeout_seconds / max_turns）只作用于内置类型，不覆盖自定义自身值
    (4) 返回生效配置

示例:
    config = get_subagent_config("general-purpose")
    names = get_available_subagent_names()
"""

import logging
from dataclasses import replace
from typing import Any

from caspian.subagents.config import BUILTIN_SUBAGENTS, SubagentConfig

logger = logging.getLogger(__name__)


def _resolve_subagents_config(app_config: Any | None = None):
    if app_config is None:
        from caspian.config.subagents_config import SubagentsAppConfig

        return SubagentsAppConfig()
    return getattr(app_config, "subagents", app_config)


def _build_custom_subagent_config(name: str, *, app_config: Any | None = None) -> SubagentConfig | None:
    """从 config.yaml custom_agents 段构造 SubagentConfig。"""
    subagents_config = _resolve_subagents_config(app_config)
    custom = getattr(subagents_config, "custom_agents", {}).get(name)
    if custom is None:
        return None

    return SubagentConfig(
        name=name,
        description=custom.description,
        system_prompt=custom.system_prompt,
        tools=custom.tools,
        disallowed_tools=custom.disallowed_tools,
        skills=custom.skills,
        model=custom.model,
        max_turns=custom.max_turns,
        timeout_seconds=custom.timeout_seconds,
    )


def get_subagent_config(name: str, *, app_config: Any | None = None) -> SubagentConfig | None:
    """按名称解析生效的 SubagentConfig（内置 → custom_agents → per-agent 覆盖）。"""
    # (1) 内置优先，未命中回退 custom_agents
    config = BUILTIN_SUBAGENTS.get(name)
    if config is None:
        config = _build_custom_subagent_config(name, app_config=app_config)
    if config is None:
        return None

    # (2) per-agent 覆盖
    subagents_config = _resolve_subagents_config(app_config)
    is_builtin = name in BUILTIN_SUBAGENTS
    agent_override = getattr(subagents_config, "agents", {}).get(name)

    overrides: dict = {}

    # timeout: per-agent 覆盖 > 全局默认（仅内置）> 自身值
    if agent_override is not None and agent_override.timeout_seconds is not None:
        if agent_override.timeout_seconds != config.timeout_seconds:
            overrides["timeout_seconds"] = agent_override.timeout_seconds
    elif is_builtin and subagents_config.timeout_seconds != config.timeout_seconds:
        overrides["timeout_seconds"] = subagents_config.timeout_seconds

    # max_turns: per-agent 覆盖 > 全局默认（仅内置）> 自身值
    global_max_turns = getattr(subagents_config, "max_turns", None)
    if agent_override is not None and agent_override.max_turns is not None:
        if agent_override.max_turns != config.max_turns:
            overrides["max_turns"] = agent_override.max_turns
    elif is_builtin and global_max_turns is not None and global_max_turns != config.max_turns:
        overrides["max_turns"] = global_max_turns

    # model: 仅 per-agent 覆盖
    effective_model = subagents_config.get_model_for(name)
    if effective_model is not None and effective_model != config.model:
        overrides["model"] = effective_model

    # skills: 仅 per-agent 覆盖
    effective_skills = subagents_config.get_skills_for(name)
    if effective_skills is not None and effective_skills != config.skills:
        overrides["skills"] = effective_skills

    if overrides:
        config = replace(config, **overrides)

    return config


def get_available_subagent_names(app_config: Any | None = None) -> list[str]:
    """列出全部可用 subagent 类型名（内置 + config.yaml 自定义）。"""
    subagents_config = _resolve_subagents_config(app_config)
    custom_names = list(getattr(subagents_config, "custom_agents", {}).keys())
    return sorted([*BUILTIN_SUBAGENTS.keys(), *custom_names])
