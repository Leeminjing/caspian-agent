"""
本文件对外提供 LeadAgentState 类及辅助 TypedDict 和自定义 reducer。

LeadAgentState: lead_agent 子图的 LangGraph State schema，继承自 AgentState，扩展业务字段
    plan_active — 计划模式激活标记（NotRequired[bool]，last-wins，空值折叠为未激活）
SandboxState: 沙箱绑定状态
ViewedImageData: 已查看图片数据
DelegationEntry: task 委派账本条目

merge_artifacts: artifacts 字段的 reducer，只增不减，去重保序
merge_viewed_images: viewed_images 字段的 reducer，累积/覆盖/清空
merge_delegations: delegations 字段的 reducer，同 id 原位替换保首见顺序，终态不可被非终态覆盖

输入: 无 — 本文件为纯定义文件，不包含函数入口
输出: LeadAgentState 类及辅助类型供 create_agent() 的 state_schema 参数使用


示例:
    from caspian.agents.lead_agent_state import LeadAgentState, merge_artifacts, merge_viewed_images
    graph = create_agent(model, tools, state_schema=LeadAgentState)
"""

from typing import TypedDict

from langchain.agents.middleware.types import AgentState
from typing_extensions import Annotated, NotRequired


class SandboxState(TypedDict):
    sandbox_id: NotRequired[str | None]


class ViewedImageData(TypedDict):
    base64: str
    mime_type: str


class DelegationEntry(TypedDict):
    """task 委派账本条目。"""

    id: str
    run_id: NotRequired[str]
    description: str
    subagent_type: str
    status: str
    result_brief: NotRequired[str]
    result_sha256: NotRequired[str]
    stop_reason: NotRequired[str]
    created_at: str


TERMINAL_DELEGATION_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "cancelled", "timed_out", "polling_timed_out"}
)


def merge_delegations(
    existing: list[DelegationEntry] | None,
    new: list[DelegationEntry] | None,
) -> list[DelegationEntry]:
    """delegations 字段的 reducer：同 id 原位替换保首见顺序，终态不可被非终态覆盖。

    输入:
        existing: list[DelegationEntry] | None — 既有账本
        new: list[DelegationEntry] | None — 新提交条目

    输出:
        list[DelegationEntry] — 合并后账本

    工作流:
        (1) new 为空 → 保留既有
        (2) 按 id 合并：新条目追加，同 id 以新版本替换，保持首见顺序
        (3) 既有条目为终态且新条目非终态 → 拒绝替换
    """
    if not new:
        return existing or []

    by_id: dict[str, DelegationEntry] = {}
    order: list[str] = []
    for entry in [*(existing or []), *new]:
        entry_id = entry["id"]
        previous = by_id.get(entry_id)
        if (
            previous is not None
            and previous["status"] in TERMINAL_DELEGATION_STATUSES
            and entry["status"] not in TERMINAL_DELEGATION_STATUSES
        ):
            continue
        if entry_id not in by_id:
            order.append(entry_id)
        by_id[entry_id] = entry
    return [by_id[entry_id] for entry_id in order]


def merge_artifacts(existing: list[str] | None, new: list[str] | None) -> list[str]:
    if existing is None and new is None:
        return []
    if existing is None:
        return new
    if new is None:
        return existing
    return list(dict.fromkeys(existing + new))


def merge_viewed_images(
    existing: dict[str, ViewedImageData] | None,
    new: dict[str, ViewedImageData] | None,
) -> dict[str, ViewedImageData]:
    if existing is None and new is None:
        return {}
    if existing is None:
        return new
    if new is None:
        return existing
    if not new:
        return {}
    return {**existing, **new}


class LeadAgentState(AgentState):
    sandbox: NotRequired[SandboxState | None]
    title: NotRequired[str | None]
    artifacts: Annotated[list[str], merge_artifacts]
    viewed_images: Annotated[dict[str, ViewedImageData], merge_viewed_images]
    task_contract: NotRequired[str]
    delegations: Annotated[list[DelegationEntry], merge_delegations]
    plan_active: NotRequired[bool]
    # 中间件（如 DecisionTableEditMiddleware）可设置 jump_to="end" 让图跳过模型调用直接结束
    jump_to: NotRequired[str | None]
