"""
本文件对外重导出 create_chat_model 与 resolve_model_config。

输入为可选 route 名和 AppConfig；输出分别为已构造 BaseChatModel 与显式选中的 ModelConfig。
工作流由 factory 完成配置选择和类解析，本入口不做模型名或 endpoint capability 推断。

示例: model_config = resolve_model_config("my-route", app_config=config)
"""

from caspian.models.factory import create_chat_model, resolve_model_config

__all__ = ["create_chat_model", "resolve_model_config"]
