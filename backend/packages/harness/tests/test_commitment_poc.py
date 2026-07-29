"""
本文件提供 CommitmentMiddleware PoC 的标准库 unittest。

输入:
    假 Worker/Evaluator、临时目录、假 StreamBridge 和假 agent stream

输出:
    可运行检查，覆盖开关、审核重试、人工修订、磁盘结果、消息隔离和 interrupt/resume
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from langchain.agents import create_agent
from langchain.messages import HumanMessage, RemoveMessage
from langchain.tools import tool
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Interrupt, interrupt

from backend.app.gateway.routers.thread_runs import RunCreateRequest
from backend.app.gateway.services import _build_graph_input
from caspian.agents.middlewares.builder import build_general_middlewares
from caspian.agents.commitment import (
    CommitmentMiddleware,
    CommitmentState,
    ReviewOutput,
    ReviewedDelegator,
    _SearchResultParser,
    TaskEnvelope,
    WorkerOutput,
    _build_final_message,
    _build_supervisor,
    _contains_unresolved_versions,
    _context7_candidate_version,
    _context7_stable_version,
    _extract_structured,
    _filter_stage_four_result,
    _has_open_conflicts,
    _human_payload,
    _normalize_stage_three_result,
    _review_human_revision,
    _safe_segment,
    _stage_four_needs_review,
    _stage_timeout,
    _validate_stage_result,
    _write_contract,
    _write_knowledge,
    build_delegate_with_review_tool,
)
from caspian.agents.commitment.tracing import emit_commitment_messages
from caspian.config.commitment_config import CommitmentConfig
from caspian.mcp.tools import get_context7_tools
from caspian.runtime.runs.schemas import RunStatus
from caspian.runtime.runs.worker import _extract_interrupts, run_agent


def supervisor_stage_messages(stage: int, result: dict) -> list:
    call_id = f"stage-{stage}"
    return [
        HumanMessage(content="task"),
        AIMessage(
            content=f"stage {stage}",
            tool_calls=[
                {
                    "name": "delegate_with_review",
                    "args": {"stage": stage},
                    "id": call_id,
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(
            content=json.dumps(
                {"status": "approved", "stage": stage, "result": result}
            ),
            tool_call_id=call_id,
        ),
    ]


class StubDelegator(ReviewedDelegator):
    def __init__(self, approvals: list[bool]) -> None:
        super().__init__(None, [])
        self.approvals = approvals
        self.worker_calls = 0

    async def _worker(
        self,
        envelope,
        feedback,
        supervisor_messages=None,
        attempt=1,
    ):
        self.worker_calls += 1
        return WorkerOutput(
            result={"goal": f"attempt-{self.worker_calls}"},
            artifact_ref=None,
        )

    async def _evaluator(
        self,
        envelope,
        worker_output,
        supervisor_messages=None,
        attempt=1,
        structure_error="",
        reviewer_feedback="",
    ):
        approved = self.approvals[self.worker_calls - 1]
        return ReviewOutput(approved=approved, feedback="retry")


class FakeSupervisor:
    def __init__(self, result):
        self.result = result

    async def astream(self, _input, stream_mode):
        yield "custom", {
            "type": "commitment_messages",
            "actor": "worker",
            "stage": 1,
            "messages": [AIMessage(content="done")],
        }
        yield "values", self.result


class ScriptedToolModel(BaseChatModel):
    next_stage: int = 1

    @property
    def _llm_type(self):
        return "scripted-tool-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if messages and isinstance(messages[-1], ToolMessage):
            message = AIMessage(content="done")
        else:
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "delegate_with_review",
                        "args": {
                            "stage": self.next_stage,
                            "instruction": "stage",
                            "context": {},
                            "acceptance_criteria": [],
                        },
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self


class PlainModel(BaseChatModel):
    @property
    def _llm_type(self):
        return "plain-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="lead done"))]
        )

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self


class RecordingReviewModel(BaseChatModel):
    recorded_messages: list = []

    @property
    def _llm_type(self):
        return "recording-review-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.recorded_messages = list(messages)
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content='{"approved": true, "feedback": ""}'
                    )
                )
            ]
        )

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self


class AlwaysApprovedDelegator(ReviewedDelegator):
    def __init__(self):
        super().__init__(None, [])

    async def run(self, envelope, supervisor_messages=None):
        return WorkerOutput(result={"goal": "approved"}), ""


class NeverReturningDelegator(ReviewedDelegator):
    def __init__(self):
        super().__init__(None, [])

    async def run(self, envelope, supervisor_messages=None):
        await asyncio.Event().wait()


class FakeAgent:
    checkpointer = None
    store = None

    async def astream(self, *_args, **_kwargs):
        yield (
            "custom",
            {
                "type": "commitment_messages",
                "actor": "worker",
                "stage": 1,
                "messages": [HumanMessage(content="task")],
            },
        )
        yield (
            "values",
            {
                "__interrupt__": (
                    Interrupt(value={"stage": 3}, id="interrupt-1"),
                )
            },
        )


class FakeBridge:
    def __init__(self):
        self.events = []
        self.ended = False

    def publish(self, run_id, event):
        self.events.append((run_id, event))

    def publish_end(self, _run_id):
        self.ended = True

    def cleanup(self, _run_id, delay):
        self.cleanup_delay = delay


class FakeRunManager:
    def __init__(self):
        self.updates = []

    def update(self, run_id, **values):
        self.updates.append((run_id, values))


class CommitmentPocTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_output_tokens_stream_without_private_reasoning(self):
        class TokenAgent:
            async def astream(self, *_args, **_kwargs):
                yield (
                    "messages",
                    (
                        AIMessageChunk(
                            content="公开输出",
                            additional_kwargs={"reasoning_content": "私密推理"},
                        ),
                        {},
                    ),
                )
                yield "values", {"messages": [AIMessage(content="公开输出")]}

        events = []
        delegator = ReviewedDelegator(None, [])
        with patch(
            "caspian.agents.commitment.tracing.get_stream_writer",
            return_value=events.append,
        ):
            result = await delegator._stream_agent(
                TokenAgent(),
                [HumanMessage(content="task")],
                actor="worker",
                stage=1,
                stream_id="worker-1",
            )
        self.assertEqual(result["messages"][0].content, "公开输出")
        self.assertEqual(events[0]["type"], "commitment_messages")
        self.assertEqual(events[0]["messages"][0].content, "task")
        self.assertEqual(events[1]["messages"][0].content, "公开输出")
        self.assertNotIn("私密推理", str(events))

    def test_commitment_messages_use_real_message_array(self):
        events = []
        with patch(
            "caspian.agents.commitment.tracing.get_stream_writer",
            return_value=events.append,
        ):
            emit_commitment_messages(
                actor="worker",
                stage=2,
                attempt=1,
                messages=[AIMessage(content="ok")],
            )
        self.assertEqual(events[0]["type"], "commitment_messages")
        self.assertEqual(events[0]["messages"][0].content, "ok")

    async def test_context7_api_key_is_sent_as_mcp_header(self):
        with (
            patch.dict("os.environ", {"CONTEXT7_API_KEY": "ctx7sk-test"}),
            patch(
                "caspian.mcp.tools.load_mcp_tools",
                new_callable=AsyncMock,
                return_value=[],
            ) as load_tools,
        ):
            await get_context7_tools("https://mcp.context7.com/mcp")

        load_tools.assert_awaited_once_with(
            {
                "context7": {
                    "transport": "http",
                    "url": "https://mcp.context7.com/mcp",
                    "headers": {"Authorization": "Bearer ctx7sk-test"},
                }
            }
        )

    async def test_stage_three_evaluator_does_not_invent_prior_stage_priorities(self):
        model = RecordingReviewModel()
        delegator = ReviewedDelegator(model, [])
        await delegator._evaluator(
            TaskEnvelope(
                stage=3,
                instruction="分配优先级",
                context={},
                acceptance_criteria=["priority仅允许1到3"],
            ),
            WorkerOutput(
                result={
                    "requirements": [
                        {
                            "requirement": "包含测试和详细文档",
                            "priority": 2,
                        }
                    ]
                }
            ),
            supervisor_stage_messages(
                2,
                {"requirements": ["包含测试和详细文档"]},
            ),
        )
        system_prompt = "\n".join(
            str(message.content)
            for message in model.recorded_messages
            if message.type == "system"
        )
        self.assertIn("第二步不包含优先级", system_prompt)

    async def test_evaluator_receives_complete_worker_review_context(self):
        model = RecordingReviewModel()
        await ReviewedDelegator(model, [])._evaluator(
            TaskEnvelope(
                stage=3,
                instruction="分配优先级",
                context={"human_feedback": "人工要求"},
                acceptance_criteria=["逐项定级"],
            ),
            WorkerOutput(result={"requirements": []}),
            supervisor_stage_messages(2, {"requirements": []}),
            attempt=2,
            reviewer_feedback="上一轮审核反馈",
        )
        evaluator_message = next(
            message
            for message in model.recorded_messages
            if message.type == "human"
        )
        evaluator_input = json.loads(str(evaluator_message.content))
        self.assertEqual(
            evaluator_input["task"]["context"]["human_feedback"],
            "人工要求",
        )
        self.assertEqual(
            evaluator_input["reviewer_feedback"],
            "上一轮审核反馈",
        )
        self.assertEqual(
            evaluator_input["worker_output"]["result"],
            {"requirements": []},
        )
        self.assertIn("deterministic_validation_error", evaluator_input)
        self.assertEqual(
            len(evaluator_input["supervisor_messages"]),
            3,
        )

    def test_stage_three_normalizes_freeform_priorities_and_exact_text(self):
        normalized = _normalize_stage_three_result(
            WorkerOutput(
                result={
                    "requirements": [
                        {"text": "rewritten A", "priority": "high"},
                        {"text": "rewritten B", "priority": "medium"},
                    ],
                    "notes": "extra",
                },
                reasoning_summary="按第二步保留要求逐项定级。",
            ),
            ["必须完成 A", "可以协商 B"],
        )
        self.assertEqual(
            normalized.result,
            {
                "requirements": [
                    {"requirement": "必须完成 A", "priority": 3},
                    {"requirement": "可以协商 B", "priority": 2},
                ]
            },
        )
        self.assertEqual(
            normalized.reasoning_summary,
            "按第二步保留要求逐项定级。",
        )

    async def test_stage_five_calls_context7_once_per_selected_technology(self):
        resolve_calls = []
        query_calls = []

        @tool("resolve-library-id")
        async def resolver(libraryName: str, query: str) -> str:
            """Resolve one technology."""
            resolve_calls.append(libraryName)
            return f"Context7-compatible library ID: /official/{libraryName}"

        @tool("query-docs")
        async def query_docs(libraryId: str, query: str) -> str:
            """Query official version documentation."""
            query_calls.append(libraryId)
            return "Latest stable version: 1.2.3"

        class StageFiveDelegator(ReviewedDelegator):
            async def _invoke_schema(self, *, schema, **_kwargs):
                if schema.__name__ == "TechnologySelection":
                    return schema.model_validate(
                        {"technologies": ["React", "Next.js"]}
                    )
                raise AssertionError("stage 5 must not call a model assembler")

        output = await StageFiveDelegator(
            None, [resolver, query_docs]
        )._stage_five_worker(
            TaskEnvelope(
                stage=5,
                instruction="核验版本",
                context={},
                acceptance_criteria=[],
            ),
            "",
            supervisor_stage_messages(3, {"requirements": []}),
        )
        self.assertCountEqual(resolve_calls, ["React", "Next.js"])
        self.assertCountEqual(
            query_calls,
            ["/official/React", "/official/Next.js"],
        )
        self.assertEqual(
            [item["version"] for item in output.result["technologies"]],
            ["1.2.3", "1.2.3"],
        )
        self.assertEqual(
            {
                item["version_basis"]
                for item in output.result["technologies"]
            },
            {"official_docs_explicit"},
        )

    async def test_stage_five_evaluator_trusts_bounded_context7_provenance(self):
        model = RecordingReviewModel()
        await ReviewedDelegator(model, [])._evaluator(
            TaskEnvelope(
                stage=5,
                instruction="核验版本",
                context={},
                acceptance_criteria=["不得猜测版本"],
            ),
            WorkerOutput(
                result={
                    "technologies": [
                        {
                            "name": "React",
                            "project_version": "unresolved",
                            "version": "19.2",
                            "library_id": "/reactjs/react.dev",
                            "source_url": None,
                        }
                    ]
                }
            ),
        )
        system_prompt = "\n".join(
            str(message.content)
            for message in model.recorded_messages
            if message.type == "system"
        )
        self.assertIn("受控代码直接取自Context7", system_prompt)

    async def test_stage_five_defaults_unknown_version_to_latest_stable(self):
        @tool("resolve-library-id")
        async def resolver(libraryName: str, query: str) -> str:
            """Resolve one technology."""
            return (
                "Context7-compatible library ID: /tailwindlabs/tailwindcss.com\n"
                "Versions: __branch__v4-beta-docs"
            )

        @tool("query-docs")
        async def query_docs(libraryId: str, query: str) -> str:
            """Return documentation without an exact stable version."""
            return "Official installation documentation without a version."

        class StageFiveDelegator(ReviewedDelegator):
            async def _invoke_schema(self, *, schema, **_kwargs):
                return schema.model_validate(
                    {"technologies": ["Tailwind CSS"]}
                )

        output = await StageFiveDelegator(
            None,
            [resolver, query_docs],
        )._stage_five_worker(
            TaskEnvelope(
                stage=5,
                instruction="核验版本",
                context={},
                acceptance_criteria=[],
            ),
            "",
            supervisor_stage_messages(3, {"requirements": []}),
        )
        technology = output.result["technologies"][0]
        self.assertEqual(technology["version"], "latest-stable")
        self.assertEqual(
            technology["version_basis"],
            "latest_stable_policy",
        )

    def test_stage_three_must_copy_stage_two_requirements_exactly(self):
        messages = supervisor_stage_messages(
            2,
            {"requirements": ["包含测试和详细文档"]},
        )
        self.assertIsNone(
            _validate_stage_result(
                3,
                {
                    "requirements": [
                        {
                            "requirement": "包含测试和详细文档",
                            "priority": 2,
                        }
                    ]
                },
                messages,
            )
        )
        self.assertIsNotNone(
            _validate_stage_result(
                3,
                {
                    "requirements": [
                        {"requirement": "包含测试", "priority": 3}
                    ]
                },
                messages,
            )
        )

    def test_stage_three_excludes_discarded_stage_two_requirements(self):
        messages = supervisor_stage_messages(
            2,
            {
                "requirements": ["保留实时同步", "放弃纯静态限制"],
                "discarded_requirements": ["放弃纯静态限制"],
            },
        )
        self.assertIsNone(
            _validate_stage_result(
                3,
                {
                    "requirements": [
                        {"requirement": "保留实时同步", "priority": 3}
                    ]
                },
                messages,
            )
        )
        self.assertIsNotNone(
            _validate_stage_result(
                3,
                {
                    "requirements": [
                        {"requirement": "保留实时同步", "priority": 3},
                        {"requirement": "放弃纯静态限制", "priority": 1},
                    ]
                },
                messages,
            )
        )

    def test_default_switch_and_builder_are_unchanged(self):
        self.assertFalse(CommitmentConfig().enabled)
        self.assertEqual(
            [type(item).__name__ for item in build_general_middlewares()],
            ["UploadsMiddleware", "SandboxAuditMiddleware"],
        )

    def test_enabled_builder_inserts_commitment(self):
        sentinel = object()
        with patch(
            "caspian.agents.middlewares.builder.CommitmentMiddleware",
            return_value=sentinel,
        ):
            result = build_general_middlewares(
                commitment_enabled=True,
                model=object(),
                context7_tools=[],
            )
        self.assertIs(result[1], sentinel)
        self.assertEqual(type(result[0]).__name__, "UploadsMiddleware")
        self.assertEqual(type(result[2]).__name__, "SandboxAuditMiddleware")

    async def test_reviewed_delegator_retries_without_exposing_failures(self):
        delegator = StubDelegator([False, True])
        output, error = await delegator.run(
            TaskEnvelope(stage=1, instruction="goal")
        )
        self.assertEqual(error, "")
        self.assertEqual(output.result["goal"], "attempt-2")
        self.assertEqual(delegator.worker_calls, 2)

    async def test_reviewed_delegator_stops_after_three_failures(self):
        delegator = StubDelegator([False, False, False])
        output, error = await delegator.run(
            TaskEnvelope(stage=1, instruction="goal")
        )
        self.assertIsNone(output)
        self.assertEqual(error, "retry")
        self.assertEqual(delegator.worker_calls, 3)

    async def test_worker_schema_error_retries_inside_delegator(self):
        class SchemaRetryDelegator(ReviewedDelegator):
            def __init__(self):
                super().__init__(None, [])
                self.worker_calls = 0

            async def _worker(
                self,
                envelope,
                feedback,
                supervisor_messages=None,
                attempt=1,
            ):
                self.worker_calls += 1
                if self.worker_calls == 1:
                    return WorkerOutput.model_validate(
                        {"result": "# invalid contract"}
                    )
                self.assert_feedback = feedback
                return WorkerOutput(
                    result={"contract_markdown": "# Contract"}
                )

            async def _evaluator(
                self,
                envelope,
                worker_output,
                supervisor_messages=None,
                attempt=1,
                structure_error="",
                reviewer_feedback="",
            ):
                return ReviewOutput(approved=True)

        events = []
        delegator = SchemaRetryDelegator()
        with patch(
            "caspian.agents.commitment.tracing.get_stream_writer",
            return_value=events.append,
        ):
            output, error = await delegator.run(
                TaskEnvelope(stage=7, instruction="contract")
            )

        self.assertEqual(error, "")
        self.assertEqual(output.result["contract_markdown"], "# Contract")
        self.assertEqual(delegator.worker_calls, 2)
        self.assertIn("result.contract_markdown", delegator.assert_feedback)
        self.assertEqual(events[0]["actor"], "evaluator")
        review = json.loads(events[0]["messages"][1].content)
        self.assertFalse(review["approved"])

    async def test_unexpected_tool_error_is_not_wrapped_as_tool_message(self):
        class ExplodingDelegator(ReviewedDelegator):
            def __init__(self):
                super().__init__(None, [])

            async def run(self, envelope, supervisor_messages=None):
                raise RuntimeError("unexpected failure")

        supervisor = _build_supervisor(ExplodingDelegator())
        with self.assertRaisesRegex(RuntimeError, "unexpected failure"):
            await supervisor.ainvoke(
                {
                    "messages": [HumanMessage(content="start")],
                    "stage": 0,
                    "awaiting_human": None,
                    "artifacts": {},
                    "source_text": "goal",
                    "thread_id": "error-thread",
                    "knowledge_files": [],
                }
            )

    async def test_evaluator_controls_retry_without_supervisor_trace(self):
        events = []
        delegator = StubDelegator([False, True])
        with patch(
            "caspian.agents.commitment.tracing.get_stream_writer",
            return_value=events.append,
        ):
            await delegator.run(
                TaskEnvelope(
                    stage=1,
                    instruction="goal",
                    acceptance_criteria=["目标明确", "边界完整"],
                )
            )
        self.assertEqual(delegator.worker_calls, 2)
        self.assertEqual(events, [])

    async def test_delegate_tool_advances_exactly_one_stage(self):
        model = ScriptedToolModel(next_stage=1)
        agent = create_agent(
            model=model,
            tools=[
                build_delegate_with_review_tool(AlwaysApprovedDelegator())
            ],
            state_schema=CommitmentState,
        )
        result = await agent.ainvoke(
            {
                "messages": [HumanMessage(content="start")],
                "stage": 0,
                "artifacts": {},
                "source_text": "goal",
                "thread_id": "thread-1",
            }
        )
        self.assertEqual(result["stage"], 1)
        self.assertEqual(result["artifacts"]["1"]["goal"], "approved")
        tool_index = next(
            index
            for index, message in enumerate(result["messages"])
            if isinstance(message, ToolMessage)
        )
        tool_message = result["messages"][tool_index]
        supervisor_message = result["messages"][tool_index - 1]
        payload = json.loads(tool_message.content)
        self.assertEqual(
            set(payload),
            {"status", "stage", "result", "artifact_ref"},
        )
        self.assertEqual(
            supervisor_message.tool_calls[0]["id"],
            tool_message.tool_call_id,
        )

    async def test_stage_timeout_becomes_human_revision_instead_of_hanging(self):
        supervisor = _build_supervisor(NeverReturningDelegator())
        with patch(
            "caspian.agents.commitment.stage_rules._STAGE_TIMEOUT_SECONDS",
            0.01,
        ):
            result = await asyncio.wait_for(
                supervisor.ainvoke(
                    {
                        "messages": [HumanMessage(content="start")],
                        "stage": 0,
                        "awaiting_human": None,
                        "artifacts": {},
                        "source_text": "goal",
                        "thread_id": "timeout-thread",
                        "knowledge_files": [],
                    }
                ),
                timeout=0.2,
            )
        payload = result["__interrupt__"][0].value
        self.assertEqual(payload["stage"], 1)
        self.assertEqual(payload["allowed_decisions"], ["revise"])
        self.assertIn("超时", payload["error"])

    async def test_human_feedback_does_not_append_supervisor_messages(self):
        class FeedbackDelegator(ReviewedDelegator):
            def __init__(self):
                super().__init__(None, [])
                self.feedbacks = []

            async def run(self, envelope, supervisor_messages=None):
                self.feedbacks.append(
                    envelope.context.get("human_feedback", "")
                )
                return WorkerOutput(result={"goal": "approved"}), ""

        delegator = FeedbackDelegator()
        supervisor = _build_supervisor(delegator)
        supervisor.checkpointer = InMemorySaver()
        config = {"configurable": {"thread_id": "feedback-thread"}}
        first = await supervisor.ainvoke(
            {
                "messages": [HumanMessage(content="continue")],
                "stage": 2,
                "awaiting_human": None,
                "artifacts": {},
                "source_text": "goal",
                "thread_id": "feedback-thread",
            },
            config=config,
        )
        self.assertEqual(first["__interrupt__"][0].value["stage"], 3)
        self.assertEqual(len(first["messages"]), 3)
        first_ids = [message.id for message in first["messages"]]

        resumed = await supervisor.ainvoke(
            Command(
                resume={
                    "decision": "revise",
                    "feedback": "human-only-feedback",
                }
            ),
            config=config,
        )
        self.assertEqual(resumed["__interrupt__"][0].value["stage"], 3)
        self.assertEqual(resumed["stage"], 3)
        self.assertEqual(len(resumed["messages"]), 3)
        self.assertEqual(
            [message.id for message in resumed["messages"]],
            first_ids,
        )
        self.assertEqual(delegator.feedbacks[-1], "human-only-feedback")
        self.assertNotIn(
            "human-only-feedback",
            json.dumps(
                [
                    message.model_dump()
                    for message in resumed["messages"]
                ],
                ensure_ascii=False,
                default=str,
            ),
        )

    async def test_stage_seven_writes_only_the_approved_edited_contract(self):
        class ContractDelegator(ReviewedDelegator):
            def __init__(self):
                super().__init__(None, [])

            async def run(self, envelope, supervisor_messages=None):
                self.assert_stage = envelope.stage
                return (
                    WorkerOutput(
                        result={"contract_markdown": "# Draft contract"}
                    ),
                    "",
                )

            async def _evaluator(
                self,
                envelope,
                worker_output,
                supervisor_messages=None,
                attempt=1,
                structure_error="",
                reviewer_feedback="",
            ):
                return ReviewOutput(
                    approved=True,
                    reasoning_summary="编辑后的合同满足验收条件。",
                )

        supervisor = _build_supervisor(ContractDelegator())
        supervisor.checkpointer = InMemorySaver()
        config = {"configurable": {"thread_id": "contract-review-thread"}}
        initial_state = {
            "messages": [HumanMessage(content="continue")],
            "stage": 6,
            "awaiting_human": None,
            "artifacts": {"6": {"knowledge": []}},
            "source_text": "goal",
            "thread_id": "contract-review-thread",
            "knowledge_files": [],
        }

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "caspian.agents.commitment.artifacts._PROJECT_ROOT",
            Path(temp_dir),
        ):
            first = await supervisor.ainvoke(initial_state, config=config)
            contract_path = (
                Path(temp_dir)
                / "requirements"
                / "contract-review-thread"
                / "task-contract.md"
            )
            self.assertEqual(first["__interrupt__"][0].value["stage"], 7)
            self.assertFalse(contract_path.exists())
            self.assertEqual(len(first["messages"]), 3)
            stage_seven_ids = [
                message.id for message in first["messages"]
            ]

            revised = await supervisor.ainvoke(
                Command(
                    resume={
                        "decision": "revise",
                        "replacement": {
                            "contract_markdown": "# Edited contract"
                        },
                    }
                ),
                config=config,
            )
            self.assertEqual(
                revised["__interrupt__"][0]
                .value["draft"]["contract_markdown"],
                "# Edited contract",
            )
            self.assertFalse(contract_path.exists())
            self.assertEqual(len(revised["messages"]), 3)
            self.assertEqual(
                [message.id for message in revised["messages"]],
                stage_seven_ids,
            )

            completed = await supervisor.ainvoke(
                Command(resume={"decision": "approve"}),
                config=config,
            )
            self.assertEqual(completed["stage"], 9)
            self.assertEqual(completed["task_contract"], "# Edited contract")
            self.assertEqual(
                contract_path.read_text(encoding="utf-8"),
                "# Edited contract\n",
            )
            stage_seven_calls = [
                call
                for message in completed["messages"]
                if isinstance(message, AIMessage)
                for call in message.tool_calls
                if call["args"]["stage"] == 7
            ]
            stage_seven_tools = [
                message
                for message in completed["messages"]
                if isinstance(message, ToolMessage)
                and json.loads(message.content).get("stage") == 7
            ]
            self.assertEqual(len(stage_seven_calls), 1)
            self.assertEqual(len(stage_seven_tools), 1)
            self.assertEqual(
                stage_seven_calls[0]["id"],
                stage_seven_tools[0].tool_call_id,
            )

    async def test_human_replacement_is_validated(self):
        state = {
            "artifacts": {
                "2": {
                    "requirements": ["must"],
                    "discarded_requirements": [],
                }
            },
            "messages": supervisor_stage_messages(
                2,
                {
                    "requirements": ["must"],
                    "discarded_requirements": [],
                },
            ),
        }
        revised, error = await _review_human_revision(
            {
                "decision": "revise",
                "replacement": {
                    "requirements": [{"text": "must", "priority": 3}]
                },
            },
            3,
            state,
            StubDelegator([True]),
        )
        self.assertEqual(error, "")
        self.assertEqual(revised["requirements"][0]["priority"], 3)

        rejected, error = await _review_human_revision(
            {
                "decision": "revise",
                "replacement": {
                    "requirements": [{"text": "must", "priority": 3}]
                },
            },
            3,
            state,
            StubDelegator([False]),
        )
        self.assertIsNone(rejected)
        self.assertEqual(error, "retry")

    def test_path_segments_reject_traversal(self):
        for value in ("", ".", "..", "../escape", "a/b"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _safe_segment(value, "segment")

    def test_unresolved_versions_cannot_be_approved(self):
        self.assertTrue(
            _contains_unresolved_versions(
                {"technologies": [{"name": "LangGraph", "version": "unresolved"}]}
            )
        )
        self.assertFalse(
            _contains_unresolved_versions(
                {"technologies": [{"name": "LangGraph", "version": "1.2.6"}]}
            )
        )
        self.assertFalse(
            _contains_unresolved_versions(
                {
                    "technologies": [
                        {"name": "LangGraph", "version": "latest-stable"}
                    ]
                }
            )
        )

    def test_knowledge_stages_have_a_longer_timeout(self):
        self.assertEqual(_stage_timeout(4), 600)
        self.assertEqual(_stage_timeout(5), 900)
        self.assertEqual(_stage_timeout(6), 900)

    def test_stage_five_rejects_an_empty_technology_list(self):
        self.assertIsNotNone(
            _validate_stage_result(5, {"technologies": []})
        )

    def test_context7_stable_version_requires_explicit_stable_wording(self):
        self.assertEqual(
            _context7_stable_version(
                "Declares the latest stable version of React as 19.2."
            ),
            "19.2",
        )
        self.assertIsNone(
            _context7_stable_version(
                "Current version is 16.3.0-canary.80."
            )
        )

    def test_context7_candidate_version_ignores_prereleases(self):
        evidence = """
        Context7-compatible library ID: /vercel/next.js
        Versions: v16.1.6, v16.2.9, v16.3.0-canary.80, v15.1.11
        """
        self.assertEqual(
            _context7_candidate_version(evidence, "/vercel/next.js"),
            "16.2.9",
        )
        self.assertIsNone(
            _context7_candidate_version(
                """
                Context7-compatible library ID: /tailwindlabs/tailwindcss.com
                Versions: __branch__v4-beta-docs
                """,
                "/tailwindlabs/tailwindcss.com",
            )
        )

    def test_stage_two_requires_compatibility_evidence(self):
        self.assertIsNotNone(
            _validate_stage_result(
                2,
                {
                    "requirements": ["web", "desktop UI"],
                    "compatibility_checks": [],
                    "conflicts": [],
                },
            )
        )
        self.assertIsNotNone(
            _validate_stage_result(
                2,
                {
                    "requirements": ["web", "desktop UI"],
                    "compatibility_checks": [
                        {
                            "technology": "desktop UI",
                            "application_type": "web",
                            "ui_surface": "browser",
                            "runtime_platform": "server",
                            "host_model": "web server",
                            "status": "conflict",
                        }
                    ],
                    "conflicts": [],
                },
            )
        )
        self.assertIsNone(
            _validate_stage_result(
                2,
                {
                    "requirements": ["web", "desktop UI"],
                    "compatibility_checks": [
                        {
                            "technology": "desktop UI",
                            "application_type": "desktop",
                            "ui_surface": "native window",
                            "runtime_platform": "Windows",
                            "host_model": "desktop process",
                            "status": "conflict",
                        }
                    ],
                    "conflicts": [
                        {
                            "requirements": ["web", "desktop UI"],
                            "conflict_type": "ui_surface",
                            "explanation": "browser and native window differ",
                            "status": "open",
                        }
                    ],
                },
            )
        )
        self.assertTrue(
            _has_open_conflicts(
                {
                    "compatibility_checks": [{"status": "conflict"}],
                    "conflicts": [{"status": "open"}],
                }
            )
        )
        self.assertFalse(
            _has_open_conflicts(
                {
                    "compatibility_checks": [{"status": "verified"}],
                    "conflicts": [
                        {"status": "resolved", "resolution": "use web UI"}
                    ],
                }
            )
        )

    def test_stage_two_open_conflict_cannot_advance_to_priorities(self):
        draft = {
            "requirements": ["UI 技术兼容性评估"],
            "compatibility_checks": [
                {
                    "technology": "SunnyUI",
                    "application_type": "desktop",
                    "ui_surface": "WinForms",
                    "runtime_platform": "Windows",
                    "host_model": "desktop process",
                    "status": "conflict",
                }
            ],
            "conflicts": [
                {
                    "requirements": ["UI 技术兼容性评估"],
                    "conflict_type": "ui_surface",
                    "explanation": "SunnyUI 的 WinForms 界面不能作为 MVC 浏览器界面。",
                    "status": "open",
                }
            ],
        }
        self.assertIsNone(_validate_stage_result(2, draft))
        payload = _human_payload(2, {"artifacts": {"2": draft}})
        self.assertEqual(payload["allowed_decisions"], ["revise"])
        self.assertEqual(payload["revise_label"], "解决矛盾")

    def test_stage_four_reference_candidates_require_human_confirmation(self):
        proposed = {
            "files": [
                {
                    "mention": "需求文档",
                    "candidates": ["requirements-v2.md"],
                    "status": "proposed",
                }
            ],
            "urls": [
                {
                    "mention": "Next.js 文档",
                    "url": None,
                    "candidates": ["https://nextjs.org/docs"],
                    "source": "search",
                    "status": "proposed",
                }
            ],
        }
        self.assertIsNone(_validate_stage_result(4, proposed))
        self.assertTrue(_stage_four_needs_review(proposed))
        self.assertEqual(
            _human_payload(4, {"artifacts": {"4": proposed}})[
                "allowed_decisions"
            ],
            ["approve", "revise"],
        )

        unresolved = {
            "files": [
                {
                    "mention": "需求文档",
                    "candidates": [],
                    "status": "unresolved",
                }
            ],
            "urls": [],
        }
        payload = _human_payload(4, {"artifacts": {"4": unresolved}})
        self.assertEqual(payload["allowed_decisions"], ["revise"])
        self.assertEqual(payload["revise_label"], "补充引用")

        complete = {
            "files": [
                {
                    "mention": "requirements-v2.md",
                    "uploaded_filename": "requirements-v2.md",
                    "candidates": [],
                    "status": "matched",
                }
            ],
            "urls": [
                {
                    "mention": "https://nextjs.org/docs",
                    "url": "https://nextjs.org/docs",
                    "candidates": [],
                    "source": "user",
                    "status": "provided",
                }
            ],
        }
        self.assertIsNone(_validate_stage_result(4, complete))
        self.assertFalse(_stage_four_needs_review(complete))

    def test_stage_four_filters_technology_usage_but_keeps_reference_intent(self):
        output = WorkerOutput(
            result={
                "files": [],
                "urls": [
                    {
                        "mention": "React",
                        "url": None,
                        "candidates": ["https://react.dev/"],
                        "source": "search",
                        "status": "proposed",
                    }
                ],
            }
        )
        self.assertEqual(
            _filter_stage_four_result(
                output,
                "项目必须使用 React 开发。",
            ).result["urls"],
            [],
        )
        self.assertEqual(
            len(
                _filter_stage_four_result(
                    output,
                    "请参考 React 官方文档完成实现。",
                ).result["urls"]
            ),
            1,
        )


    def test_reference_search_parser_returns_destination_url(self):
        parser = _SearchResultParser()
        parser.feed(
            '<a class="result__a" href="//duckduckgo.com/l/?uddg='
            'https%3A%2F%2Fnextjs.org%2Fdocs">Next.js Docs</a>'
        )
        self.assertEqual(
            parser.results,
            [{"title": "Next.js Docs", "url": "https://nextjs.org/docs"}],
        )

    def test_structured_output_parses_plain_or_fenced_json(self):
        for content in (
            '{"approved": true, "feedback": ""}',
            '```json\n{"approved": true, "feedback": ""}\n```',
            '说明如下：\n```json\n{"approved": true, "feedback": ""}\n```',
            '{"approved": false, "feedback": "请把"version"改正"}',
        ):
            parsed = _extract_structured(
                {"messages": [AIMessage(content=content)]},
                ReviewOutput,
            )
            self.assertEqual(parsed.approved, '"approved": true' in content)

    def test_knowledge_contract_and_final_message(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "caspian.agents.commitment.artifacts._PROJECT_ROOT",
                Path(temp_dir),
            ):
                files = _write_knowledge(
                    {
                        "knowledge": [
                            {
                                "technology": "langgraph",
                                "version": "1.2.6",
                                "source_url": "https://docs.langchain.com/langgraph",
                                "content": "Interrupt uses Command(resume=...).",
                            }
                        ]
                    }
                )
                contract, contract_ref = _write_contract(
                    "thread-1", {"contract_markdown": "# Contract"}
                )
                message = _build_final_message(contract, files)
                self.assertTrue((Path(temp_dir) / files[0]).is_file())
                self.assertTrue((Path(temp_dir) / contract_ref).is_file())
                self.assertIn("<task_contract>", message)
                self.assertIn("<theoretical foundation", message)

    def test_knowledge_filenames_slugify_technology_display_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "caspian.agents.commitment.artifacts._PROJECT_ROOT",
                Path(temp_dir),
            ):
                files = _write_knowledge(
                    {
                        "knowledge": [
                            {
                                "technology": "Tailwind CSS",
                                "version": "latest-stable",
                                "source_url": "https://tailwindcss.com/docs",
                                "content": "Official docs.",
                            },
                            {
                                "technology": "shadcn/ui",
                                "version": "3.5.0",
                                "source_url": "https://ui.shadcn.com/docs",
                                "content": "Official docs.",
                            },
                        ]
                    }
                )
                self.assertEqual(
                    files,
                    [
                        "knowledge/Tailwind-CSS-latest-stable.md",
                        "knowledge/shadcn-ui-3.5.0.md",
                    ],
                )
                self.assertIn(
                    "# shadcn/ui 3.5.0",
                    (Path(temp_dir) / files[1]).read_text(encoding="utf-8"),
                )

    async def test_middleware_returns_only_contract_message(self):
        middleware = object.__new__(CommitmentMiddleware)
        middleware._supervisor = FakeSupervisor(
            {
                "stage": 9,
                "task_contract": "# Contract",
                "final_message": "<task_contract># Contract</task_contract>",
            }
        )
        runtime = SimpleNamespace(
            execution_info=SimpleNamespace(thread_id="thread-1")
        )
        batches = []
        with patch(
            "caspian.agents.commitment.middleware._write_commitment_messages",
            side_effect=batches.append,
        ):
            update = await middleware._run(
                {"messages": [HumanMessage(content="raw goal")]},
                runtime,
            )
        self.assertEqual(update["task_contract"], "# Contract")
        self.assertEqual(len(update["messages"]), 2)
        self.assertIsInstance(update["messages"][0], RemoveMessage)
        self.assertIsInstance(update["messages"][1], HumanMessage)
        self.assertNotIn("Worker", update["messages"][1].content)
        self.assertTrue(
            all(batch["type"] == "commitment_messages" for batch in batches)
        )

    async def test_nested_interrupt_reaches_parent_and_resumes(self):
        async def pause_for_review(_state):
            interrupt({"type": "commitment_review", "stage": 3})
            return {
                "stage": 9,
                "task_contract": "# Contract",
                "final_message": "<task_contract># Contract</task_contract>",
            }

        builder = StateGraph(CommitmentState)
        builder.add_node("pause_for_review", pause_for_review)
        builder.add_edge(START, "pause_for_review")
        builder.add_edge("pause_for_review", END)

        middleware = object.__new__(CommitmentMiddleware)
        middleware._supervisor = builder.compile()
        agent = create_agent(
            model=PlainModel(),
            tools=[],
            middleware=[middleware],
            state_schema=CommitmentState,
            checkpointer=InMemorySaver(),
        )
        config = {"configurable": {"thread_id": "nested-interrupt"}}

        paused = await agent.ainvoke(
            {"messages": [HumanMessage(content="goal")]},
            config=config,
        )
        self.assertEqual(paused["__interrupt__"][0].value["stage"], 3)

        resumed = await agent.ainvoke(
            Command(resume={"decision": "approve"}),
            config=config,
        )
        self.assertEqual(resumed["task_contract"], "# Contract")
        self.assertEqual(resumed["messages"][-1].content, "lead done")

    def test_resume_request_and_graph_input(self):
        resume = {"decision": "approve"}
        request = RunCreateRequest(resume=resume)
        graph_input = _build_graph_input(request)
        self.assertIsInstance(graph_input, Command)
        self.assertEqual(graph_input.resume, resume)
        with self.assertRaises(ValueError):
            RunCreateRequest()

    def test_interrupt_serialization(self):
        chunk = (
            "values",
            {"__interrupt__": (Interrupt(value={"stage": 3}, id="abc"),)},
        )
        self.assertEqual(
            _extract_interrupts(chunk),
            [{"id": "abc", "value": {"stage": 3}}],
        )

    async def test_worker_marks_graph_interrupt(self):
        record = SimpleNamespace(
            run_id="run-1",
            thread_id="thread-1",
            model_name=None,
            abort_event=asyncio.Event(),
            abort_action="interrupt",
        )
        bridge = FakeBridge()
        manager = FakeRunManager()
        config = SimpleNamespace(
            models=[],
            commitment=SimpleNamespace(enabled=True),
        )
        with patch(
            "caspian.runtime.runs.worker.make_lead_agent",
            AsyncMock(return_value=FakeAgent()),
        ):
            await run_agent(
                record=record,
                bridge=bridge,
                run_manager=manager,
                app_config=config,
                graph_input={"messages": [HumanMessage(content="goal")]},
                runnable_config={"configurable": {"thread_id": "thread-1"}},
                stream_modes=["values"],
            )
        self.assertEqual(manager.updates[-1][1]["status"], RunStatus.interrupted)
        self.assertEqual(bridge.events[-1][1].event, "interrupt")
        self.assertEqual(bridge.events[-1][1].data["id"], "interrupt-1")
        self.assertEqual(bridge.events[-2][1].event, "events")
        self.assertEqual(
            bridge.events[-2][1].data["type"],
            "commitment_messages",
        )
        self.assertEqual(bridge.events[-2][1].data["actor"], "worker")
        self.assertTrue(bridge.ended)


if __name__ == "__main__":
    unittest.main()
