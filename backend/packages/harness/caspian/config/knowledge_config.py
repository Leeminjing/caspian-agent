"""
本文件定义 KnowledgeConfig Pydantic 配置模型。

对外提供:
    KnowledgeConfig(BaseModel) — knowledge 配置段的数据模型
    LevelPolicy(BaseModel) — 等级策略模型（域名→等级）
    RatingConfig(BaseModel) — 入库评级配置模型

输入: config.yaml 中 knowledge 段的原始数据
输出: KnowledgeConfig 实例

示例:
    from caspian.config.knowledge_config import KnowledgeConfig

    cfg = KnowledgeConfig(level_policy={"domains": {"docs.example.com": 3}})
"""

from pydantic import BaseModel, Field


class LevelPolicy(BaseModel):
    domains: dict[str, int] = Field(default_factory=dict)


class RatingConfig(BaseModel):
    """入库评级配置：软评级 + 硬映射的参数。

    model=None 表示使用默认模型（AppConfig.models[0]）；生产建议钉死以减小等级漂移。
    """

    enabled: bool = True
    model: str | None = None
    confidence_threshold: float = 0.5
    timeout_seconds: float = 60.0


class KnowledgeConfig(BaseModel):
    level_policy: LevelPolicy = LevelPolicy()
    rating: RatingConfig = RatingConfig()
