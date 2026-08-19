"""
本文件提供 PluginHookMiddleware，把插件注入的有序 Hook 实现适配为 LangChain AgentMiddleware，
是插件 Hook 进入 agent 生命周期的唯一通道。

对外提供:
    PluginHookMiddleware — AgentMiddleware 子类，按接口语义执行插件 Hook 链

输入:
    PluginHookMiddleware(plugin_runtime, user_id=None) — 构造时从 PluginRuntime 取
        (public + 指定用户 custom) 的 Hook 装配快照；plugin_runtime 为 None 时为空链（零开销）
    Hook 实现调用契约:
        before_agent / before_model / before_tool（可修改）: provider(value: dict, ctx: dict) →
            dict | None（返回 dict 成为下一实现及后续流程使用的值；None 表示不修改）
        after_model / after_tool（只读观察）: provider(payload, ctx: dict) → 返回值丢弃

输出:
    before_agent / abefore_agent → dict | None（messages 变更翻译为 RemoveMessage 原位替换更新）
    before_model / abefore_model → dict | None（同上）
    wrap_model_call / awrap_model_call → 模型响应（after_model 观察后原样返回）
    wrap_tool_call / awrap_tool_call → ToolMessage | Command（before_tool 改参、after_tool 观察）

具体工作流:
    (1) 链空 → 直接返回 None / handler 原结果（零开销直通）
    (2) 链执行: 逐实现调用，异步实现经 asyncio.wait_for 超时保护（接口默认 30s，实现可覆盖）；
        同步实现直接调用（无法中断，超时保护不适用，建议插件实现使用异步入口）
    (3) 失败语义: 异常/超时记录为该插件实现的失败（trace + plugin_trace 事件），按接口失败
        策略（v1 全部 skip）跳过继续，不中断 run
    (4) 可修改链: 返回值非 None 时成为后续输入；messages 开放数据（before_agent/before_model）
        的最终变更翻译为 RemoveMessage(REMOVE_ALL) + 新消息列表的状态更新
    (5) 只读链: 返回值一律丢弃，原始结果不变

示例:
    middleware = PluginHookMiddleware(plugin_runtime, user_id=user_id)
    # before_model 注入的 A、B 依次收到 {"messages": [...]}，A 的修改成为 B 的输入
"""

import asyncio
import inspect
import logging
import time
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from caspian.plugins.runtime import PluginRuntime
from caspian.plugins.spec import resolve_interface
from caspian.plugins.trace import PluginTraceEvent, emit_plugin_trace, truncate

logger = logging.getLogger(__name__)

_MUTATOR_CHAIN_IFACES = frozenset({"before_agent", "before_model", "before_tool"})


def _plugin_context(runtime: Any) -> dict[str, Any]:
    """从 LangGraph runtime 提取插件上下文（user_id/thread_id/model_name 等）。"""
    ctx: dict[str, Any] = {}
    try:
        if isinstance(getattr(runtime, "context", None), dict):
            ctx.update(runtime.context)
    except Exception:
        pass
    thread_id = getattr(getattr(runtime, "execution_info", None), "thread_id", None)
    if thread_id is not None:
        ctx.setdefault("thread_id", str(thread_id))
    return ctx


def _messages_update(result: dict, current: list) -> dict | None:
    """把可修改链的 messages 变更翻译为状态更新；未修改时返回 None。"""
    new = result.get("messages")
    if new is None or new == current:
        return None
    return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *list(new)]}


class PluginHookMiddleware(AgentMiddleware):
    """插件 Hook 链中间件：空链零开销，链内失败按接口策略跳过。"""

    def __init__(
        self,
        plugin_runtime: PluginRuntime | None = None,
        user_id: str | None = None,
    ) -> None:
        super().__init__()
        snapshot = plugin_runtime.snapshot(user_id) if plugin_runtime is not None else {}
        self._chains: dict[str, list[tuple[str, Any]]] = snapshot.get("hooks", {})
        self._runtime = plugin_runtime

    # ------------------------------------------------------------------
    # 链执行核心
    # ------------------------------------------------------------------

    async def _run_mutator_chain(
        self, interface: str, value: dict, runtime: Any
    ) -> dict | None:
        """逐实现执行可修改链：返回值非 None 时成为后续输入；None 表示链无变更。"""
        chain = self._chains.get(interface) or []
        if not chain:
            return None
        ctx = _plugin_context(runtime)
        current = dict(value)
        for plugin_name, impl in chain:
            changed = False
            try:
                started = time.monotonic()
                result = impl.provider(current, ctx)
                if inspect.isawaitable(result):
                    timeout = impl.timeout_seconds or (
                        resolve_interface(interface).timeout_seconds or 30.0
                    )
                    result = await asyncio.wait_for(result, timeout=timeout)
                changed = result is not None
                if changed:
                    current = dict(result)
                self._record(runtime, interface, plugin_name, "ok", changed,
                             latency_ms=(time.monotonic() - started) * 1000,
                             snapshot=current if changed else "")
            except asyncio.TimeoutError:
                self._record(runtime, interface, plugin_name, "timeout", False,
                             detail="Extension Timeout")
            except Exception as exc:
                self._record(runtime, interface, plugin_name, "failed", False,
                             detail=f"{type(exc).__name__}: {exc}")
        return current if current != value else None

    async def _run_observer_chain(self, interface: str, payload: Any, runtime: Any) -> None:
        """逐实现执行只读观察链：返回值一律丢弃。"""
        chain = self._chains.get(interface) or []
        if not chain:
            return
        ctx = _plugin_context(runtime)
        for plugin_name, impl in chain:
            try:
                started = time.monotonic()
                result = impl.provider(payload, ctx)
                if inspect.isawaitable(result):
                    timeout = impl.timeout_seconds or (
                        resolve_interface(interface).timeout_seconds or 30.0
                    )
                    await asyncio.wait_for(result, timeout=timeout)
                self._record(runtime, interface, plugin_name, "ok", False,
                             latency_ms=(time.monotonic() - started) * 1000)
            except asyncio.TimeoutError:
                self._record(runtime, interface, plugin_name, "timeout", False,
                             detail="Extension Timeout")
            except Exception as exc:
                self._record(runtime, interface, plugin_name, "failed", False,
                             detail=f"{type(exc).__name__}: {exc}")

    def _record(
        self,
        runtime: Any,
        interface: str,
        plugin: str,
        status: str,
        changed: bool,
        *,
        latency_ms: float = 0.0,
        snapshot: Any = "",
        detail: str = "",
    ) -> None:
        """记录 trace 环缓冲并发布 plugin_trace SSE 事件。"""
        run_id = ""
        try:
            ctx = _plugin_context(runtime)
            run_id = str(ctx.get("run_id") or "")
        except Exception:
            pass
        payload = {
            "interface": interface,
            "plugin": plugin,
            "status": status,
            "changed": changed,
            "latency_ms": round(latency_ms, 1),
            "snapshot": truncate(snapshot) if snapshot else "",
            "detail": detail,
        }
        if self._runtime is not None:
            self._runtime.trace.record(
                PluginTraceEvent(run_id=run_id, **payload)
            )
        emit_plugin_trace({"run_id": run_id, **payload})

    # ------------------------------------------------------------------
    # AgentMiddleware 钩子
    # ------------------------------------------------------------------

    def before_agent(self, state, runtime):
        return asyncio.run(self._before_agent(state, runtime))

    async def abefore_agent(self, state, runtime):
        return await self._before_agent(state, runtime)

    async def _before_agent(self, state, runtime):
        messages = list(state.get("messages", []))
        result = await self._run_mutator_chain("before_agent", {"messages": messages}, runtime)
        if result is None:
            return None
        return _messages_update(result, messages)

    def before_model(self, state, runtime):
        return asyncio.run(self._before_model(state, runtime))

    async def abefore_model(self, state, runtime):
        return await self._before_model(state, runtime)

    async def _before_model(self, state, runtime):
        messages = list(state.get("messages", []))
        result = await self._run_mutator_chain("before_model", {"messages": messages}, runtime)
        if result is None:
            return None
        return _messages_update(result, messages)

    def wrap_model_call(self, request, handler):
        return asyncio.run(self._wrap_model_call(request, handler))

    async def awrap_model_call(self, request, handler):
        return await self._wrap_model_call(request, handler)

    async def _wrap_model_call(self, request, handler):
        if not self._chains.get("after_model"):
            return await handler(request)
        response = await handler(request)
        await self._run_observer_chain("after_model", response, request.runtime)
        return response

    def wrap_tool_call(self, request, handler):
        return asyncio.run(self._wrap_tool_call(request, handler))

    async def awrap_tool_call(self, request, handler):
        return await self._wrap_tool_call(request, handler)

    async def _wrap_tool_call(self, request, handler):
        before = self._chains.get("before_tool") or []
        after = self._chains.get("after_tool") or []
        if not before and not after:
            return await handler(request)
        tool_call = dict(request.tool_call)
        result = await self._run_mutator_chain("before_tool", tool_call, request.runtime)
        if result is not None:
            tool_call = result
        response = await handler(request.override(tool_call=tool_call))
        await self._run_observer_chain("after_tool", response, request.runtime)
        return response
