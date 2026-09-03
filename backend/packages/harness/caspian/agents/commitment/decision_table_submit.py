"""
本文件对外提供 submit_decision_table 共享改表事务函数，作为决策等级表改写的唯一事务入口。

对外提供:
    submit_decision_table — 对候选表执行「冲突检测 → 提交/中断」，供 update_decision_table
    工具与 DecisionTableEditMiddleware（手工编辑）共同复用，保证两种方式作用完全一致

输入:
    thread_id: str — 目标 thread 标识
    candidate: list[DecisionRow] — 候选新表条目（已由调用方构造并校验）
    existing: list[DecisionRow] | None — 旧表条目；None 时内部读取磁盘
    user_id: str | None — 用户标识，用于定位决策等级表文件
    expected_version: str | None — 期望版本（CAS）；None 时内部按读取到的版本回填
    model: BaseChatModel | None — 语义扫描所用模型；None 时懒创建，失败降级为仅等级裁决
    interrupt_fn: Callable[[dict], dict] | None — 中断函数，默认用 langgraph.interrupt

输出:
    str — 结果说明：检测通过/采纳返回新版本；保留旧表返回未变更；异常返回错误说明

具体工作流:
    (1) 读取旧表（若未传入），并按读取版本回填 expected_version（CAS 底线）
    (2) detect_decision_table → COMMIT / CONFIRM
    (3) COMMIT：以 expected_version 做 CAS 落盘并返回新 version
    (4) CONFIRM：调用 interrupt_fn 待机，用户二选一（keep/adopt）后落盘或保留

示例:
    result = submit_decision_table(thread_id, candidate, interrupt_fn=interrupt)
"""

import logging
from collections.abc import Callable

from langgraph.types import interrupt

from caspian.agents.commitment.decision_table import DecisionRow, read_decision_table, rewrite_decision_table
from caspian.agents.commitment.decision_table_detect import (
    DecisionTableAction,
    detect_decision_table,
    detect_decision_table_internal,
)

logger = logging.getLogger(__name__)


def _commit_table(
    thread_id: str,
    rows: list[DecisionRow],
    *,
    user_id: str | None = None,
    expected_version: str | None = None,
) -> str | None:
    """把候选/已决表原子写入并返回新 version（受保护 helper）。"""
    return rewrite_decision_table(
        str(thread_id), rows, user_id=user_id, expected_version=expected_version
    )


def _confirm_interrupt(candidate: list[DecisionRow], existing: list[DecisionRow], reasons: list[str]) -> dict:
    """构造中断载荷并返回用户裁定 dict（受保护 helper，供默认 interrupt_fn 使用）。"""
    payload = {
        "type": "decision_table_adjudication",
        "candidate": [
            {"id": row.id, "requirement": row.requirement, "decision": row.decision, "priority": row.priority}
            for row in candidate
        ],
        "existing": [
            {"id": row.id, "requirement": row.requirement, "decision": row.decision, "priority": row.priority}
            for row in existing
        ],
        "conflicts": reasons,
        "allowed_decisions": ["keep", "adopt"],
    }
    return interrupt(payload)


async def submit_decision_table(
    thread_id: str,
    candidate: list[DecisionRow],
    existing: list[DecisionRow] | None = None,
    *,
    model=None,
    internal_consistency: bool = False,
    interrupt_fn: Callable[[dict], dict] | None = None,
    user_id: str | None = None,
    expected_version: str | None = None,
) -> str:
    """对候选新表执行冲突检测并提交/中断，返回结果说明。

    输入:
        thread_id: 目标 thread 标识
        candidate: 候选新表条目（已由调用方构造并完成机械校验）
        existing: 旧表条目；未提供时内部读取磁盘
        user_id: 用户标识；非空按用户隔离
        expected_version: 期望版本（CAS）；未提供时按内部读取版本回填
        model: 语义扫描模型；None 懒创建
        internal_consistency: True 时按"新表内部自洽"检测（手工编辑完整表替换）
        interrupt_fn: 中断函数；默认 langgraph.interrupt，测试可注入

    输出:
        str — 结果说明
    """
    if existing is None:
        table = read_decision_table(str(thread_id), user_id=user_id)
        existing = list(table.rows) if table is not None else []
        if expected_version is None and table is not None:
            expected_version = table.version

    if internal_consistency:
        verdict = await detect_decision_table_internal(candidate, model)
    else:
        verdict = await detect_decision_table(candidate, existing, model)

    do_interrupt: Callable[[dict], dict] = interrupt_fn or interrupt
    if verdict.action == DecisionTableAction.CONFIRM:
        # 任一明确冲突 → 中断待机，用户二选一（保留旧表 / 采纳新表）
        choice = do_interrupt(_confirm_interrupt(candidate, existing, verdict.reasons))
        if choice.get("decision") == "adopt":
            version = _commit_table(str(thread_id), candidate, user_id=user_id, expected_version=expected_version)
            if version is None:
                return "写入决策等级表失败，请重试"
            return f"已采纳新表，决策等级表已更新，新版本 {version}"
        return "已保留旧表，决策等级表未变更"
    # 检测通过（无冲突或仅 potential）→ 直接提交
    version = _commit_table(str(thread_id), candidate, user_id=user_id, expected_version=expected_version)
    if version is None:
        return "写入决策等级表失败，请重试"
    return f"决策等级表已更新，新版本 {version}"
