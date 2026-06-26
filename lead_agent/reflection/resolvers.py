"""
本文件对外提供 resolve_class 函数。

输入:
    use: 格式为 "module.path:ClassName" 的字符串，如 "langchain_openai:ChatOpenAI"
输出:
    对应的 Python 类对象
工作流:
    1. 校验 use 格式为 "module:Class"，不合法抛 ValueError
    2. 按 ":" 分割为 module_path 和 class_name
    3. 通过 importlib.import_module 动态导入模块
    4. 通过 getattr 获取并返回类对象
示例:
    resolve_class("langchain_openai:ChatOpenAI") → <class 'langchain_openai.chat_models.ChatOpenAI'>
"""

import importlib


def resolve_class(use: str) -> type:
    parts = use.split(":", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"无效的 use 格式: '{use}'，应为 'module:Class'")

    module_path, class_name = parts
    module = importlib.import_module(module_path)
    return getattr(module, class_name)
