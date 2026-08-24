"""
本文件对外提供 GoalModeConfig，声明目标模式的启停开关与两个策略阈值。

输入:
    config.yaml 中 goal_mode 段

输出:
    GoalModeConfig — 供 lead agent 装配层判断是否装配目标中间件、注册目标工具、接入目标驱动

工作流:
    (1) enabled 为开关；default_max_goal_rounds 为 create 省略 round 上限时采用的默认值
    (2) blocked_after_consecutive_rounds 为 goal 回合下模型标记 blocked 所需的最小连续回合数
    (3) 两个阈值必须为正整数，否则配置解析失败（load 即报错，不静默忽略）
"""

from pydantic import BaseModel, ConfigDict, model_validator


class GoalModeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    default_max_goal_rounds: int = 256
    blocked_after_consecutive_rounds: int = 3

    @model_validator(mode="after")
    def _validate_thresholds(self) -> "GoalModeConfig":
        if not isinstance(self.default_max_goal_rounds, int) or self.default_max_goal_rounds < 1:
            raise ValueError("GoalModeConfig: `default_max_goal_rounds` 必须为正整数")
        if not isinstance(self.blocked_after_consecutive_rounds, int) or self.blocked_after_consecutive_rounds < 1:
            raise ValueError("GoalModeConfig: `blocked_after_consecutive_rounds` 必须为正整数")
        return self
