"""
本文件对外提供 SubagentsAppConfig Pydantic 配置模型与限额钳制函数。

对外提供:
    SubagentsAppConfig — config.yaml subagents 段的配置模型
    SubagentOverrideConfig — per-agent 覆盖配置模型
    CustomSubagentConfig — 用户自定义 subagent 类型配置模型
    clamp_subagent_concurrency — 将并发上限钳制到 [1, 4]
    clamp_total_subagents_per_run — 将单 run 委托总额钳制到 [1, 50]
    DEFAULT_MAX_TOTAL_SUBAGENTS_PER_RUN — 默认单 run 委托总额

输入: config.yaml 中 subagents 段的原始数据
输出: SubagentsAppConfig 实例，供 AppConfig 聚合与注册表解析使用

示例:
    cfg = SubagentsAppConfig(timeout_seconds=1800)
"""

from pydantic import BaseModel, Field

DEFAULT_MAX_TOTAL_SUBAGENTS_PER_RUN = 6
MIN_CONCURRENT_SUBAGENT_CALLS = 1
MAX_CONCURRENT_SUBAGENT_CALLS = 4
MIN_TOTAL_SUBAGENTS_PER_RUN = 1
MAX_TOTAL_SUBAGENTS_PER_RUN = 50


def clamp_subagent_concurrency(value: int) -> int:
    """将并发上限钳制到 [1, 4]，越界取边界值。"""
    return max(MIN_CONCURRENT_SUBAGENT_CALLS, min(MAX_CONCURRENT_SUBAGENT_CALLS, value))


def clamp_total_subagents_per_run(value: int) -> int:
    """将单 run 委托总额钳制到 [1, 50]，越界取边界值。"""
    return max(MIN_TOTAL_SUBAGENTS_PER_RUN, min(MAX_TOTAL_SUBAGENTS_PER_RUN, value))


class SubagentOverrideConfig(BaseModel):
    """per-agent 配置覆盖。"""

    timeout_seconds: int | None = Field(
        default=None,
        ge=1,
        description="该 subagent 的超时秒数（None = 用全局默认）",
    )
    max_turns: int | None = Field(
        default=None,
        ge=1,
        description="该 subagent 的最大轮数（None = 用全局或内置默认）",
    )
    model: str | None = Field(
        default=None,
        min_length=1,
        description="该 subagent 的模型名（None = 继承父 agent 模型）",
    )
    skills: list[str] | None = Field(
        default=None,
        description="该 subagent 的技能白名单（None = 继承全部 enabled 技能，[] = 禁用技能）",
    )


class CustomSubagentConfig(BaseModel):
    """用户在 config.yaml 中声明的自定义 subagent 类型。"""

    description: str = Field(
        description="lead agent 何时应委托给该 subagent 的说明",
    )
    system_prompt: str = Field(
        description="引导该 subagent 行为的系统提示词",
    )
    tools: list[str] | None = Field(
        default=None,
        description="工具名白名单（None = 继承父全部工具）",
    )
    disallowed_tools: list[str] | None = Field(
        default_factory=lambda: ["task", "ask_clarification", "present_files"],
        description="禁止的工具名列表",
    )
    skills: list[str] | None = Field(
        default=None,
        description="技能名白名单（None = 继承全部 enabled 技能，[] = 禁用技能）",
    )
    model: str = Field(
        default="inherit",
        description="使用的模型，'inherit' 表示继承父模型",
    )
    max_turns: int = Field(
        default=50,
        ge=1,
        description="最大 agent 轮数",
    )
    timeout_seconds: int = Field(
        default=900,
        ge=1,
        description="最大执行秒数",
    )


class SubagentsAppConfig(BaseModel):
    """config.yaml subagents 段的配置模型。"""

    enabled: bool = Field(
        default=True,
        description="是否启用 subagent 委托能力（关闭时 lead agent 不装配 task 工具与限制中间件）",
    )
    timeout_seconds: int = Field(
        default=1800,
        ge=1,
        description="内置 subagent 的默认超时秒数；自定义类型用自身值",
    )
    max_turns: int | None = Field(
        default=None,
        ge=1,
        description="可选：覆盖全部 subagent 的默认最大轮数（None = 保持内置默认）",
    )
    max_total_per_run: int = Field(
        default=DEFAULT_MAX_TOTAL_SUBAGENTS_PER_RUN,
        ge=MIN_TOTAL_SUBAGENTS_PER_RUN,
        le=MAX_TOTAL_SUBAGENTS_PER_RUN,
        description="单个 lead run 允许的 subagent 委托总数（1-50）",
    )
    agents: dict[str, SubagentOverrideConfig] = Field(
        default_factory=dict,
        description="按 agent 名称的 per-agent 覆盖",
    )
    custom_agents: dict[str, CustomSubagentConfig] = Field(
        default_factory=dict,
        description="按 agent 名称的用户自定义 subagent 类型",
    )

    def get_timeout_for(self, agent_name: str) -> int:
        """解析某 agent 的生效超时：per-agent 覆盖优先，否则全局默认。"""
        override = self.agents.get(agent_name)
        if override is not None and override.timeout_seconds is not None:
            return override.timeout_seconds
        return self.timeout_seconds

    def get_max_turns_for(self, agent_name: str) -> int | None:
        """解析某 agent 的生效最大轮数：per-agent 覆盖优先，否则全局默认。"""
        override = self.agents.get(agent_name)
        if override is not None and override.max_turns is not None:
            return override.max_turns
        return self.max_turns

    def get_model_for(self, agent_name: str) -> str | None:
        """解析某 agent 的模型覆盖：仅 per-agent 覆盖（无全局默认）。"""
        override = self.agents.get(agent_name)
        if override is not None and override.model is not None:
            return override.model
        return None

    def get_skills_for(self, agent_name: str) -> list[str] | None:
        """解析某 agent 的技能白名单覆盖：仅 per-agent 覆盖。"""
        override = self.agents.get(agent_name)
        if override is not None and override.skills is not None:
            return override.skills
        return None
