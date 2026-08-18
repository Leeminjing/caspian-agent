"""
本文件对外提供 ContextCompressionConfig Pydantic 配置模型,承接 config.yaml 的 context_compression 段。

对外提供:
    ContextCompressionConfig — 上下文压缩配置模型

输入:
    config.yaml 的 context_compression 段原始数据

输出:
    ContextCompressionConfig 实例

具体工作流:
    (1) AppConfig 挂载 context_compression 字段后,由 AppConfig.model_validate 递归构造
    (2) enabled=False 时中间件不装配(fail-safe 默认)
    (3) 各阈值字段见 design.md D3

示例:
    app_config = get_app_config("config.yaml")
    cfg = app_config.context_compression
"""

from pydantic import BaseModel


class ContextCompressionConfig(BaseModel):
    """上下文压缩配置。

    字段:
        enabled: bool — 总开关,false 时中间件不装配(默认 false)
        trigger_tokens: int — 预防触发阈值,state.messages 近似 tokens 达到即压缩
        keep_messages: int — 压缩后保留的最近消息条数(近期原文)
        max_tokens_to_summarize: int — 喂给摘要模型的输入 tokens 上限
        summary_model: str | None — 摘要模型名,None 时复用主模型
        summary_timeout_seconds: int — 摘要调用超时秒数,fail-soft
        prune_max_chars: int — 溢出恢复 L0 单条 ToolMessage 保留字符数
        recovery_max_attempts: int — 恢复阶梯每级重试次数
    """

    enabled: bool = False
    trigger_tokens: int = 100000
    keep_messages: int = 20
    max_tokens_to_summarize: int = 4000
    summary_model: str | None = None
    summary_timeout_seconds: int = 120
    prune_max_chars: int = 800
    recovery_max_attempts: int = 1
