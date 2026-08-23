"""
本文件对外提供 build_exit_plan_mode_tool，作为计划模式的评审退出工具工厂。

输入:
    无

输出:
    BaseTool — 名为 exit_plan_mode 的 LangChain 异步工具

工作流:
    (1) execute 读取 runtime.state.get('plan_active')；未激活时返回失败提示
    (2) 校验 plan 非空且以 # 开头；否则返回失败提示
    (3) 激活时调用 langgraph.types.interrupt（负载 {type:'plan_review', plan, approve_label,
        keep_label}）暂停，把计划呈现给前端；用户经 Command(resume=...) 提供决定后继续
        - {decision:'approve'}       → 置 plan_active=False 并返回批准确认 ToolMessage
        - {decision:'keep', feedback} → 留在计划模式，返回携带反馈的结果文本
        - 其他（放弃/聊一聊）           → 返回提示，指示模型留在计划模式、等待用户消息

示例:
    tool = build_exit_plan_mode_tool()
"""

from typing import Annotated, Any

from langchain.tools import InjectedToolCallId, tool
from langchain_core.messages import ToolMessage
from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command, interrupt


def _plan_review_payload(plan: str) -> dict[str, Any]:
    """构造 plan_review 中断负载（受保护 helper）。"""
    return {
        "type": "plan_review",
        "plan": plan,
        "approve_label": "Approve",
        "keep_label": "Keep planning",
    }


def build_exit_plan_mode_tool():
    """工厂：返回按 @tool 声明的 exit_plan_mode 工具。"""

    @tool("exit_plan_mode", parse_docstring=True)
    async def exit_plan_mode(
        runtime: ToolRuntime,
        plan: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> str | Command:
        """Present the completed plan for review; on approval, leave plan mode.

        Use only in plan mode. Present your plan for the user's review and, on
        approval, leave plan mode. Send the COMPLETE plan as markdown, starting
        with a # heading that names it. The user may approve (carry out the plan
        from your next step) or keep planning — their feedback comes back in the
        tool result; revise and present again.

        Args:
            plan: The complete plan, as markdown, starting with a # heading that names it.
        """
        state = runtime.state or {}
        if not state.get("plan_active"):
            return "exit_plan_mode is only available in plan mode."
        if not plan.strip() or not plan.strip().startswith("#"):
            return "exit_plan_mode requires a non-empty markdown plan starting with a # heading."
        decision = interrupt(_plan_review_payload(plan))
        if isinstance(decision, dict) and decision.get("decision") == "approve":
            return Command(
                update={
                    "plan_active": False,
                    "messages": [
                        ToolMessage(
                            content="Plan approved — plan mode exited; carry out the plan starting with your next step.",
                            tool_call_id=tool_call_id,
                            name="exit_plan_mode",
                        )
                    ],
                }
            )
        if isinstance(decision, dict) and decision.get("decision") == "keep":
            feedback = decision.get("feedback") or ""
            text = "The user chose to keep planning; revise the plan and present it again."
            return f"{text} Their feedback: {feedback}" if feedback else text
        return (
            "The user dismissed the plan review to speak instead; stay in plan mode, "
            "stop here, and wait for their message."
        )

    return exit_plan_mode
