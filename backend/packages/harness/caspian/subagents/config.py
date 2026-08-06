"""
本文件对外提供 SubagentConfig 数据类、内置 subagent 注册表常量与模型名解析函数。

对外提供:
    SubagentConfig — 单个 subagent 类型的运行时配置
    BUILTIN_SUBAGENTS — 内置 subagent 类型注册表（general-purpose / bash）
    resolve_subagent_model_name — 解析生效模型名（显式指定 > 继承父模型 > 默认模型）

输入:
    name / description / system_prompt / tools / disallowed_tools / skills / model / max_turns / timeout_seconds

输出:
    SubagentConfig 实例；内置类型字典；解析后的模型名字符串

具体工作流:
    (1) SubagentConfig 为纯 dataclass，由 registry 或内置表构造
    (2) BUILTIN_SUBAGENTS 定义内置类型默认值（general-purpose=150 turns，bash=60 turns）
    (3) resolve_subagent_model_name 按优先级解析模型名，均不可用时回退 config.yaml 默认模型

示例:
    config = SubagentConfig(name="my-agent", description="...")
    model_name = resolve_subagent_model_name(config, parent_model="deepseek-v4-flash")
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from caspian.config.app_config import AppConfig


@dataclass
class SubagentConfig:
    """单个 subagent 类型的运行时配置。"""

    name: str
    description: str
    system_prompt: str | None = None
    tools: list[str] | None = None
    disallowed_tools: list[str] | None = field(default_factory=lambda: ["task"])
    skills: list[str] | None = None
    model: str = "inherit"
    max_turns: int = 50
    timeout_seconds: int = 900


BUILTIN_SUBAGENTS: dict[str, SubagentConfig] = {
    "general-purpose": SubagentConfig(
        name="general-purpose",
        description="通用推理与执行 agent，适合多步推理、web 检索、文件操作等有界子任务。",
        max_turns=150,
        timeout_seconds=900,
    ),
    "bash": SubagentConfig(
        name="bash",
        description="沙箱内命令行执行专家，适合脚本、数据处理、文件转换与环境搭建。",
        max_turns=60,
        timeout_seconds=900,
    ),
}


def _default_model_name(app_config: "AppConfig") -> str:
    if not app_config.models:
        raise ValueError("AppConfig.models 为空，无法获取默认模型")
    return app_config.models[0].name


def resolve_subagent_model_name(
    config: SubagentConfig,
    parent_model: str | None,
    *,
    app_config: "AppConfig | None" = None,
) -> str:
    """解析 subagent 的生效模型名。

    输入:
        config: SubagentConfig — 含 model 字段（"inherit" 表示继承）
        parent_model: str | None — 父 lead agent 的模型名
        app_config: AppConfig | None — 用于默认模型回退

    输出:
        str — 生效模型名

    工作流:
        (1) config.model 非 "inherit" → 直接返回
        (2) parent_model 非 None → 返回父模型
        (3) 否则回退 config.yaml models[0]
    """
    if config.model != "inherit":
        return config.model

    if parent_model is not None:
        return parent_model

    if app_config is None:
        from caspian.config import get_app_config

        app_config = get_app_config("config.yaml")
    return _default_model_name(app_config)
