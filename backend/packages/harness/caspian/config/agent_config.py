"""
本文件对外提供 AgentConfig，声明 agent 执行参数。

输入:
    config.yaml 中 agent 段

输出:
    AgentConfig — 供 AppConfig 聚合，worker 读取执行参数
"""

from pydantic import BaseModel, Field


class AgentConfig(BaseModel):
    recursion_limit: int = Field(
        default=50,
        ge=1,
        description="LangGraph 单次 run 的 recursion limit（模型循环上限，防死循环）",
    )
