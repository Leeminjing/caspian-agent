"""
本文件对外提供 resolve_model_config 与 create_chat_model 函数。

输入:
    name: 目标模型名，对应 ModelConfig.name。None 时取 AppConfig.models[0] 作为默认
    app_config: 配置对象。None 时内部调用 get_app_config() 自动加载默认 config.yaml
    **kwargs: 透传给 ChatModel 构造器的额外参数（如 temperature、max_tokens），若与 ModelConfig
              映射的参数重名则 **kwargs 优先

输出:
    resolve_model_config → 选中的 ModelConfig，供 agent 装配读取 route capability
    BaseChatModel 实例

工作流:
    1. resolve_model_config 只按显式配置名选择 route；None 使用首项
    2. create_chat_model 调用 resolver，再解析实现类并构造模型
    3. capability 保留在 ModelConfig，由装配层读取，不写入模型名或 adapter 猜测

示例:
    create_chat_model() → 默认模型 ChatOpenAI 实例
    create_chat_model("deepseek-v4-flash", temperature=0.5) → 指定模型 + 额外温度参数
"""

from langchain_core.language_models import BaseChatModel

from caspian.config import AppConfig, get_app_config
from caspian.config.model_config import ModelConfig
from caspian.reflection.resolvers import resolve_class


def resolve_model_config(
    name: str | None = None,
    *,
    app_config: AppConfig | None = None,
) -> ModelConfig:
    if app_config is None:
        app_config = get_app_config("config.yaml")

    if name is None:
        if not app_config.models:
            raise ValueError("AppConfig.models 为空，无法获取默认模型")
        return app_config.models[0]
    else:
        for m in app_config.models:
            if m.name == name:
                return m
    raise ValueError(f"未找到模型配置: '{name}'")


def create_chat_model(
    name: str | None = None,
    *,
    app_config: AppConfig | None = None,
    **kwargs,
) -> BaseChatModel:
    model_config = resolve_model_config(name, app_config=app_config)

    chat_model_cls = resolve_class(model_config.use)

    params: dict = {
        "model": model_config.model,
        "api_key": model_config.api_key,
        "base_url": model_config.base_url,
    }
    params.update(kwargs)

    return chat_model_cls(**params)
