"""
本文件对外提供 subagent 结果契约的纯函数：模型可见文本与结构化元数据双通道。

对外提供:
    format_subagent_result_message — 生成模型可见的 task 结果文本
    make_subagent_additional_kwargs — 生成 ToolMessage.additional_kwargs 结构化元数据
    read_subagent_result_metadata — 从元数据还原结构化结果（供账本重建）

输入:
    status / result / error / stop_reason / model_name — task 终态信息

输出:
    tuple[str, str | None] — (模型可见文本, 归一化错误文本)
    dict[str, object] — additional_kwargs 载荷
    StructuredSubagentResult | None — 还原的结构化结果

具体工作流:
    (1) 文本通道：模型可见结果（"Task Succeeded. Result: ..." 等格式）
    (2) 元数据通道：subagent_status / subagent_stop_reason / subagent_result_brief(+sha256) /
        subagent_error / subagent_model_name，结果截断到 2000 字符
    (3) 契约校验：状态或 stop_reason 不在枚举内抛 ValueError

示例:
    content, meta_error = format_subagent_result_message("completed", result="分析完成")
    kwargs = make_subagent_additional_kwargs("completed", result="分析完成")
"""

import hashlib
import re
from typing import Any, Literal, NotRequired, TypedDict

SUBAGENT_STATUS_KEY = "subagent_status"
SUBAGENT_STOP_REASON_KEY = "subagent_stop_reason"
SUBAGENT_ERROR_KEY = "subagent_error"
SUBAGENT_RESULT_BRIEF_KEY = "subagent_result_brief"
SUBAGENT_RESULT_SHA256_KEY = "subagent_result_sha256"
SUBAGENT_MODEL_NAME_KEY = "subagent_model_name"
SUBAGENT_METADATA_TEXT_MAX_CHARS = 2000

_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}")

SubagentStatusValue = Literal[
    "completed",
    "failed",
    "cancelled",
    "timed_out",
    "polling_timed_out",
]

SUBAGENT_STATUS_VALUES: tuple[SubagentStatusValue, ...] = (
    "completed",
    "failed",
    "cancelled",
    "timed_out",
    "polling_timed_out",
)

SubagentStopReasonValue = Literal["token_capped", "turn_capped", "loop_capped"]

SUBAGENT_STOP_REASON_VALUES: tuple[SubagentStopReasonValue, ...] = (
    "token_capped",
    "turn_capped",
    "loop_capped",
)

_STOP_REASON_LABELS: dict[SubagentStopReasonValue, str] = {
    "token_capped": "token budget",
    "turn_capped": "turn budget",
    "loop_capped": "repeated tool-call loop",
}


class StructuredSubagentResult(TypedDict):
    status: SubagentStatusValue
    stop_reason: NotRequired[SubagentStopReasonValue]
    result_brief: NotRequired[str]
    result_sha256: NotRequired[str]
    error: NotRequired[str]


def _bound_metadata_text(text: str, cap: int = SUBAGENT_METADATA_TEXT_MAX_CHARS) -> str:
    """确定性 head/tail 截断：保留头 2/3 与尾 1/3，中间省略。"""
    cleaned = text.strip()
    if len(cleaned) <= cap:
        return cleaned
    marker = "\n...\n"
    if cap <= len(marker):
        return cleaned[:cap]
    head = cap * 2 // 3
    tail = cap - head - len(marker)
    if tail <= 0:
        return cleaned[:cap]
    return f"{cleaned[:head]}{marker}{cleaned[-tail:]}"


def make_subagent_additional_kwargs(
    status: SubagentStatusValue,
    *,
    result: str | None = None,
    error: str | None = None,
    stop_reason: SubagentStopReasonValue | None = None,
    model_name: str | None = None,
) -> dict[str, object]:
    """构造 ToolMessage.additional_kwargs 元数据载荷。

    输入:
        status: SubagentStatusValue — 终态状态
        result: str | None — 完成结果（仅 completed 携带）
        error: str | None — 错误文本（非 completed 携带）
        stop_reason: SubagentStopReasonValue | None — 守护截断原因（可加性字段）
        model_name: str | None — 实际使用模型名

    输出:
        dict[str, object] — 元数据载荷

    工作流:
        (1) 校验 status / stop_reason 在枚举内，未知值抛 ValueError
        (2) completed → result_brief + sha256；其余 → error
        (3) 空字段不落载荷
    """
    if status not in SUBAGENT_STATUS_VALUES:
        raise ValueError(f"invalid subagent status {status!r}")
    if stop_reason is not None and stop_reason not in SUBAGENT_STOP_REASON_VALUES:
        raise ValueError(f"invalid subagent stop_reason {stop_reason!r}")

    payload: dict[str, object] = {SUBAGENT_STATUS_KEY: status}
    if status == "completed" and isinstance(result, str) and result.strip():
        payload[SUBAGENT_RESULT_BRIEF_KEY] = _bound_metadata_text(result)
        payload[SUBAGENT_RESULT_SHA256_KEY] = hashlib.sha256(result.encode("utf-8")).hexdigest()
    if status != "completed" and isinstance(error, str) and error.strip():
        payload[SUBAGENT_ERROR_KEY] = _bound_metadata_text(error)
    if stop_reason is not None:
        payload[SUBAGENT_STOP_REASON_KEY] = stop_reason
    if isinstance(model_name, str) and model_name.strip():
        payload[SUBAGENT_MODEL_NAME_KEY] = model_name.strip()
    return payload


def format_subagent_result_message(
    status: SubagentStatusValue,
    *,
    result: str | None = None,
    error: str | None = None,
    stop_reason: SubagentStopReasonValue | None = None,
) -> tuple[str, str | None]:
    """生成模型可见的 task 结果文本。

    输入:
        status / result / error / stop_reason — 终态信息

    输出:
        tuple[str, str | None] — (模型可见文本, 归一化错误文本)

    工作流:
        (1) completed → "Task Succeeded. Result: ..."（cap 时折叠 (capped: ...) 标注）
        (2) cancelled / timed_out / polling_timed_out → 各自默认文案
        (3) failed → "Task failed. Error: ..."
    """
    result_text = "" if result is None else str(result)
    error_text = str(error).strip() if isinstance(error, str) else ""
    capped = _STOP_REASON_LABELS.get(stop_reason) if stop_reason is not None else None

    if status == "completed":
        if capped:
            return f"Task Succeeded (capped: {capped}). Result: {result_text}", None
        return f"Task Succeeded. Result: {result_text}", None

    if status == "cancelled":
        detail = error_text or "Task cancelled by user."
        if detail == "Task cancelled by user.":
            return detail, detail
        return f"Task cancelled by user. Error: {detail}", detail

    if status == "timed_out":
        detail = error_text or "Task timed out."
        if detail == "Task timed out.":
            return detail, detail
        return f"Task timed out. Error: {detail}", detail

    if status == "polling_timed_out":
        detail = error_text or "Task polling timed out."
        return detail, detail

    detail = error_text or "Task failed."
    if capped:
        if detail == "Task failed.":
            return f"Task failed (capped: {capped}).", detail
        return f"Task failed (capped: {capped}). Error: {detail}", detail
    if detail == "Task failed.":
        return detail, detail
    return f"Task failed. Error: {detail}", detail


def read_subagent_result_metadata(
    additional_kwargs: dict[str, Any] | None,
) -> StructuredSubagentResult | None:
    """从 ToolMessage.additional_kwargs 还原结构化结果。

    输入:
        additional_kwargs: dict | None — ToolMessage 元数据

    输出:
        StructuredSubagentResult | None — 还原结果；状态未知或载荷缺失时返回 None

    工作流:
        (1) 读取 subagent_status 并校验在枚举内
        (2) completed → result_brief + 校验 sha256 形状
        (3) 其余 → error；stop_reason 可选透传
    """
    if not additional_kwargs:
        return None
    raw_status = additional_kwargs.get(SUBAGENT_STATUS_KEY)
    if not isinstance(raw_status, str) or raw_status not in SUBAGENT_STATUS_VALUES:
        return None

    payload: StructuredSubagentResult = {"status": raw_status}
    raw_result = additional_kwargs.get(SUBAGENT_RESULT_BRIEF_KEY)
    raw_hash = additional_kwargs.get(SUBAGENT_RESULT_SHA256_KEY)
    raw_error = additional_kwargs.get(SUBAGENT_ERROR_KEY)
    if raw_status == "completed" and isinstance(raw_result, str) and raw_result.strip():
        payload["result_brief"] = _bound_metadata_text(raw_result)
        if isinstance(raw_hash, str) and _SHA256_HEX_RE.fullmatch(raw_hash):
            payload["result_sha256"] = raw_hash
    if raw_status != "completed" and isinstance(raw_error, str) and raw_error.strip():
        payload["error"] = _bound_metadata_text(raw_error)
    raw_stop_reason = additional_kwargs.get(SUBAGENT_STOP_REASON_KEY)
    if isinstance(raw_stop_reason, str) and raw_stop_reason in SUBAGENT_STOP_REASON_VALUES:
        payload["stop_reason"] = raw_stop_reason
    return payload
