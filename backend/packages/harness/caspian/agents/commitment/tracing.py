"""
本文件对外提供 emit_commitment_trace 函数，用于流式发布承诺层的可审计执行轨迹。

输入:
    actor — supervisor、worker、evaluator、tool、human 或 system。
    event / title / status — 事件标识、展示标题和执行状态。
    stage / attempt — 当前业务阶段和审核轮次。
    detail / payload — 可展示的动作说明与结构化输入输出。

输出:
    None — 事件写入 LangGraph custom stream；非流式调用时静默跳过。

具体工作流:
    (1) 组装不包含模型隐藏思维链的结构化事件。
    (2) 获取当前 LangGraph stream writer。
    (3) 将事件写入 custom stream，或由外层 middleware 转发嵌套子图事件。

示例:
    emit_commitment_trace(
        actor="worker",
        event="completed",
        title="Worker 已生成候选结果",
        status="completed",
        stage=2,
        payload={"result": {"requirements": []}},
    )
"""

from typing import Any

from langgraph.config import get_stream_writer


def _write_commitment_trace(trace: dict[str, Any]) -> None:
    try:
        writer = get_stream_writer()
    except RuntimeError:
        return
    writer(trace)


def emit_commitment_trace(
    *,
    actor: str,
    event: str,
    title: str,
    status: str,
    stage: int,
    attempt: int | None = None,
    detail: str = "",
    payload: Any = None,
) -> None:
    trace = {
        "type": "commitment_trace",
        "actor": actor,
        "event": event,
        "title": title,
        "status": status,
        "stage": stage,
    }
    if attempt is not None:
        trace["attempt"] = attempt
    if detail:
        trace["detail"] = detail
    if payload is not None:
        trace["payload"] = payload
    _write_commitment_trace(trace)
