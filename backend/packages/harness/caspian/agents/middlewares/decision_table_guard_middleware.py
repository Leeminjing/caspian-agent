"""
本文件对外提供 `DecisionTableGuardMiddleware` 类，作为决策等级表的行为强制守卫中间件。

对外提供:
    DecisionTableGuardMiddleware(AgentMiddleware) — 覆盖 wrap_tool_call / awrap_tool_call，
    对工具调用按当前 thread 决策等级表的硬层 guard 做确定性匹配：priority 3 拦截、2 警告、1 放行

输入:
    wrap_tool_call / awrap_tool_call:
        request: ToolCallRequest — 即将执行的工具调用请求（含 tool_call、runtime）
        handler: Callable — 下游处理器，调用 handler(request) 执行真实工具

输出:
    ToolMessage | Command — block 时返回拦截消息（不执行工具），warn 时返回执行结果+警示，其余原样返回

具体工作流:
    (1) 从 runtime.execution_info.thread_id 取 thread_id、从 runtime.context 取 user_id
    (2) read_decision_table 读当前等级表；无表或无硬层条目（含 guards）→ 直接放行
    (3) 对每个硬层条目按 guard 的 target 抽取工具字段文本，用 operator 匹配、kind 判方向
    (4) 命中取最高 priority 的条目：3 → block（返回 ToolMessage，不执行）、2 → warn、1 → 放行
    (5) 每次调用现读表（不缓存），与 DecisionTableMiddleware.before_model 热加载语义一致

示例:
    middleware = DecisionTableGuardMiddleware()
    # 在 create_agent(middleware=[..., middleware]) 中使用
"""

import fnmatch
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from caspian.agents.commitment.decision_table import DecisionRow, Guard, read_decision_table

logger = logging.getLogger(__name__)

_SHELL_TOOLS: frozenset = frozenset({"bash_tool", "powershell_tool", "cmd_tool", "sh_tool"})
_FILE_TOOLS: frozenset = frozenset({"read_file_tool", "write_file_tool"})
_URL_TOOLS: frozenset = frozenset({"web_fetch_tool", "web_fetch"})
_QUERY_TOOLS: frozenset = frozenset({"web_search_tool", "web_search"})
_KNOWLEDGE_TOOLS: frozenset = frozenset({"add_knowledge"})
_SUBAGENT_TOOLS: frozenset = frozenset({"task_tool", "task"})


def _extract_texts(tool_name: str, args: dict, target: str) -> list[str]:
    """按 target 从工具参数抽取待匹配文本列表（受保护 helper）。

    输入:
        tool_name: str — 工具名称
        args: dict — 工具调用参数
        target: str — 抽取目标（shell/file_path/file_content/url/query/knowledge/subagent）

    输出:
        list[str] — 待匹配文本列表；目标与工具不匹配或无对应字段时返回空列表
    """
    if not isinstance(args, dict):
        return []

    if target == "shell":
        if tool_name in _SHELL_TOOLS:
            command = args.get("command")
            return [command] if isinstance(command, str) else []
        return []

    if target == "file_path":
        if tool_name in _FILE_TOOLS:
            path = args.get("path")
            return [path] if isinstance(path, str) else []
        return []

    if target == "file_content":
        if tool_name == "write_file_tool":
            content = args.get("content")
            return [content] if isinstance(content, str) else []
        return []

    if target == "url":
        if tool_name in _URL_TOOLS:
            url = args.get("url")
            return [url] if isinstance(url, str) else []
        return []

    if target == "query":
        if tool_name in _QUERY_TOOLS:
            query = args.get("query")
            return [query] if isinstance(query, str) else []
        return []

    if target == "knowledge":
        if tool_name in _KNOWLEDGE_TOOLS:
            return [
                value for value in (args.get("content"), args.get("source"))
                if isinstance(value, str) and value
            ]
        return []

    if target == "subagent":
        if tool_name in _SUBAGENT_TOOLS:
            return [
                value for value in (args.get("subagent_type"), args.get("description"))
                if isinstance(value, str) and value
            ]
        return []

    return []


def _match(operator: str, pattern: str, text: str) -> bool:
    """按 operator 判定文本是否命中模式（受保护 helper）。

    输入:
        operator: str — regex / glob / contains / exact
        pattern: str — 模式
        text: str — 待匹配文本

    输出:
        bool — 是否命中
    """
    if operator == "regex":
        try:
            return re.search(pattern, text) is not None
        except re.error:
            return False
    if operator == "glob":
        return fnmatch.fnmatchcase(text, pattern)
    if operator == "contains":
        return pattern in text
    if operator == "exact":
        return pattern == text
    return False


def _row_violation(row: DecisionRow, tool_name: str, args: dict) -> Guard | None:
    """判定某硬层条目是否被当前工具调用违反（受保护 helper）。

    输入:
        row: DecisionRow — 硬层条目
        tool_name: str — 工具名称
        args: dict — 工具调用参数

    输出:
        Guard | None — 命中的 guard；未违反返回 None
    """
    for guard in row.guards:
        texts = _extract_texts(tool_name, args, guard.target)
        if not texts:
            continue
        if guard.kind == "forbid":
            if any(_match(guard.operator, guard.pattern, text) for text in texts):
                return guard
        elif guard.kind == "require":
            if not any(_match(guard.operator, guard.pattern, text) for text in texts):
                return guard
    return None


class DecisionTableGuardMiddleware(AgentMiddleware):

    @staticmethod
    def _thread_id(request: ToolCallRequest) -> str | None:
        return getattr(getattr(request.runtime, "execution_info", None), "thread_id", None)

    @staticmethod
    def _user_id(request: ToolCallRequest) -> str | None:
        try:
            ctx = request.runtime.context
            if isinstance(ctx, dict):
                user_id = ctx.get("user_id")
                if user_id:
                    return str(user_id)
        except Exception:
            pass
        return None

    def _classify(
        self, request: ToolCallRequest
    ) -> tuple[str, DecisionRow | None, Guard | None]:
        """判定当前工具调用的处置（受保护 helper）。

        输入:
            request: ToolCallRequest — 工具调用请求

        输出:
            tuple[str, DecisionRow | None, Guard | None] — (block/warn/pass, 命中条目, 命中 guard)
        """
        thread_id = self._thread_id(request)
        if thread_id is None:
            return "pass", None, None

        table = read_decision_table(str(thread_id), user_id=self._user_id(request))
        if table is None:
            return "pass", None, None

        hard_entries = table.hard_entries()
        if not hard_entries:
            return "pass", None, None

        tool_name = request.tool_call.get("name", "")
        args = request.tool_call.get("args", {}) or {}

        best: tuple[int, DecisionRow, Guard] | None = None
        for row in hard_entries:
            guard = _row_violation(row, tool_name, args)
            if guard is not None and (best is None or row.priority > best[0]):
                best = (row.priority, row, guard)

        if best is None:
            return "pass", None, None

        priority, row, guard = best
        if priority == 1:
            return "pass", None, None
        if priority == 3:
            return "block", row, guard
        return "warn", row, guard

    @staticmethod
    def _make_block_message(request: ToolCallRequest, row: DecisionRow, guard: Guard) -> ToolMessage:
        """构造拦截消息（受保护 helper）。

        输入:
            request: ToolCallRequest — 被拦截的工具调用
            row: DecisionRow — 命中的硬层条目
            guard: Guard — 命中的 guard

        输出:
            ToolMessage — status="error" 的拦截消息
        """
        tool_name = request.tool_call.get("name", "unknown")
        reason = guard.message or row.requirement
        return ToolMessage(
            content=(
                "[决策等级表] 动作已拦截 (status=error)\n"
                f"条目: {row.id}（等级 {row.priority}）\n"
                f"要求: {row.requirement}\n"
                f"原因: 命中守卫规则，{reason}"
            ),
            tool_call_id=request.tool_call.get("id", ""),
            name=tool_name,
        )

    @staticmethod
    def _append_warning(result, row: DecisionRow, guard: Guard) -> ToolMessage | Command:
        """在工具执行结果中追加等级表警示（受保护 helper）。

        输入:
            result: ToolMessage | Command — 原始执行结果
            row: DecisionRow — 命中的硬层条目
            guard: Guard — 命中的 guard

        输出:
            ToolMessage | Command — 追加警示后的结果（Command 不追加）
        """
        reason = guard.message or row.requirement
        warning = (
            f"\n\n[决策等级表] ⚠️ 可协商项命中: 条目 {row.id}（等级 {row.priority}）。\n"
            f"要求: {row.requirement}\n"
            f"说明: {reason}"
        )
        if isinstance(result, ToolMessage):
            return ToolMessage(
                content=(result.content or "") + warning,
                tool_call_id=result.tool_call_id,
                name=getattr(result, "name", None),
            )
        return result

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        """同步钩子：按决策等级表硬层守卫拦截/警示工具调用。

        输入:
            request: ToolCallRequest — 即将执行的工具调用
            handler: Callable — 下游处理器

        输出:
            ToolMessage | Command — 拦截消息或执行结果
        """
        try:
            disposition, row, guard = self._classify(request)
        except Exception:
            logger.error("DecisionTableGuard: 分类异常，fallback 放行", exc_info=True)
            return handler(request)

        if disposition == "block":
            return self._make_block_message(request, row, guard)

        result = handler(request)
        if disposition == "warn":
            result = self._append_warning(result, row, guard)
        return result

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        """异步钩子：逻辑与同步版本同构。

        输入:
            request: ToolCallRequest — 即将执行的工具调用
            handler: Callable — 下游异步处理器

        输出:
            ToolMessage | Command — 拦截消息或执行结果
        """
        try:
            disposition, row, guard = self._classify(request)
        except Exception:
            logger.error("DecisionTableGuard: 分类异常，fallback 放行", exc_info=True)
            return await handler(request)

        if disposition == "block":
            return self._make_block_message(request, row, guard)

        result = await handler(request)
        if disposition == "warn":
            result = self._append_warning(result, row, guard)
        return result
