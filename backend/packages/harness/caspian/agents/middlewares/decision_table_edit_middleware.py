"""
本文件对外提供 DecisionTableEditMiddleware 类，作为用户手工编辑决策等级表的确定性入口。

对外提供:
    DecisionTableEditMiddleware(AgentMiddleware) — 在 before_agent 识别「编辑意图」human 消息，
    解析候选新表并确定性地调用共享 submit_decision_table，使手工编辑与 update_decision_table
    工具走同一段冲突检测/中断/热加载事务

输入:
    before_agent / abefore_agent:
        state: AgentState — 当前 agent 状态（含 messages）
        runtime: ToolRuntime — LangGraph 运行时（含 execution_info.thread_id）

输出:
    dict | None — 处理编辑后返回 messages 状态增量（把编辑意图消息原位替换为结果说明）；
                  无编辑意图或载荷非法时返回 None（放行普通 run）

具体工作流:
    (1) 扫描 state.messages 找带 decision_table_edit 标记的 human 消息
    (2) 找不到或载荷非法 → 返回 None（放行普通 run）
    (3) 解析候选新表条目，调用 submit_decision_table（可中断待机）
    (4) 返回固定 id 原位替换的结果消息

示例:
    middleware = DecisionTableEditMiddleware()
    # 在 create_agent(middleware=[..., middleware]) 中使用
"""

import logging

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import AgentState
from langchain.messages import HumanMessage
from langgraph.prebuilt import ToolRuntime

from caspian.agents.commitment.decision_table import DecisionRow, Guard
from caspian.agents.commitment.decision_table_submit import submit_decision_table

logger = logging.getLogger(__name__)

_EDIT_MARKER = "decision_table_edit"
_RESULT_MESSAGE_ID_SUFFIX = "-dt-edit-result"


def _find_edit_intent(state: AgentState) -> tuple[HumanMessage | None, list[DecisionRow] | None, str | None]:
    """从消息中解析编辑意图与候选新表（受保护 helper）。

    输入:
        state: AgentState — 当前 agent 状态
    输出:
        tuple[HumanMessage | None, list[DecisionRow] | None, str | None] —
            (触发消息, 候选条目, 错误)；无编辑意图时触发消息与条目均为 None
    """
    for message in reversed(state.get("messages", [])):
        if not isinstance(message, HumanMessage) or not message.id:
            continue
        kwargs = getattr(message, "additional_kwargs", None) or {}
        payload = kwargs.get(_EDIT_MARKER)
        if not isinstance(payload, dict):
            continue
        rows_raw = payload.get("rows")
        if not isinstance(rows_raw, list) or not rows_raw:
            return message, None, "编辑载荷缺少合法 rows"
        rows: list[DecisionRow] = []
        for item in rows_raw:
            if not isinstance(item, dict):
                return message, None, "编辑载荷 rows 项必须是对象"
            requirement = str(item.get("requirement", "") or "").strip()
            decision = str(item.get("decision", "") or "").strip()
            priority = item.get("priority")
            if not requirement or decision not in {"保留", "丢弃"} or priority not in {1, 2, 3}:
                return message, None, "编辑载荷条目字段非法（requirement/decision/priority）"
            row_id = str(item.get("id", "") or "").strip()
            guards: list[Guard] = []
            for guard_value in item.get("guards", []) or []:
                guard = Guard.from_dict(guard_value)
                if guard is not None:
                    guards.append(guard)
            rows.append(DecisionRow(
                requirement=requirement,
                decision=decision,
                priority=priority,
                id=row_id,
                guards=guards,
            ))
        return message, rows, None
    return None, None, None


def _result_message_id(trigger: HumanMessage) -> str:
    """结果消息沿用触发消息 id 或派生固定 id（受保护 helper）。"""
    return str(trigger.id) or (trigger.name or "user") + _RESULT_MESSAGE_ID_SUFFIX


class DecisionTableEditMiddleware(AgentMiddleware):

    async def _run(self, state: AgentState, runtime: ToolRuntime) -> dict | None:
        """核心逻辑：识别编辑意图并确定性地提交改表事务。

        输入:
            state: AgentState — 当前 agent 状态
            runtime: ToolRuntime — LangGraph 运行时
        输出:
            dict | None — 处理编辑返回消息增量；无意图/非法载荷放行返回 None
        """
        thread_id = None
        if runtime.execution_info is not None:
            thread_id = runtime.execution_info.thread_id
        if thread_id is None:
            logger.warning("DecisionTableEditMiddleware: 无法获取 thread_id，跳过")
            return None

        user_id = None
        ctx = getattr(runtime, "context", None)
        if isinstance(ctx, dict):
            raw_user_id = ctx.get("user_id")
            if raw_user_id:
                user_id = str(raw_user_id)

        trigger, candidate, error = _find_edit_intent(state)
        if trigger is None:
            return None  # 非编辑意图，放行普通 run
        if error:
            return {
                "jump_to": "end",
                "messages": [
                    HumanMessage(
                        content=f"决策等级表编辑失败：{error}",
                        id=_result_message_id(trigger),
                        additional_kwargs={"decision_table_edit_ack": True},
                    )
                ],
            }

        result = await submit_decision_table(
            str(thread_id), candidate, internal_consistency=True, user_id=user_id
        )
        # 编辑事务不是对话：以带 ack 标记的结果消息收尾，并把图跳到 end（不调模型、不留 AI 回复）
        return {
            "jump_to": "end",
            "messages": [
                HumanMessage(
                    content=result,
                    id=_result_message_id(trigger),
                    additional_kwargs={"decision_table_edit_ack": True},
                ),
            ],
        }

    def before_agent(self, state: AgentState, runtime: ToolRuntime) -> dict | None:
        """同步钩子：agent 执行前处理编辑意图。"""
        import asyncio

        try:
            return asyncio.run(self._run(state, runtime))
        except Exception:
            logger.error("DecisionTableEditMiddleware.before_agent 异常，放行", exc_info=True)
            return None

    async def abefore_agent(self, state: AgentState, runtime: ToolRuntime) -> dict | None:
        """异步钩子：逻辑与同步版本同构。"""
        try:
            return await self._run(state, runtime)
        except Exception:
            logger.error("DecisionTableEditMiddleware.abefore_agent 异常，放行", exc_info=True)
            return None


# 允许 before_agent 在图路由时跳到 end：跳过模型调用，编辑事务就此收尾（不产生对话）
DecisionTableEditMiddleware.before_agent.__can_jump_to__ = ("end",)
DecisionTableEditMiddleware.abefore_agent.__can_jump_to__ = ("end",)
