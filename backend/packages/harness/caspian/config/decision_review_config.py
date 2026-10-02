"""本文件对外提供 DecisionReviewConfig，配置周期提醒及两阶段模型判断超时。

输入为 config.yaml 的 decision_review 段；输出为经过范围验证的提醒间隔、风险判断及完整复核超时。
工作流由模型边界按主 Agent 调用轮次提醒，enabled 仅控制该周期提醒；工具闸门对每次具体动作先做轻量判断。
示例：`DecisionReviewConfig(reminder_interval=5, risk_timeout_seconds=15, review_timeout_seconds=60)`。
"""

from pydantic import BaseModel, ConfigDict, Field


class DecisionReviewConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    reminder_interval: int = Field(default=5, ge=1)
    risk_timeout_seconds: int = Field(default=15, ge=1, le=120)
    review_timeout_seconds: int = Field(default=60, ge=1, le=300)
