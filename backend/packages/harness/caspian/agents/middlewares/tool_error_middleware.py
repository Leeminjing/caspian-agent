"""
本文件对外提供 ToolErrorMiddleware：统一收口"工具失败 → LLM 可见结果"。

背景:
    langchain create_agent 内部建 ToolNode 时未配置 handle_tool_errors，其默认
    _default_handle_tool_errors 只对 ToolInvocationError 返回 message，其它异常一律
    raise e。这导致工具执行抛出的 RuntimeError/SecurityError/ValueError 等会逃逸出
    agent.astream，run 失败/悬挂，用户看到"会话卡住"。

解决:
    该中间件经 AgentMiddleware.wrap_tool_call / awrap_tool_call 接入 ToolNode，执行每个
    工具前包一层：捕获任意非中断类异常，转成一条带 [tool_error] 前缀、保留原始异常
    类别与详情的 ToolMessage 回传 LLM，使 run 继续、LLM 据此决定下一步，而非中断/悬挂。

输入:
    无特殊输入（钩子由 create_agent -> ToolNode 注入 request 与 handler）

输出:
    awrap_tool_call -> ToolMessage | Command

工作流:
    (1) 调用 handler(request) 执行原工具
    (2) 成功 -> 原样透传结果
    (3) 捕获异常 -> (asyncio.CancelledError / KeyboardInterrupt) 直接重抛（不吞取消信号）
                     其它 Exception -> 返回 ToolMessage(content=f"[tool_error] {类型}: {异常}",
                     tool_call_id=request.tool_call["id"], name=request.tool_call["name"])

示例:
    middleware = ToolErrorMiddleware()
"""

import asyncio
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

__all__ = ["ToolErrorMiddleware"]


class ToolErrorMiddleware(AgentMiddleware):
    """统一收口：任何工具执行异常都转为 [tool_error] ToolMessage 回传 LLM，不重抛。"""

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> Any:
        try:
            return await handler(request)
        except (asyncio.CancelledError, KeyboardInterrupt):
            # 中断/取消信号不吞掉，保持 abort/取消语义
            raise
        except Exception as exc:  # noqa: BLE001 - 有意兜底：所有工具失败都回传 LLM
            call = getattr(request, "tool_call", {}) or {}
            return ToolMessage(
                content=f"[tool_error] {type(exc).__name__}: {exc}",
                tool_call_id=call.get("id"),
                name=call.get("name"),
            )

    def wrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> Any:
        # 同步路径：本系统以 async 执行（astream），此处仅作签名对齐回退
        return handler(request)
