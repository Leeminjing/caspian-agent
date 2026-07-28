"""
本文件对外提供 create_chat_model 函数。

输入:
    name: 目标模型名，对应 ModelConfig.name。None 时取 AppConfig.models[0] 作为默认
    app_config: 配置对象。None 时内部调用 get_app_config() 自动加载默认 config.yaml
    **kwargs: 透传给 ChatModel 构造器的额外参数（如 temperature、max_tokens），若与 ModelConfig
              映射的参数重名则 **kwargs 优先

输出:
    BaseChatModel 实例

工作流:
    1. 若 app_config 为 None，调用 get_app_config("config.yaml") 自动加载
    2. 若 name 为 None，取 app_config.models[0]，空列表抛 ValueError
    3. 否则遍历 app_config.models 按 name 匹配，未命中抛 ValueError
    4. 调用 resolve_class(model_config.use) 获取 ChatModel 类
    5. 以 model/api_key/base_url 为基础参数，合并 **kwargs（kwargs 优先），构造并返回实例

示例:
    create_chat_model() → 默认模型 ChatOpenAI 实例
    create_chat_model("deepseek-v4-flash", temperature=0.5) → 指定模型 + 额外温度参数
"""

from langchain_core.language_models import BaseChatModel

from focus.config import AppConfig, get_app_config
from focus.reflection.resolvers import resolve_class


def create_chat_model(
    name: str | None = None,
    *,
    app_config: AppConfig | None = None,
    **kwargs,
) -> BaseChatModel:
    if app_config is None:
        app_config = get_app_config("config.yaml")

    if name is None:
        if not app_config.models:
            raise ValueError("AppConfig.models 为空，无法获取默认模型")
        model_config = app_config.models[0]
    else:
        for m in app_config.models:
            if m.name == name:
                model_config = m
                break
        else:
            raise ValueError(f"未找到模型配置: '{name}'")

    chat_model_cls = resolve_class(model_config.use)

    params: dict = {
        "model": model_config.model,
        "api_key": model_config.api_key,
        "base_url": model_config.base_url,
    }
    params.update(kwargs)

    return chat_model_cls(**params)
