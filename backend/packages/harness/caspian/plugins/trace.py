"""
本文件提供插件执行 trace 的记录与发布，支撑调试视图的"执行参与链"与变更快照展示。

对外提供:
    PluginTraceEvent — 单条执行参与记录（dataclass）
    TraceBuffer — 进程内 trace 环缓冲（容量上限，防内存无界增长）
    emit_plugin_trace — 经 LangGraph custom 流向当前 run 发布 plugin_trace 事件

输入:
    TraceBuffer.record(event: PluginTraceEvent) — 记录一条执行参与
    TraceBuffer.recent(run_id / plugin / limit) — 过滤查询最近记录
    emit_plugin_trace(payload: dict) — 事件载荷（须含 type="plugin_trace"）

输出:
    record → None（超出容量自动淘汰最旧）
    recent → list[dict]（JSON 兼容）
    emit_plugin_trace → None（无 stream writer 时静默跳过）

具体工作流:
    (1) 每条记录含 run_id / interface / plugin / status(ok/failed/timeout/skipped) /
        changed / latency_ms / snapshot（截断文本，mutator 变更链展示用）
    (2) 环缓冲为全局单队列，按 run_id 过滤查询
    (3) 事件发布复用既有 compaction_status / commitment_messages 的 custom 流模式

示例:
    buffer = TraceBuffer()
    buffer.record(PluginTraceEvent(run_id="r1", interface="before_model",
                                   plugin="vision", status="ok", changed=True))
    buffer.recent(run_id="r1")
"""

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_MAX_EVENTS = 500
_SNAPSHOT_CAP = 200


@dataclass
class PluginTraceEvent:
    """单条插件执行参与记录。"""

    run_id: str
    interface: str
    plugin: str
    status: str  # ok / failed / timeout / skipped
    changed: bool = False
    latency_ms: float = 0.0
    snapshot: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "interface": self.interface,
            "plugin": self.plugin,
            "status": self.status,
            "changed": self.changed,
            "latency_ms": self.latency_ms,
            "snapshot": self.snapshot,
            "detail": self.detail,
        }


def truncate(value: Any, cap: int = _SNAPSHOT_CAP) -> str:
    """截断序列化为文本，供变更快照展示。"""
    text = str(value)
    return text[:cap] + ("…" if len(text) > cap else "")


class TraceBuffer:
    """进程内 trace 环缓冲。"""

    def __init__(self, max_events: int = _MAX_EVENTS) -> None:
        self._events: deque[PluginTraceEvent] = deque(maxlen=max_events)

    def record(self, event: PluginTraceEvent) -> None:
        self._events.append(event)

    def recent(
        self,
        run_id: str | None = None,
        plugin: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for event in self._events:
            if run_id is not None and event.run_id != run_id:
                continue
            if plugin is not None and event.plugin != plugin:
                continue
            result.append(event.to_dict())
        return result[-limit:]


def emit_plugin_trace(payload: dict[str, Any]) -> None:
    """经 custom 流向当前 run 发布 plugin_trace 事件（无 writer 时静默跳过）。"""
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
    except RuntimeError:
        return
    writer({"type": "plugin_trace", **payload})
