"""
本文件对外提供委派账本的确定性捕获与渲染纯函数。

对外提供:
    extract_delegations — 从消息流确定性重建委派账本
    render_delegation_ledger — 将账本渲染为模型可见的 "Work already delegated" 上下文

输入:
    messages: list[AnyMessage] — AIMessage（task tool_calls）与 ToolMessage（结构化元数据）
    entries: list[DelegationEntry] — 账本条目

输出:
    list[DelegationEntry] — 重建账本（按首见顺序）
    str — 渲染文本（空账本返回空字符串）

具体工作流:
    (1) extract_delegations: 遍历 AIMessage 的 task tool_calls 建立条目（in_progress），
        配对 ToolMessage 的结构化元数据更新状态与结果
    (2) render_delegation_ledger: 标题 + 指导 + 条目行（最新在前），6000 字符预算超限省略最旧
    (3) 渲染文本做 HTML 转义，防止特殊字符破坏上下文结构

示例:
    entries = extract_delegations(messages)
    context = render_delegation_ledger(entries)
"""

from datetime import UTC, datetime
from html import escape
from typing import Any

from langchain.messages import AIMessage, AnyMessage, ToolMessage

from caspian.agents.lead_agent_state import DelegationEntry
from caspian.subagents.status_contract import read_subagent_result_metadata

_RESULT_BRIEF_CAP = 2000
_DESCRIPTION_CAP = 200
_LEDGER_RENDER_CHAR_BUDGET = 6000
_LEDGER_ENTRY_RESULT_RENDER_CAP = 120

_STATUS_ONLY_RESULT_BRIEFS = {
    "failed": "Task failed.",
    "cancelled": "Task cancelled by user.",
    "timed_out": "Task timed out.",
    "polling_timed_out": "Task polling timed out.",
}


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _bound_text(text: str, cap: int = _RESULT_BRIEF_CAP) -> str:
    """确定性 head/tail 截断。"""
    if len(text) <= cap:
        return text
    if cap <= 0:
        return ""
    marker = "\n...\n"
    head = cap * 2 // 3
    if cap <= len(marker):
        return text[:cap]
    tail = cap - head - len(marker)
    if tail <= 0:
        return text[:cap]
    return f"{text[:head]}{marker}{text[-tail:]}"


def _escape_context_text(value: object) -> str:
    return escape(" ".join(str(value).split()), quote=False)


def _status_guidance(status: str, stop_reason: str | None = None) -> str:
    """按状态生成模型指导（复用 / 勿重复 / 可重试）。"""
    if stop_reason:
        if status == "completed":
            return "hit a guardrail cap with a partial result; reuse the partial result or retry with a tighter scope"
        return "hit a guardrail cap with no usable result; retry with a tighter scope"
    if status == "in_progress":
        return "already delegated; do NOT delegate again; wait for or build on the result"
    if status == "completed":
        return "completed result; do NOT delegate again; reuse this result"
    if status == "failed":
        return "failed attempt; may retry with a changed plan"
    if status == "cancelled":
        return "cancelled attempt; may retry with a changed plan"
    if status == "timed_out":
        return "timed-out attempt; may retry with a changed plan"
    if status == "polling_timed_out":
        return "polling timed-out attempt; may retry with a changed plan"
    return "prior attempt; inspect status before retrying"


def _tool_call_name(tool_call: dict[str, Any]) -> str:
    name = tool_call.get("name")
    if isinstance(name, str):
        return name
    function = tool_call.get("function")
    if isinstance(function, dict) and isinstance(function.get("name"), str):
        return function["name"]
    return ""


def _tool_call_id(tool_call: dict[str, Any]) -> str | None:
    tool_call_id = tool_call.get("id")
    return str(tool_call_id) if tool_call_id else None


def _tool_call_args(tool_call: dict[str, Any]) -> dict[str, Any]:
    args = tool_call.get("args")
    return args if isinstance(args, dict) else {}


def extract_delegations(messages: list[AnyMessage]) -> list[DelegationEntry]:
    """从消息流确定性重建委派账本。

    输入:
        messages: list[AnyMessage] — AIMessage 与 ToolMessage 混合列表

    输出:
        list[DelegationEntry] — 按首见顺序的账本条目

    工作流:
        (1) 遍历 AIMessage 的 task tool_calls，建立 in_progress 条目
        (2) 遍历 ToolMessage，用结构化元数据更新配对条目
    """
    entries_by_id: dict[str, DelegationEntry] = {}
    order: list[str] = []
    now = _utc_now_iso()

    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        for tool_call in message.tool_calls or []:
            if _tool_call_name(tool_call) != "task":
                continue
            tool_call_id = _tool_call_id(tool_call)
            if tool_call_id is None:
                continue
            args = _tool_call_args(tool_call)
            description = str(args.get("description") or args.get("prompt") or "")[:_DESCRIPTION_CAP]
            if tool_call_id not in entries_by_id:
                order.append(tool_call_id)
            entries_by_id[tool_call_id] = {
                "id": tool_call_id,
                "description": description,
                "subagent_type": str(args.get("subagent_type") or ""),
                "status": "in_progress",
                "created_at": now,
            }

    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        tool_call_id = str(message.tool_call_id) if message.tool_call_id else ""
        entry = entries_by_id.get(tool_call_id)
        if entry is None:
            continue
        structured = read_subagent_result_metadata(message.additional_kwargs)
        if structured is None:
            continue
        entry["status"] = structured["status"]
        stop_reason = structured.get("stop_reason")
        if stop_reason:
            entry["stop_reason"] = stop_reason
        result_text = (
            structured.get("result_brief")
            or structured.get("error")
            or _STATUS_ONLY_RESULT_BRIEFS.get(structured["status"])
        )
        if result_text:
            entry["result_brief"] = _bound_text(result_text)
            entry["result_sha256"] = structured.get("result_sha256") or ""

    return [entries_by_id[tool_call_id] for tool_call_id in order]


def _fits_budget(lines: list[str], candidate: str, max_chars: int) -> bool:
    return len("\n".join([*lines, candidate])) <= max_chars


def _render_entry_line(entry: DelegationEntry) -> str:
    """渲染单条账本行（转义 + 状态指导 + 结果摘要）。"""
    status = _escape_context_text(entry["status"])
    description = _escape_context_text(entry["description"])
    subagent_type = _escape_context_text(entry["subagent_type"])
    guidance = _status_guidance(entry["status"], entry.get("stop_reason"))
    line = f"- [{status}] {description} (via {subagent_type}; {guidance})"
    result_brief = entry.get("result_brief")
    if result_brief:
        line += f" -> {_escape_context_text(_bound_text(result_brief, _LEDGER_ENTRY_RESULT_RENDER_CAP))}"
    return line


def render_delegation_ledger(
    entries: list[DelegationEntry],
    *,
    max_chars: int = _LEDGER_RENDER_CHAR_BUDGET,
) -> str:
    """将账本渲染为模型可见上下文。

    输入:
        entries: list[DelegationEntry] — 账本条目
        max_chars: int — 渲染预算（默认 6000）

    输出:
        str — 渲染文本；空账本返回空字符串

    工作流:
        (1) 空账本 → ""
        (2) 标题 + 指导说明 + 条目行（最新在前）
        (3) 超预算省略最旧条目并标注省略计数
    """
    if not entries:
        return ""

    lines = [
        "## Work already delegated",
        "Newest entries are shown first. In-progress entries are already delegated. "
        "Completed entries are reusable results. Failed, cancelled, or timed-out entries are prior attempts.",
    ]
    omitted = 0
    for index, entry in enumerate(reversed(entries)):
        line = _render_entry_line(entry)
        if _fits_budget(lines, line, max_chars):
            lines.append(line)
            continue
        omitted = len(entries) - index
        break

    if omitted:
        omitted_line = f"- ... {omitted} older delegation entries omitted from this model view because of context budget"
        while len(lines) > 1 and not _fits_budget(lines, omitted_line, max_chars):
            lines.pop()
            omitted += 1
            omitted_line = f"- ... {omitted} older delegation entries omitted from this model view because of context budget"
        if _fits_budget(lines, omitted_line, max_chars):
            lines.append(omitted_line)

    rendered = "\n".join(lines)
    if len(rendered) <= max_chars:
        return rendered
    return rendered[: max(0, max_chars - 4)] + "\n..."
