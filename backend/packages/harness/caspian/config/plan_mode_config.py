"""
本文件对外提供 PlanModeConfig，声明计划模式的启停开关与策略段文本。

输入:
    config.yaml 中 plan_mode 段

输出:
    PlanModeConfig — 供 lead agent 装配层判断是否注册 PlanModeMiddleware 与 exit_plan_mode 工具

工作流:
    (1) enabled 为开关；section 为计划模式激活时注入模型系统提示词的策略段文本
    (2) enabled=True 时 section 必填非空，否则配置解析失败（load 即报错，不静默忽略）
"""

from pydantic import BaseModel, ConfigDict, model_validator


class PlanModeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    section: str = ""

    @model_validator(mode="after")
    def _validate_section(self) -> "PlanModeConfig":
        if self.enabled and not self.section.strip():
            raise ValueError("PlanModeConfig: `section` 必填且非空（enabled=True 时）")
        return self
