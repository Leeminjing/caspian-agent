"""
本文件对外提供 CommitmentMiddleware、TaskEnvelope 和 ReviewedDelegator。

输入:
    lead agent state、LangGraph runtime、ChatModel 与 Context7 工具

输出:
    CommitmentMiddleware — 在 lead agent 执行前运行隔离的九阶段承诺流程
    TaskEnvelope — Supervisor 调用 delegate_with_review 的固定输入
    ReviewedDelegator — Worker–Evaluator 最多三次审核闭环

具体工作流:
    (1) Supervisor 只调用 delegate_with_review，stage 从 0 递增至 9
    (2) 语义阶段由 Worker 产出并由 Evaluator 审核
    (3) 阶段 3、5、6 通过 interrupt 等待人工批准或修订
    (4) 阶段 6、7 写入 knowledge 与 requirements
    (5) 最终只向 lead state 返回 task_contract 和一条 HumanMessage
"""

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import AgentState
from langchain.messages import HumanMessage, RemoveMessage, ToolMessage
from langchain.tools import ToolRuntime, tool
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field
from typing_extensions import NotRequired


_HUMAN_REVIEW_STAGES = frozenset({3, 5, 6})
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")
_MAX_REVIEW_ATTEMPTS = 3
_PROJECT_ROOT = Path(__file__).resolve().parents[6]

_STAGE_INSTRUCTIONS: dict[int, tuple[str, list[str]]] = {
    1: ("明确用户的单一主目标，保留边界和预期结果。", ["目标清晰", "不引入用户未提出的目标"]),
    2: ("汇总全部要求并指出要求之间的矛盾。", ["要求完整", "矛盾显式列出"]),
    3: ("给每条要求分配 1、2、3 三档优先级；3=必须，2=可协商，1=可选。", ["每条要求有等级", "等级只使用1到3"]),
    4: ("汇总必要输入，包括参考文件、参考网址和缺失输入。", ["输入可定位", "缺失项明确"]),
    5: ("识别涉及技术，对比项目当前版本与 Context7 候选最新稳定版。", ["每项技术有精确版本或unresolved", "不得猜测版本"]),
    6: ("按已批准版本调用 Context7 获取官方技术知识。", ["每项知识含技术、版本、官方来源和正文", "不得使用非官方来源"]),
    7: ("把已批准的阶段结果组装为完整 Markdown 任务合同。", ["合同包含九步已有结论", "合同可直接指导执行"]),
    8: ("产出最终 task_contract。", ["内容与磁盘合同一致"]),
    9: ("准备 lead agent 的最终合同消息。", ["只包含合同和理论基础"]),
}


class TaskEnvelope(BaseModel):
    stage: int = Field(ge=1, le=9)
    instruction: str
    context: dict[str, Any] = Field(default_factory=dict)
    acceptance_criteria: list[str] = Field(default_factory=list)


class WorkerOutput(BaseModel):
    result: dict[str, Any]
    artifact_ref: str | None = None


class ReviewOutput(BaseModel):
    approved: bool
    feedback: str = ""


class CommitmentState(AgentState):
    stage: NotRequired[int]
    awaiting_human: NotRequired[int | None]
    artifacts: NotRequired[dict[str, Any]]
    source_text: NotRequired[str]
    thread_id: NotRequired[str]
    knowledge_files: NotRequired[list[str]]
    task_contract: NotRequired[str]
    final_message: NotRequired[str]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _safe_segment(value: str, label: str) -> str:
    if value in {".", ".."} or not value or not _SAFE_SEGMENT.fullmatch(value):
        raise ValueError(f"{label} 只允许字母、数字、点、下划线和连字符")
    return value


def _stage_envelope(stage: int, state: dict[str, Any], feedback: str = "") -> TaskEnvelope:
    instruction, criteria = _STAGE_INSTRUCTIONS[stage]
    context = {
        "source_text": state.get("source_text", ""),
        "approved_stages": state.get("artifacts", {}),
    }
    if feedback:
        context["human_feedback"] = feedback
    return TaskEnvelope(
        stage=stage,
        instruction=instruction,
        context=context,
        acceptance_criteria=criteria,
    )


def _extract_structured(result: dict[str, Any], schema: type[BaseModel]) -> BaseModel:
    value = result.get("structured_response")
    if isinstance(value, schema):
        return value
    return schema.model_validate(value)


def _validate_stage_result(stage: int, result: dict[str, Any]) -> str | None:
    if not result:
        return "结果为空"
    if stage == 3:
        requirements = result.get("requirements")
        if not isinstance(requirements, list) or any(
            not isinstance(item, dict) or item.get("priority") not in {1, 2, 3}
            for item in requirements
        ):
            return "阶段3必须返回 requirements 列表，priority 仅允许1、2、3"
    if stage == 5 and not isinstance(result.get("technologies"), list):
        return "阶段5必须返回 technologies 列表"
    if stage == 6 and not isinstance(result.get("knowledge"), list):
        return "阶段6必须返回 knowledge 列表"
    if stage == 7 and not isinstance(result.get("contract_markdown"), str):
        return "阶段7必须返回 contract_markdown"
    return None


def _contains_unresolved_versions(result: Any) -> bool:
    if not isinstance(result, dict):
        return True
    technologies = result.get("technologies")
    if not isinstance(technologies, list) or not technologies:
        return True
    return any(
        not isinstance(item, dict)
        or not item.get("version")
        or str(item.get("version")).lower() == "unresolved"
        for item in technologies
    )


class ReviewedDelegator:
    def __init__(
        self,
        model: BaseChatModel,
        context7_tools: list[BaseTool],
    ) -> None:
        self._model = model
        self._context7_tools = context7_tools

    async def _worker(self, envelope: TaskEnvelope, feedback: str) -> WorkerOutput:
        tools = self._context7_tools if envelope.stage in {5, 6} else []
        agent = create_agent(
            model=self._model,
            tools=tools,
            system_prompt=(
                "你是承诺层 Worker。只处理当前阶段，使用工具核实版本和官方资料；"
                "不得依赖未核实的世界知识。返回符合 WorkerOutput 的结构化结果。"
            ),
            response_format=WorkerOutput,
            name=f"commitment_worker_{envelope.stage}",
        )
        prompt = {
            "stage": envelope.stage,
            "instruction": envelope.instruction,
            "context": envelope.context,
            "acceptance_criteria": envelope.acceptance_criteria,
            "reviewer_feedback": feedback,
        }
        result = await agent.ainvoke(
            {"messages": [HumanMessage(content=_json(prompt))]}
        )
        return _extract_structured(result, WorkerOutput)

    async def _evaluator(
        self,
        envelope: TaskEnvelope,
        worker_output: WorkerOutput,
    ) -> ReviewOutput:
        agent = create_agent(
            model=self._model,
            tools=[],
            system_prompt=(
                "你是独立 Evaluator。严格按验收条件判断 Worker 结果。"
                "存在遗漏、臆测版本、非官方来源或结构错误时必须拒绝并给出可执行反馈。"
            ),
            response_format=ReviewOutput,
            name=f"commitment_evaluator_{envelope.stage}",
        )
        result = await agent.ainvoke(
            {
                "messages": [
                    HumanMessage(
                        content=_json(
                            {
                                "task": envelope.model_dump(),
                                "worker_output": worker_output.model_dump(),
                            }
                        )
                    )
                ]
            }
        )
        return _extract_structured(result, ReviewOutput)

    async def run(self, envelope: TaskEnvelope) -> tuple[WorkerOutput | None, str]:
        feedback = ""
        for _ in range(_MAX_REVIEW_ATTEMPTS):
            worker_output = await self._worker(envelope, feedback)
            structure_error = _validate_stage_result(
                envelope.stage, worker_output.result
            )
            if structure_error:
                feedback = structure_error
                continue
            review = await self._evaluator(envelope, worker_output)
            if review.approved:
                return worker_output, ""
            feedback = review.feedback or "Evaluator 未提供具体反馈"
        return None, feedback


def _write_knowledge(result: dict[str, Any]) -> list[str]:
    files: list[str] = []
    for item in result.get("knowledge", []):
        if not isinstance(item, dict):
            raise ValueError("knowledge 项必须是对象")
        technology = _safe_segment(str(item.get("technology", "")), "technology")
        version = _safe_segment(str(item.get("version", "")), "version")
        source = str(item.get("source_url", "")).strip()
        content = str(item.get("content", "")).strip()
        if not source.startswith("http") or not content:
            raise ValueError("knowledge 项必须包含官方 source_url 和 content")
        relative_path = Path("knowledge") / f"{technology}-{version}.md"
        path = _PROJECT_ROOT / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# {technology} {version}\n\nSource: {source}\n\n{content}\n",
            encoding="utf-8",
        )
        files.append(relative_path.as_posix())
    if not files:
        raise ValueError("阶段6没有产生知识文件")
    return files


def _write_contract(thread_id: str, result: dict[str, Any]) -> tuple[str, str]:
    safe_thread_id = _safe_segment(thread_id, "thread_id")
    contract = str(result.get("contract_markdown", "")).strip()
    if not contract:
        raise ValueError("合同内容为空")
    relative_path = Path("requirements") / safe_thread_id / "task-contract.md"
    path = _PROJECT_ROOT / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contract + "\n", encoding="utf-8")
    return contract, relative_path.as_posix()


def _build_final_message(contract: str, knowledge_files: list[str]) -> str:
    sections = [f"<task_contract>\n{contract}\n</task_contract>"]
    for name in knowledge_files:
        content = (_PROJECT_ROOT / name).read_text(encoding="utf-8")
        sections.append(
            f'<theoretical foundation source="{name}">\n'
            f"{content}\n"
            "</theoretical foundation>"
        )
    return "\n\n".join(sections)


def _human_payload(stage: int, state: dict[str, Any], error: str = "") -> dict[str, Any]:
    return {
        "type": "commitment_review",
        "stage": stage,
        "draft": state.get("artifacts", {}).get(str(stage)),
        "allowed_decisions": ["approve", "revise"],
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
        error = _validate_stage_result(stage, replacement)
        if error:
            return None, error
        review = await delegator._evaluator(
            _stage_envelope(stage, state, feedback),
            WorkerOutput(result=replacement),
        )
        return (
            (replacement, "")
            if review.approved
            else (None, review.feedback or "Evaluator 拒绝 replacement")
        )
    if feedback:
        output, error = await delegator.run(
            _stage_envelope(stage, state, feedback)
        )
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
            output, error = await delegator.run(envelope)
            if output is None:
                return Command(
                    update={
                        "messages": [
                            ToolMessage(
                                content=_json(
                                    {
                                        "status": "reviewed_failed",
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

        if stage in _HUMAN_REVIEW_STAGES:
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


def _supervisor_prompt() -> str:
    stage_lines = "\n".join(
        f"{stage}: {instruction} 验收={criteria}"
        for stage, (instruction, criteria) in _STAGE_INSTRUCTIONS.items()
    )
    return (
        "你是承诺层 Supervisor。你只能调用 delegate_with_review，不能自行回答任务。"
        "从 stage=1 开始，每次只调用一次工具；工具成功后按 next_stage 继续。"
        "工具返回 invalid_stage 时按 expected 重试；返回 reviewed_failed 时停止并报告失败；"
        "stage=9 完成后停止调用工具并返回完成。所有 context 使用空对象即可。\n"
        f"{stage_lines}"
    )


class CommitmentMiddleware(AgentMiddleware):
    state_schema = CommitmentState

    def __init__(
        self,
        model: BaseChatModel,
        context7_tools: list[BaseTool],
    ) -> None:
        super().__init__()
        delegator = ReviewedDelegator(model, context7_tools)
        self._supervisor = create_agent(
            model=model,
            tools=[build_delegate_with_review_tool(delegator)],
            system_prompt=_supervisor_prompt(),
            state_schema=CommitmentState,
            name="commitment_supervisor",
        )

    async def _run(self, state: AgentState, runtime: Any) -> dict[str, Any] | None:
        if state.get("task_contract"):
            return None
        messages = state.get("messages", [])
        if not messages:
            return None
        thread_id = getattr(getattr(runtime, "execution_info", None), "thread_id", None)
        if thread_id is None:
            raise ValueError("CommitmentMiddleware 无法获取 thread_id")
        source_text = "\n\n".join(str(message.content) for message in messages)
        result = await self._supervisor.ainvoke(
            {
                "messages": [
                    HumanMessage(
                        content="开始承诺流程。调用 delegate_with_review 执行 stage 1。"
                    )
                ],
                "stage": 0,
                "awaiting_human": None,
                "artifacts": {},
                "source_text": source_text,
                "thread_id": str(thread_id),
                "knowledge_files": [],
            }
        )
        if result.get("stage") != 9:
            raise RuntimeError("承诺流程未完成 stage 9")
        contract = str(result.get("task_contract", ""))
        final_message = str(result.get("final_message", ""))
        if not contract or not final_message:
            raise RuntimeError("承诺流程未产出合同或最终消息")
        return {
            "task_contract": contract,
            "messages": [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                HumanMessage(content=final_message),
            ],
        }

    def before_agent(self, state: AgentState, runtime: Any) -> dict[str, Any] | None:
        return asyncio.run(self._run(state, runtime))

    async def abefore_agent(
        self, state: AgentState, runtime: Any
    ) -> dict[str, Any] | None:
        return await self._run(state, runtime)
