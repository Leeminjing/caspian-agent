"""本文件对外提供 DecisionReviewConfig，配置周期提醒和动作前复核类别。

输入为 config.yaml 的 decision_review 段；输出为经过范围验证的提醒间隔与工具集合。
工作流由模型边界按调用轮次读取间隔，由工具边界按工具名决定是否暂停复核。
示例：`DecisionReviewConfig(reminder_interval=5, critical_tools=["bash_tool"])`。
"""

from pydantic import BaseModel, Field


class DecisionReviewConfig(BaseModel):
    enabled: bool = True
    reminder_interval: int = Field(default=5, ge=1)
    critical_tools: list[str] = Field(default_factory=lambda: [
        "bash_tool", "powershell_tool", "cmd_tool", "sh_tool",
        "write_file_tool", "task_tool", "task",
    ])
