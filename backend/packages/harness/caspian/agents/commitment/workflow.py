"""
本文件对外提供人工恢复流程、delegate_with_review 工具和 Supervisor 子图构建函数。

输入:
    CommitmentState — 当前阶段、人工等待标记、阶段产物和 thread 上下文。
    ReviewedDelegator — 执行 Worker-Evaluator 审核的内部委派器。
    approve / revise resume — 人工批准、反馈或 replacement 数据。

输出:
    BaseTool — Supervisor 唯一可调用的 delegate_with_review 工具。
    CompiledStateGraph — 严格按 stage 1 到 9 推进的 Supervisor 子图。
    Command — 阶段推进、人工等待、知识合同落盘及消息更新。

具体工作流:
    (1) Supervisor 根据最后完成阶段构造下一份 TaskEnvelope。
    (2) delegate_with_review 校验 stage 后调用 ReviewedDelegator。
    (3) 固定人工节点、条件冲突或失败结果通过 interrupt 等待人工处理。
    (4) 人工修订重新经过 Evaluator，未批准前不推进业务阶段。
    (5) 阶段六和七写入知识与合同，阶段九生成最终合同消息。

示例:
    supervisor = _build_supervisor(ReviewedDelegator(model, context7_tools))
"""

import asyncio
import json
from typing import Any

from langchain.messages import AIMessage, ToolMessage
from langchain.tools import ToolRuntime, tool
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command, interrupt

from caspian.agents.commitment.artifacts import (
    _build_final_message,
    _write_contract,
    _write_knowledge,
)
from caspian.agents.commitment.delegation import ReviewedDelegator
from caspian.agents.commitment.schemas import (
    CommitmentState,
    TaskEnvelope,
    WorkerOutput,
)
from caspian.agents.commitment.stage_rules import (
    _HUMAN_REVIEW_STAGES,
    _contains_unresolved_versions,
    _has_open_conflicts,
    _json,
    _must_revise,
    _stage_envelope,
    _stage_four_needs_review,
    _stage_timeout,
    _validate_stage_result,
)

def _human_payload(stage: int, state: dict[str, Any], error: str = "") -> dict[str, Any]:
    draft = state.get("artifacts", {}).get(str(stage))
    decisions = (
        ["revise"]
        if _must_revise(stage, draft)
        else ["approve", "revise"]
    )
    if not error and isinstance(draft, dict):
        error = str(draft.get("feedback", ""))
    revise_label = (
        "重试或修订"
        if isinstance(draft, dict) and draft.get("status") == "reviewed_failed"
        else "解决矛盾"
        if stage == 2 and decisions == ["revise"]
        else "补充引用"
        if stage == 4 and decisions == ["revise"]
        else "提出修订"
    )
    return {
        "type": "commitment_review",
        "stage": stage,
        "draft": draft,
        "allowed_decisions": decisions,
        "revise_label": revise_label,
        "error": error,
    }

async def _review_human_revision(
    response: dict[str, Any],
    stage: int,
    state: dict[str, Any],
    delegator: ReviewedDelegator,
) -> tuple[Any | None, str]:
    replacement = response.get("replacement")
    feedback = str(response.get("feedback", "")).strip()
    if replacement is not None:
        if not isinstance(replacement, dict):
            return None, "replacement 必须是对象"
        error = _validate_stage_result(
            stage,
            replacement,
            _stage_envelope(stage, state, feedback).context,
        )
        if error:
            return None, error
        try:
            review = await asyncio.wait_for(
                delegator._evaluator(
                    _stage_envelope(stage, state, feedback),
                    WorkerOutput(result=replacement),
                ),
                timeout=_stage_timeout(stage),
            )
        except TimeoutError:
            return None, f"阶段{stage} replacement 审核超时，请重试"
        return (
            (replacement, "")
            if review.approved
            else (None, review.feedback or "Evaluator 拒绝 replacement")
        )
    if feedback:
        try:
            output, error = await asyncio.wait_for(
                delegator.run(_stage_envelope(stage, state, feedback)),
                timeout=_stage_timeout(stage),
            )
        except TimeoutError:
            return None, f"阶段{stage}执行超时，请重试或提交 replacement"
        return (output.result, "") if output else (None, error)
    return None, "revise 必须提供 replacement 或 feedback"

def build_delegate_with_review_tool(delegator: ReviewedDelegator) -> BaseTool:
    @tool(
        "delegate_with_review",
        args_schema=TaskEnvelope,
        description="执行承诺层当前阶段，经 Worker 和 Evaluator 审核后推进状态。",
    )
    async def delegate_with_review(
        stage: int,
        instruction: str,
        context: dict[str, Any],
        acceptance_criteria: list[str],
        runtime: ToolRuntime,
    ) -> Command:
        state = dict(runtime.state)
        artifacts = dict(state.get("artifacts", {}))
        awaiting = state.get("awaiting_human")

        if awaiting:
            error = ""
            while True:
                response = interrupt(_human_payload(awaiting, state, error))
                if not isinstance(response, dict):
                    error = "resume 必须是对象"
                    continue
                decision = response.get("decision")
                if decision == "approve":
                    if _must_revise(awaiting, artifacts.get(str(awaiting))):
                        error = f"第{awaiting}步仍需修订，不能直接批准"
                        continue
                    if awaiting == 5 and _contains_unresolved_versions(
                        artifacts.get("5")
                    ):
                        error = "技术版本仍有 unresolved，必须先 revise"
                        continue
                    content = _json(
                        {
                            "status": "approved",
                            "stage": awaiting,
                            "result": artifacts.get(str(awaiting)),
                        }
                    )
                    return Command(
                        update={
                            "awaiting_human": None,
                            "messages": [
                                ToolMessage(
                                    content=content,
                                    tool_call_id=runtime.tool_call_id,
                                )
                            ],
                        }
                    )
                if decision == "revise":
                    revised, error = await _review_human_revision(
                        response, awaiting, state, delegator
                    )
                    if revised is None:
                        continue
                    artifacts[str(awaiting)] = revised
                    knowledge_files = state.get("knowledge_files", [])
                    if awaiting == 6:
                        knowledge_files = _write_knowledge(revised)
                    return Command(
                        update={
                            "artifacts": artifacts,
                            "knowledge_files": knowledge_files,
                            "messages": [
                                ToolMessage(
                                    content=_json(
                                        {
                                            "status": "revised",
                                            "stage": awaiting,
                                            "result": revised,
                                            "next_action": "call_again_for_approval",
                                        }
                                    ),
                                    tool_call_id=runtime.tool_call_id,
                                )
                            ],
                        }
                    )
                error = "decision 只允许 approve 或 revise"

        expected = int(state.get("stage", 0)) + 1
        if stage != expected:
            return Command(
                update={
                    "messages": [
                        ToolMessage(
                            content=_json(
                                {
                                    "status": "invalid_stage",
                                    "expected": expected,
                                    "received": stage,
                                }
                            ),
                            tool_call_id=runtime.tool_call_id,
                        )
                    ]
                }
            )

        envelope = TaskEnvelope(
            stage=stage,
            instruction=instruction,
            context={
                **context,
                "source_text": state.get("source_text", ""),
                "approved_stages": artifacts,
            },
            acceptance_criteria=acceptance_criteria,
        )

        artifact_ref = None
        if stage <= 7:
            try:
                output, error = await asyncio.wait_for(
                    delegator.run(envelope),
                    timeout=_stage_timeout(stage),
                )
            except TimeoutError:
                output = None
                error = f"阶段{stage}执行超时，请重试或提交 replacement"
            if output is None:
                result = {
                    "status": "reviewed_failed",
                    "feedback": error,
                }
                artifacts[str(stage)] = result
                return Command(
                    update={
                        "artifacts": artifacts,
                        "awaiting_human": stage,
                        "messages": [
                            ToolMessage(
                                content=_json(
                                    {
                                        "status": "needs_human",
                                        "stage": stage,
                                        "feedback": error,
                                    }
                                ),
                                tool_call_id=runtime.tool_call_id,
                            )
                        ]
                    }
                )
            result = output.result
            artifact_ref = output.artifact_ref
        elif stage == 8:
            result = {"task_contract": state.get("task_contract", "")}
        else:
            result = {
                "final_message": _build_final_message(
                    state.get("task_contract", ""),
                    list(state.get("knowledge_files", [])),
                )
            }

        updates: dict[str, Any] = {"stage": stage}
        artifacts[str(stage)] = result
        updates["artifacts"] = artifacts

        if stage == 6:
            updates["knowledge_files"] = _write_knowledge(result)
            artifact_ref = ",".join(updates["knowledge_files"])
        elif stage == 7:
            contract, artifact_ref = _write_contract(
                str(state.get("thread_id", "")), result
            )
            updates["task_contract"] = contract
        elif stage == 8:
            updates["task_contract"] = state.get("task_contract", "")
        elif stage == 9:
            updates["final_message"] = result["final_message"]

        if (
            stage in _HUMAN_REVIEW_STAGES
            or (stage == 2 and _has_open_conflicts(result))
            or (stage == 4 and _stage_four_needs_review(result))
        ):
            updates["awaiting_human"] = stage

        updates["messages"] = [
            ToolMessage(
                content=_json(
                    {
                        "status": "approved",
                        "stage": stage,
                        "result": result,
                        "artifact_ref": artifact_ref,
                        "next_stage": stage + 1 if stage < 9 else None,
                    }
                ),
                tool_call_id=runtime.tool_call_id,
            )
        ]
        return Command(update=updates)

    return delegate_with_review

def _prepare_supervisor_call(state: CommitmentState) -> dict[str, Any]:
    stage = int(state.get("awaiting_human") or state.get("stage", 0) + 1)
    envelope = _stage_envelope(stage, dict(state))
    return {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "delegate_with_review",
                        "args": envelope.model_dump(),
                        "id": f"commitment-stage-{stage}-{len(state.get('messages', []))}",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    }

def _route_supervisor(state: CommitmentState) -> str:
    if int(state.get("stage", 0)) >= 9:
        return END
    messages = state.get("messages", [])
    if messages and isinstance(messages[-1], ToolMessage):
        try:
            if json.loads(str(messages[-1].content)).get("status") == "reviewed_failed":
                return END
        except (TypeError, ValueError):
            pass
    return "prepare_call"

def _build_supervisor(delegator: ReviewedDelegator):
    builder = StateGraph(CommitmentState)
    builder.add_node("prepare_call", _prepare_supervisor_call)
    builder.add_node(
        "delegate_with_review",
        ToolNode([build_delegate_with_review_tool(delegator)]),
    )
    builder.add_edge(START, "prepare_call")
    builder.add_edge("prepare_call", "delegate_with_review")
    builder.add_conditional_edges("delegate_with_review", _route_supervisor)
    return builder.compile()
