"""
本文件定义 LanggraphStoreConfig Pydantic 配置模型。

对外提供:
    LanggraphStoreConfig(BaseModel) — langgraph_store 配置段的数据模型

输入: config.yaml 中 langgraph_store 段的原始数据
输出: LanggraphStoreConfig 实例

示例:
    from caspian.config.langgraph_store_config import LanggraphStoreConfig

    cfg = LanggraphStoreConfig(backend="async_postgres", vector_enabled=True, embed="text-embedding-v4", dims=1024, fields=["$"], embed_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")
"""

from pydantic import BaseModel


class LanggraphStoreConfig(BaseModel):
    backend: str = "async_postgres"
    vector_enabled: bool = True
    embed: str = "text-embedding-v4"
    dims: int = 1024
    fields: list[str] = ["$"]
    embed_base_url: str | None = None
    embed_api_key: str | None = None
