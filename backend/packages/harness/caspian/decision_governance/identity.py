"""本文件对外提供执行 Runtime 的逻辑会话 ID 解析函数。

输入为 LangGraph Runtime；输出为用户可见且用于决策治理的逻辑会话 ID。
工作流优先读取经网关传入的 context.thread_id，再回退到工具独立调用的执行线程 ID。
示例：`thread_id = logical_thread_id(runtime)`。
"""

from __future__ import annotations

from typing import Any


def logical_thread_id(runtime: Any) -> str | None:
    context = runtime.context if isinstance(getattr(runtime, "context", None), dict) else {}
    value = context.get("thread_id")
    if value:
        return str(value)
    config = runtime.config if isinstance(getattr(runtime, "config", None), dict) else {}
    configurable = config.get("configurable") or {}
    value = configurable.get("logical_thread_id")
    if value:
        return str(value)
    value = getattr(getattr(runtime, "execution_info", None), "thread_id", None)
    if value:
        return str(value)
    value = configurable.get("thread_id")
    return str(value) if value else None
