"""
本文件定义 KnowledgeConfig Pydantic 配置模型。

对外提供:
    KnowledgeConfig(BaseModel) — knowledge 配置段的数据模型
    LevelPolicy(BaseModel) — 等级策略模型（域名→等级）

输入: config.yaml 中 knowledge 段的原始数据
输出: KnowledgeConfig 实例

示例:
    from caspian.config.knowledge_config import KnowledgeConfig

    cfg = KnowledgeConfig(level_policy={"domains": {"docs.example.com": 3}})
"""

from pydantic import BaseModel, Field


class LevelPolicy(BaseModel):
    domains: dict[str, int] = Field(default_factory=dict)


class KnowledgeConfig(BaseModel):
    level_policy: LevelPolicy = LevelPolicy()
