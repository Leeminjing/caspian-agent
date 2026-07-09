"""
本文件定义 CheckpointerConfig Pydantic 配置模型。

对外提供:
    CheckpointerConfig(BaseModel) — checkpointer 配置段的数据模型

输入: config.yaml 中 checkpointer 段的原始数据
输出: CheckpointerConfig 实例

示例:
    from lead_agent.config.checkpointer_config import CheckpointerConfig

    cfg = CheckpointerConfig(type="postgres")
"""

from pydantic import BaseModel


class CheckpointerConfig(BaseModel):
    type: str = "postgres"
