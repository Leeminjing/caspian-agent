"""
本文件对外提供 RunStatus 和 DisconnectMode 两个 StrEnum 枚举类。

对外提供:
    RunStatus(StrEnum) — 单次 run 的生命周期状态枚举
    DisconnectMode(StrEnum) — SSE 消费者断开连接时的行为策略枚举

输入: 无 — 本文件为纯定义文件
输出: RunStatus、DisconnectMode 枚举类

示例:
    from lead_agent.runtime.runs.schemas import RunStatus, DisconnectMode
    status = RunStatus.pending
    mode = DisconnectMode.cancel
"""

from enum import StrEnum


class RunStatus(StrEnum):
    """单次 run 的生命周期状态"""

    pending = "pending"
    running = "running"
    success = "success"
    error = "error"
    timeout = "timeout"
    interrupted = "interrupted"


class DisconnectMode(StrEnum):
    """SSE 消费者断开连接时的行为。"""

    cancel = "cancel"
    continue_ = "continue"
