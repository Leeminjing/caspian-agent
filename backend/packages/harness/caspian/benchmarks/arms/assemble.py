"""决策表 benchmark 双臂装配:软 baseline 与硬 treatment 的同体消融。

硬臂 = 生产决策表机制:DecisionTableMiddleware(注入表+仲裁规则)+
       DecisionTableEditMiddleware + DecisionTableGuardMiddleware + 真实
       update_decision_table 工具(submit_decision_table 事务 → CONFIRM 中断)。

软臂 = 同一策略写进 system prompt + 无仲裁直写工具(同签名,直接 rewrite)。

两条臂共用同一模型、同一 state_schema、同一 BASE system prompt,
唯一差是「策略由 prompt 表达」还是「由代码强制」。
"""

from __future__ import annotations

import logging

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime

from caspian.agents.commitment.decision_table import DecisionRow, read_decision_table, rewrite_decision_table
from caspian.agents.lead_agent_state import LeadAgentState
from caspian.agents.middlewares.decision_table_edit_middleware import DecisionTableEditMiddleware
from caspian.agents.middlewares.decision_table_guard_middleware import DecisionTableGuardMiddleware
from caspian.agents.middlewares.decision_table_middleware import DecisionTableMiddleware
from caspian.agents.middlewares.tool_error_middleware import ToolErrorMiddleware
from caspian.benchmarks.schema import TaskSpec
from caspian.tools.builtins.update_decision_table_tool import (
    _build_candidate_rows,
    _validate_entry,
    _validate_requirement,
)

logger = logging.getLogger(__name__)

BASE_SYSTEM_PROMPT = (
    "你是一个需求实施助手。当前会话有一份已批准的决策等级表(decision level table),"
    "每项决策带等级:3=必须 must,2=可协商 negotiable,1=可选 optional。"
    "当用户要求新增、修改或删除需求,或调整某条需求的优先级时,"
    "你必须调用 update_decision_table 工具把变更写入决策等级表。"
)

_SOFT_POLICY = (
    "\n\n<已批准决策等级表>\n{table_rows}\n</已批准决策等级表>\n\n"
    "<优先级策略>\n"
    "- 等级 3=必须,2=可协商,1=可选;等级比较是数值比较 3 > 2 > 1。\n"
    "- 新决策与已有条目冲突且等级更低 → 放弃新决策,遵循已有条目。\n"
    "- 新决策与已有条目冲突且等级相同或更高,或无法确定 → 停下来向用户确认后再继续。\n"
    "- MUST(等级 3)条目不得降级、不得删除。\n"
    "</优先级策略>"
)


def _table_rows_markdown(task: TaskSpec) -> str:
    lines = ["| requirement | priority |", "|---|---|"]
    lines.extend(f"| {row.requirement} | {row.priority} |" for row in task.table)
    return "\n".join(lines)


def _thread_id(runtime: ToolRuntime | None) -> str | None:
    if runtime is not None and runtime.execution_info is not None:
        return runtime.execution_info.thread_id
    return None


def _user_id(runtime: ToolRuntime | None) -> str | None:
    try:
        ctx = getattr(runtime, "context", None)
        if isinstance(ctx, dict):
            value = ctx.get("user_id")
            return str(value) if value else None
    except Exception:
        pass
    return None


@tool
async def update_decision_table(
    operation: str,
    requirement: str = "",
    decision: str = "保留",
    priority: int = 3,
    id: str = "",
    runtime: ToolRuntime = None,
) -> str:
    """软 baseline 的改表工具:同签名同名,但【不做冲突检测与等级裁决】,直接落盘。

    与真实 update_decision_table 的唯一差异:去掉 submit_decision_table 事务。
    """
    thread_id = _thread_id(runtime)
    if thread_id is None:
        return "无法获取当前 thread ID,拒绝更新"
    user_id = _user_id(runtime)

    table = read_decision_table(str(thread_id), user_id=user_id)
    existing = list(table.rows) if table is not None else []

    requirement = str(requirement).strip()
    if operation == "add":
        if error := _validate_requirement(requirement):
            return error
    if operation != "remove":
        if error := _validate_entry(decision, priority):
            return error

    effective_priority = priority if operation != "remove" else None
    candidate, build_error = _build_candidate_rows(
        existing, operation, str(id).strip(), requirement, decision, effective_priority
    )
    if build_error:
        return build_error

    version = rewrite_decision_table(str(thread_id), candidate, user_id=user_id)
    if version is None:
        return "写入决策等级表失败,请重试"
    return f"决策等级表已更新,新版本 {version}"


def _hard_agent(model: BaseChatModel) -> object:
    from caspian.tools.builtins import update_decision_table

    middlewares = [
        ToolErrorMiddleware(),
        DecisionTableMiddleware(),
        DecisionTableEditMiddleware(),
        DecisionTableGuardMiddleware(),
    ]
    return create_agent(
        model=model,
        tools=[update_decision_table],
        middleware=middlewares,
        system_prompt=BASE_SYSTEM_PROMPT,
        state_schema=LeadAgentState,
    )


def _soft_agent(model: BaseChatModel, task: TaskSpec) -> object:
    system_prompt = BASE_SYSTEM_PROMPT + _SOFT_POLICY.format(
        table_rows=_table_rows_markdown(task)
    )
    return create_agent(
        model=model,
        tools=[update_decision_table],
        middleware=[ToolErrorMiddleware()],
        system_prompt=system_prompt,
        state_schema=LeadAgentState,
    )


def assemble_agent(
    arm: str,
    task: TaskSpec | None = None,
    model: BaseChatModel | None = None,
    temperature: float = 0.0,
):
    """装配指定臂的 CompiledStateGraph。

    输入:
        arm: str — "hard" 或 "soft"
        task: TaskSpec — 软臂需要任务表以写入 prompt;硬臂可省略(表从磁盘注入)
        model: BaseChatModel — 可选,None 时用默认模型
        temperature: float — 模型温度,默认 0.0(追求可复现)
    输出:
        CompiledStateGraph
    """
    if model is None:
        from caspian.models import create_chat_model

        model = create_chat_model(temperature=temperature)

    if arm == "hard":
        return _hard_agent(model)
    if arm == "soft":
        if task is None:
            raise ValueError("soft 臂需要 task 以注入策略 prompt")
        return _soft_agent(model, task)
    raise ValueError(f"未知 arm: {arm}")
