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
from langchain.messages import HumanMessage
from langchain.tools import tool
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
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
    _commit_instruction,
    _contains_unresolved_versions,
    _context7_candidate_version,
    _context7_stable_version,
    _extract_structured,
    _extract_uploads_tag,
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
        self.inputs = []

    async def astream(self, graph_input, stream_mode, **kwargs):
        self.inputs.append(graph_input)
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


class ScriptedStreamAgent:
    def __init__(self, runs):
        self.runs = runs
        self.calls = 0

    async def astream(self, _input, stream_mode):
        run = self.runs[self.calls]
        self.calls += 1
        for item in run:
            if isinstance(item, Exception):
                raise item
            yield item


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
                        content='{"approved": true, "feedback": ""}',
                    )
                )
            ]
        )

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self

    def with_structured_output(self, schema, **kwargs):
        model = self

        class StructuredReviewModel:
            async def ainvoke(self, messages):
                model.recorded_messages = list(messages)
                parsed = schema(approved=True, feedback="")
                return {
                    "raw": AIMessage(
                        content=parsed.model_dump_json(),
                        response_metadata={"finish_reason": "stop"},
                    ),
                    "parsed": parsed,
                    "parsing_error": None,
                }

        return StructuredReviewModel()


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

    async def test_worker_stage_two_prompt_enforces_discard_instructions(self):
        captured = {}

        def fake_create_agent(**kwargs):
            captured["system_prompt"] = kwargs.get("system_prompt", "")
            return object()

        class RecordingDelegator(ReviewedDelegator):
            def __init__(self):
                super().__init__(PlainModel(), [])

            async def _stream_agent(self, agent, messages, **kwargs):
                return {
                    "messages": [
                        AIMessage(
                            content=(
                                '{"result": {"requirements": ["X"], '
                                '"discarded_requirements": [], '
                                '"compatibility_checks": [], "conflicts": []}}'
                            )
                        )
                    ]
                }

        delegator = RecordingDelegator()
        with patch(
            "caspian.agents.commitment.delegation.create_agent",
            side_effect=fake_create_agent,
        ):
            await delegator._worker(
                TaskEnvelope(
                    stage=2,
                    instruction="汇总要求",
                    context={"human_feedback": "放弃离线"},
                    acceptance_criteria=[],
                ),
                "",
            )
        self.assertIn("human_feedback", captured["system_prompt"])
        self.assertIn("放弃", captured["system_prompt"])
        self.assertIn("discarded_requirements", captured["system_prompt"])
        self.assertIn("简称", captured["system_prompt"])

    async def test_evaluator_prompt_rejects_unenforced_discard_instructions(self):
        model = RecordingReviewModel()
        await ReviewedDelegator(model, [])._evaluator(
            TaskEnvelope(
                stage=2,
                instruction="汇总要求",
                context={},
                acceptance_criteria=[],
            ),
            WorkerOutput(
                result={
                    "requirements": ["X"],
                    "discarded_requirements": [],
                    "compatibility_checks": [],
                    "conflicts": [],
                }
            ),
            [HumanMessage(content="task")],
        )
        system_prompt = "\n".join(
            str(message.content)
            for message in model.recorded_messages
            if message.type == "system"
        )
        self.assertIn("human_feedback", system_prompt)
        self.assertIn("放弃", system_prompt)
        self.assertIn("审核不通过", system_prompt)

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

    async def test_evaluator_uses_json_mode_without_structured_tool_call(self):
        class JsonModeOnlyModel(PlainModel):
            structured_kwargs: dict | None = None
            bound_kwargs: dict | None = None

            def bind_tools(self, tools, *, tool_choice=None, **kwargs):
                raise AssertionError("Evaluator must not use ToolStrategy")

            def bind(self, **kwargs):
                self.bound_kwargs = kwargs
                return self

            def with_structured_output(self, schema, **kwargs):
                self.structured_kwargs = kwargs

                class StructuredModel:
                    async def ainvoke(self, messages):
                        parsed = schema(
                            approved=True,
                            feedback="",
                            reasoning_summary="JSON mode result",
                        )
                        return {
                            "raw": AIMessage(
                                content=parsed.model_dump_json(),
                                response_metadata={"finish_reason": "stop"},
                            ),
                            "parsed": parsed,
                            "parsing_error": None,
                        }

                return StructuredModel()

        model = JsonModeOnlyModel()
        review = await ReviewedDelegator(model, [])._evaluator(
            TaskEnvelope(stage=4, instruction="汇总必要输入"),
            WorkerOutput(result={"files": [], "urls": []}),
        )

        self.assertTrue(review.approved)
        self.assertEqual(model.structured_kwargs["method"], "json_mode")
        self.assertTrue(model.structured_kwargs["include_raw"])
        self.assertEqual(
            model.bound_kwargs,
            {"max_tokens": 8192, "reasoning_effort": "low"},
        )

    async def test_evaluator_recovers_from_empty_json_mode_with_plain_json(self):
        class EmptyThenPlainJsonModel(PlainModel):
            structured_calls: int = 0
            plain_calls: int = 0
            plain_messages: list | None = None

            def bind(self, **kwargs):
                return self

            def with_structured_output(self, schema, **kwargs):
                owner = self

                class EmptyStructuredModel:
                    async def ainvoke(self, messages):
                        owner.structured_calls += 1
                        return {
                            "raw": AIMessage(
                                content="",
                                response_metadata={"finish_reason": "stop"},
                            ),
                            "parsed": None,
                            "parsing_error": None,
                        }

                return EmptyStructuredModel()

            async def ainvoke(self, messages, config=None, **kwargs):
                self.plain_calls += 1
                self.plain_messages = messages
                return AIMessage(
                    content=ReviewOutput(
                        approved=True,
                        feedback="",
                        reasoning_summary="plain JSON recovery result",
                    ).model_dump_json(),
                    response_metadata={"finish_reason": "stop"},
                )

        model = EmptyThenPlainJsonModel()
        review = await ReviewedDelegator(model, [])._evaluator(
            TaskEnvelope(stage=2, instruction="汇总要求与矛盾"),
            WorkerOutput(result={"requirements": ["必须完成 A"]}),
        )

        self.assertTrue(review.approved)
        self.assertEqual(model.structured_calls, 1)
        self.assertEqual(model.plain_calls, 1)
        recovery_prompt = "\n".join(
            str(message.content) for message in model.plain_messages
        )
        self.assertIn("JSON", recovery_prompt)
        self.assertIn('"approved": false', recovery_prompt)
        self.assertIn("立即只返回最终 JSON", recovery_prompt)

    def test_stage_three_joins_reordered_priority_assignments_by_stable_id(self):
        normalized = _normalize_stage_three_result(
            WorkerOutput(
                result={
                    "priority_assignments": [
                        {"requirement_id": "R2", "priority": 2},
                        {"requirement_id": "R1", "priority": 3},
                    ]
                },
                reasoning_summary="模型自由说明不作为关联依据。",
            ),
            ["必须完成 A", "  保留原文空格与标点；不得改写。  "],
        )
        self.assertEqual(
            normalized.result,
            {
                "requirements": [
                    {"requirement": "必须完成 A", "priority": 3},
                    {
                        "requirement": "  保留原文空格与标点；不得改写。  ",
                        "priority": 2,
                    },
                ]
            },
        )
        self.assertIn("R1=3", normalized.reasoning_summary)
        self.assertIn("R2=2", normalized.reasoning_summary)

    def test_stage_three_rejects_invalid_priority_assignments_without_inference(self):
        requirements = ["必须完成 A", "代码尽量简单"]
        messages = supervisor_stage_messages(2, {"requirements": requirements})
        cases = [
            ([{"requirement_id": "R1", "priority": 3}], "缺少"),
            (
                [
                    {"requirement_id": "R1", "priority": 3},
                    {"requirement_id": "R1", "priority": 2},
                ],
                "重复",
            ),
            (
                [
                    {"requirement_id": "R1", "priority": 3},
                    {"requirement_id": "R3", "priority": 2},
                ],
                "未知",
            ),
            (
                [
                    {"requirement_id": "R1", "priority": 3},
                    {"requirement_id": "R2", "priority": "medium"},
                ],
                "整数 1、2、3",
            ),
        ]
        for assignments, expected_error in cases:
            with self.subTest(assignments=assignments):
                normalized = _normalize_stage_three_result(
                    WorkerOutput(result={"priority_assignments": assignments}),
                    requirements,
                )
                error = _validate_stage_result(3, normalized.result, messages)
                self.assertIsNotNone(error)
                self.assertIn(expected_error, error)

    async def test_stage_three_worker_sends_stable_ids_and_only_accepts_assignments(self):
        class RecordingDelegator(ReviewedDelegator):
            def __init__(self):
                super().__init__(PlainModel(), [])
                self.prompt = None

            async def _stream_agent(self, agent, messages, **kwargs):
                self.prompt = json.loads(str(messages[-1].content))
                final = AIMessage(
                    content=json.dumps(
                        {
                            "result": {
                                "priority_assignments": [
                                    {"requirement_id": "R1", "priority": 3}
                                ]
                            }
                        }
                    )
                )
                return {"messages": [final]}

        delegator = RecordingDelegator()
        with patch(
            "caspian.agents.commitment.delegation.create_agent",
            return_value=object(),
        ):
            output = await delegator._worker(
                TaskEnvelope(
                    stage=3,
                    instruction="分配优先级",
                    context={
                        "decision_table_rows": [
                            {"requirement": "必须使用 Supabase", "priority": 3}
                        ]
                    },
                ),
                "",
                supervisor_stage_messages(
                    2,
                    {
                        "requirements": ["改用 SQLite 存储"],
                        "table_conflicts": [
                            {
                                "requirement": "改用 SQLite 存储",
                                "table_requirement": "必须使用 Supabase",
                                "table_priority": 3,
                                "explanation": "两种存储方案冲突",
                            }
                        ],
                    },
                ),
            )

        self.assertEqual(
            delegator.prompt["priority_requirements"],
            [{"requirement_id": "R1", "requirement": "改用 SQLite 存储"}],
        )
        self.assertEqual(
            output.result["requirements"],
            [{"requirement": "改用 SQLite 存储", "priority": 3}],
        )
        self.assertEqual(len(output.result["table_escalations"]), 1)

    async def test_worker_binds_json_object_response_format_without_tool_choice(self):
        from langchain_core.language_models.chat_models import _ChatModelBinding

        captured = {}

        class RecordingDelegator(ReviewedDelegator):
            def __init__(self):
                super().__init__(PlainModel(), [])

            async def _stream_agent(self, agent, messages, **kwargs):
                return {
                    "messages": [
                        AIMessage(content='{"result": {"goal": "X"}}')
                    ]
                }

        def fake_create_agent(**kwargs):
            captured["model"] = kwargs["model"]
            captured["response_format"] = kwargs.get("response_format", None)
            return object()

        delegator = RecordingDelegator()
        with patch(
            "caspian.agents.commitment.delegation.create_agent",
            side_effect=fake_create_agent,
        ):
            output = await delegator._worker(
                TaskEnvelope(stage=1, instruction="明确目标", context={}),
                "",
            )

        self.assertIsNone(captured["response_format"])
        binding = captured["model"]
        self.assertIsInstance(binding, _ChatModelBinding)
        self.assertEqual(
            binding.kwargs.get("response_format"),
            {"type": "json_object"},
        )
        self.assertNotIn("tool_choice", binding.kwargs)
        self.assertEqual(output.result, {"goal": "X"})

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
            technology["library_id"],
            "/tailwindlabs/tailwindcss.com",
        )
        self.assertEqual(
            technology["version_basis"],
            "latest_stable_policy",
        )

    async def test_stage_six_resolves_missing_library_id_before_query(self):
        calls = []

        @tool("resolve-library-id")
        async def resolver(libraryName: str, query: str) -> str:
            """Resolve one technology."""
            calls.append(("resolve", libraryName))
            return "Context7-compatible library ID: /reactjs/react.dev"

        @tool("query-docs")
        async def query_docs(libraryId: str, query: str) -> str:
            """Return official implementation knowledge."""
            calls.append(("query", libraryId))
            return "Official React implementation documentation."

        class StageSixDelegator(ReviewedDelegator):
            async def _invoke_schema(self, *, schema, prompt, **_kwargs):
                self.prompt = prompt
                return schema.model_validate(
                    {
                        "result": {
                            "knowledge": [
                                {
                                    "technology": "React",
                                    "version": "latest-stable",
                                    "source_url": "https://react.dev/",
                                    "content": "Official docs.",
                                }
                            ]
                        }
                    }
                )

        delegator = StageSixDelegator(None, [resolver, query_docs])
        await delegator._stage_six_worker(
            TaskEnvelope(
                stage=6,
                instruction="提取知识",
                context={},
                acceptance_criteria=[],
            ),
            "",
            supervisor_stage_messages(
                5,
                {
                    "technologies": [
                        {
                            "name": "React",
                            "version": "latest-stable",
                            "library_id": None,
                        }
                    ]
                },
            ),
        )
        self.assertEqual(
            calls,
            [
                ("resolve", "React"),
                ("query", "/reactjs/react.dev"),
            ],
        )
        self.assertEqual(
            delegator.prompt["context7_evidence"][0]["library_id"],
            "/reactjs/react.dev",
        )

    async def test_stage_seven_evaluator_requires_controlled_revision_provenance(self):
        model = RecordingReviewModel()
        await ReviewedDelegator(model, [])._evaluator(
            TaskEnvelope(
                stage=7,
                instruction="生成任务合同",
                context={},
                acceptance_criteria=["只丢弃人工授权放弃的要求"],
            ),
            WorkerOutput(result={"contract_markdown": "# Contract"}),
            supervisor_stage_messages(
                2,
                {
                    "requirements": ["保留实时同步"],
                    "discarded_requirements": ["放弃纯静态前端"],
                },
            ),
        )
        system_prompt = "\n".join(
            str(message.content)
            for message in model.recorded_messages
            if message.type == "system"
        )
        self.assertIn("revision_provenance", system_prompt)
        self.assertIn("没有对应授权", system_prompt)

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
            [
                "ToolErrorMiddleware",
                "UploadsMiddleware",
                "DecisionTableMiddleware",
                "DecisionTableEditMiddleware",
                "DecisionTableGuardMiddleware",
                "SandboxAuditMiddleware",
            ],
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
        self.assertIs(result[4], sentinel)
        self.assertEqual(type(result[0]).__name__, "ToolErrorMiddleware")
        self.assertEqual(type(result[1]).__name__, "UploadsMiddleware")
        self.assertEqual(type(result[2]).__name__, "DecisionTableMiddleware")
        self.assertEqual(type(result[3]).__name__, "DecisionTableEditMiddleware")
        self.assertEqual(type(result[5]).__name__, "DecisionTableGuardMiddleware")
        self.assertEqual(type(result[6]).__name__, "SandboxAuditMiddleware")

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

    async def test_evaluator_schema_error_reaches_next_worker_and_stops_at_three(self):
        class InvalidEvaluatorDelegator(ReviewedDelegator):
            def __init__(self):
                super().__init__(PlainModel(), [])
                self.worker_feedback = []

            async def _stream_agent(self, agent, messages, **kwargs):
                return {"messages": [AIMessage(content="not valid json")]}

            async def _worker(
                self,
                envelope,
                feedback,
                supervisor_messages=None,
                attempt=1,
            ):
                self.worker_feedback.append(feedback)
                return WorkerOutput(result={"goal": "valid worker output"})

        delegator = InvalidEvaluatorDelegator()
        output, error = await delegator.run(
            TaskEnvelope(stage=1, instruction="goal")
        )

        self.assertIsNone(output)
        self.assertEqual(len(delegator.worker_feedback), 3)
        self.assertEqual(delegator.worker_feedback[0], "")
        self.assertIn("无法解析为审核结论", error)
        self.assertEqual(delegator.worker_feedback[1], error)
        self.assertEqual(delegator.worker_feedback[2], error)

    def test_parse_failure_logs_raw_output(self):
        from caspian.agents.commitment.delegation import _extract_structured_logged

        raw_text = "审核意见：该结果不完整，需要补充说明。"
        with patch(
            "caspian.agents.commitment.delegation.logger"
        ) as mock_logger:
            with self.assertRaises(ValueError):
                _extract_structured_logged(
                    {"messages": [AIMessage(content=raw_text)]},
                    ReviewOutput,
                    actor="evaluator",
                    stage=2,
                    attempt=1,
                )
        mock_logger.warning.assert_called_once()
        log_args = mock_logger.warning.call_args.args
        self.assertIn(raw_text, log_args[-1])
        self.assertIn("evaluator", log_args)
        self.assertIn("ReviewOutput", log_args)

    async def test_stream_agent_retries_http_transport_error_within_one_attempt(self):
        from httpx import RemoteProtocolError

        final = AIMessage(
            content='{"result":{"goal":"X"}}',
            response_metadata={"finish_reason": "stop"},
        )
        agent = ScriptedStreamAgent(
            [
                [RemoteProtocolError("incomplete chunked read")],
                [("messages", (final, {})), ("values", {"messages": [final]})],
            ]
        )

        result = await ReviewedDelegator(PlainModel(), [])._stream_agent(
            agent,
            [HumanMessage(content="input")],
            actor="worker",
            stage=1,
            stream_id="worker-1",
            attempt=2,
        )

        self.assertEqual(agent.calls, 2)
        self.assertEqual(result["messages"][-1].content, final.content)

    async def test_stream_agent_retries_reasoning_only_without_publishing_reasoning(self):
        empty = AIMessage(
            content="",
            additional_kwargs={"reasoning_content": "不得公开"},
            response_metadata={"finish_reason": "stop"},
        )
        final = AIMessage(
            content='{"result":{"goal":"X"}}',
            response_metadata={"finish_reason": "stop"},
        )
        agent = ScriptedStreamAgent(
            [
                [("messages", (empty, {})), ("values", {"messages": [empty]})],
                [("messages", (final, {})), ("values", {"messages": [final]})],
            ]
        )
        published = []

        with patch(
            "caspian.agents.commitment.delegation.emit_commitment_messages",
            side_effect=lambda **kwargs: published.extend(kwargs["messages"]),
        ):
            result = await ReviewedDelegator(PlainModel(), [])._stream_agent(
                agent,
                [HumanMessage(content="input")],
                actor="worker",
                stage=1,
                stream_id="worker-1",
            )

        self.assertEqual(agent.calls, 2)
        self.assertEqual(result["messages"][-1].content, final.content)
        self.assertNotIn("不得公开", [str(message.content) for message in published])

    async def test_stream_agent_accepts_structured_response_with_empty_public_content(self):
        from caspian.agents.commitment.delegation import _extract_structured_logged

        empty = AIMessage(
            content="",
            additional_kwargs={"reasoning_content": "私密推理"},
            response_metadata={"finish_reason": "stop"},
        )
        structured = WorkerOutput(result={"goal": "X"})
        agent = ScriptedStreamAgent(
            [[
                ("messages", (empty, {})),
                (
                    "values",
                    {"messages": [empty], "structured_response": structured},
                ),
            ]]
        )

        result = await ReviewedDelegator(PlainModel(), [])._stream_agent(
            agent,
            [HumanMessage(content="input")],
            actor="worker",
            stage=1,
            stream_id="worker-1",
        )
        parsed = _extract_structured_logged(
            result,
            WorkerOutput,
            actor="worker",
            stage=1,
            attempt=1,
        )

        self.assertEqual(agent.calls, 1)
        self.assertEqual(parsed, structured)

    async def test_stream_agent_retries_insufficient_system_resource(self):
        partial = AIMessage(
            content='{"result":',
            response_metadata={"finish_reason": "insufficient_system_resource"},
        )
        final = AIMessage(
            content='{"result":{"goal":"X"}}',
            response_metadata={"finish_reason": "stop"},
        )
        agent = ScriptedStreamAgent(
            [
                [("values", {"messages": [partial]})],
                [("values", {"messages": [final]})],
            ]
        )

        result = await ReviewedDelegator(PlainModel(), [])._stream_agent(
            agent,
            [HumanMessage(content="input")],
            actor="worker",
            stage=1,
            stream_id="worker-1",
        )

        self.assertEqual(agent.calls, 2)
        self.assertEqual(result["messages"][-1].content, final.content)

    async def test_stream_agent_reports_length_and_transport_exhaustion(self):
        from caspian.agents.commitment.delegation import _ModelOutputError

        truncated = AIMessage(
            content='{"result":',
            response_metadata={"finish_reason": "length"},
        )
        delegator = ReviewedDelegator(PlainModel(), [])
        with self.assertRaisesRegex(_ModelOutputError, "length|截断"):
            await delegator._stream_agent(
                ScriptedStreamAgent([[("values", {"messages": [truncated]})]]),
                [HumanMessage(content="input")],
                actor="worker",
                stage=1,
                stream_id="worker-1",
            )

        empty = AIMessage(content="", response_metadata={"finish_reason": "stop"})
        exhausted = ScriptedStreamAgent(
            [
                [("values", {"messages": [empty]})],
                [("values", {"messages": [empty]})],
                [("values", {"messages": [empty]})],
            ]
        )
        with self.assertRaisesRegex(_ModelOutputError, "重试耗尽"):
            await delegator._stream_agent(
                exhausted,
                [HumanMessage(content="input")],
                actor="evaluator",
                stage=1,
                stream_id="evaluator-1",
            )
        self.assertEqual(exhausted.calls, 3)

    async def test_stream_agent_applies_timeout_to_each_model_request(self):
        from caspian.agents.commitment.delegation import _ModelOutputError

        class HangingStreamAgent:
            def __init__(self):
                self.calls = 0

            async def astream(self, _input, stream_mode):
                self.calls += 1
                await asyncio.Event().wait()
                if False:
                    yield None

        agent = HangingStreamAgent()
        with patch(
            "caspian.agents.commitment.delegation._MODEL_REQUEST_TIMEOUT_SECONDS",
            0.01,
        ):
            with self.assertRaisesRegex(_ModelOutputError, "TimeoutError"):
                await ReviewedDelegator(PlainModel(), [])._stream_agent(
                    agent,
                    [HumanMessage(content="input")],
                    actor="evaluator",
                    stage=2,
                    stream_id="evaluator-2",
                )

        self.assertEqual(agent.calls, 3)

    async def test_stream_agent_does_not_transport_retry_invalid_public_json(self):
        from caspian.agents.commitment.delegation import _extract_structured_logged

        invalid = AIMessage(
            content="not json",
            response_metadata={"finish_reason": "stop"},
        )
        agent = ScriptedStreamAgent(
            [[("messages", (invalid, {})), ("values", {"messages": [invalid]})]]
        )
        result = await ReviewedDelegator(PlainModel(), [])._stream_agent(
            agent,
            [HumanMessage(content="input")],
            actor="worker",
            stage=1,
            stream_id="worker-1",
        )

        self.assertEqual(agent.calls, 1)
        with self.assertRaises(ValueError):
            _extract_structured_logged(
                result,
                WorkerOutput,
                actor="worker",
                stage=1,
                attempt=1,
            )

    def test_stage_two_dimension_semantics_in_prompt(self):
        import inspect

        from caspian.agents.commitment.delegation import ReviewedDelegator

        source = inspect.getsource(ReviewedDelegator)
        self.assertIn("compatibility_checks是逐项技术检查", source)
        self.assertIn("单项verified与组合conflict并存不构成矛盾", source)

    async def test_validation_rejection_logs_worker_result(self):
        class MissingPriorityDelegator(ReviewedDelegator):
            def __init__(self):
                super().__init__(PlainModel(), [])

            async def _worker(
                self,
                envelope,
                feedback,
                supervisor_messages=None,
                attempt=1,
            ):
                return WorkerOutput(
                    result={"requirements": [{"requirement": "代码必须尽可能简单"}]}
                )

        delegator = MissingPriorityDelegator()
        with patch(
            "caspian.agents.commitment.delegation.logger"
        ) as mock_logger:
            output, error = await delegator.run(
                TaskEnvelope(stage=3, instruction="分配优先级")
            )
        self.assertIsNone(output)
        self.assertIn("缺少有效的 priority", error)
        rejection_logs = [
            call.args
            for call in mock_logger.warning.call_args_list
            if "校验拒绝" in call.args[0]
        ]
        self.assertTrue(rejection_logs)
        self.assertIn("代码必须尽可能简单", rejection_logs[0][-1])
        self.assertIn("缺少有效的 priority", rejection_logs[0][-2])

    async def test_evaluator_empty_output_readable_feedback(self):
        from caspian.agents.commitment.delegation import _ModelOutputError

        class EmptyJsonModel(PlainModel):
            async def ainvoke(self, messages, config=None, **kwargs):
                return AIMessage(
                    content="",
                    response_metadata={"finish_reason": "stop"},
                )

            def with_structured_output(self, schema, **kwargs):
                class EmptyStructuredModel:
                    async def ainvoke(self, messages):
                        return {
                            "raw": AIMessage(
                                content="",
                                response_metadata={"finish_reason": "stop"},
                            ),
                            "parsed": None,
                            "parsing_error": None,
                        }

                return EmptyStructuredModel()

        delegator = ReviewedDelegator(EmptyJsonModel(), [])
        with self.assertRaisesRegex(_ModelOutputError, "JSON mode|业务结果"):
            await delegator._evaluator(
                TaskEnvelope(stage=1, instruction="goal"),
                WorkerOutput(result={"goal": "x"}),
                [],
                1,
            )

    async def test_worker_empty_output_readable_feedback(self):
        class EmptyWorkerDelegator(ReviewedDelegator):
            def __init__(self):
                super().__init__(PlainModel(), [])

            async def _worker(
                self,
                envelope,
                feedback,
                supervisor_messages=None,
                attempt=1,
            ):
                # 真实复现：模型返回空内容时的 EOF ValidationError
                ReviewOutput.model_validate_json("")

        delegator = EmptyWorkerDelegator()
        output, error = await delegator.run(
            TaskEnvelope(stage=1, instruction="goal")
        )

        self.assertIsNone(output)
        self.assertIn("输出为空", error)
        self.assertNotIn("validation error", error)

    async def test_model_output_failure_does_not_start_next_semantic_attempt(self):
        from caspian.agents.commitment.delegation import _ModelOutputError

        class OutputFailureDelegator(ReviewedDelegator):
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
                return WorkerOutput(result={"goal": "valid worker output"})

            async def _evaluator(self, *args, **kwargs):
                raise _ModelOutputError("模型传输重试耗尽")

        delegator = OutputFailureDelegator()
        output, error = await delegator.run(
            TaskEnvelope(stage=1, instruction="goal")
        )

        self.assertIsNone(output)
        self.assertEqual(delegator.worker_calls, 1)
        self.assertIn("Evaluator 模型输出错误", error)

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
                # 外层守卫仅防"整个图真挂死"，不是判定阈值;放宽容忍慢 runner 的冷启动
                # 时间(内层 _STAGE_TIMEOUT_SECONDS=0.01 仍会稳定触发阶段超时)。
                timeout=10,
            )
        payload = result["__interrupt__"][0].value
        self.assertEqual(payload["stage"], 1)
        self.assertEqual(payload["allowed_decisions"], ["revise"])
        self.assertIn("超时", payload["error"])

    async def test_human_feedback_replaces_message_with_revision_provenance(self):
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
        stage_payload = json.loads(
            next(
                message.content
                for message in resumed["messages"]
                if isinstance(message, ToolMessage)
            )
        )
        self.assertEqual(
            stage_payload["revision_provenance"],
            {
                "stage": 3,
                "decision": "revise",
                "feedback": "human-only-feedback",
            },
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

    def test_review_output_lenient_parse_trailing_garbage(self):
        parsed = _extract_structured(
            {
                "messages": [
                    AIMessage(
                        content='{"approved": true, "feedback": "通过"} '
                        '后续继续执行。"}}'
                    )
                ]
            },
            ReviewOutput,
        )
        self.assertTrue(parsed.approved)
        self.assertEqual(parsed.feedback, "通过")

    def test_review_output_lenient_parse_truncated(self):
        parsed = _extract_structured(
            {
                "messages": [
                    AIMessage(content='{"approved": true, "feedback": "部分')
                ]
            },
            ReviewOutput,
        )
        self.assertTrue(parsed.approved)

    def test_stage_two_prompt_forbids_requirement_annotations(self):
        import inspect

        from caspian.agents.commitment.delegation import ReviewedDelegator

        source = inspect.getsource(ReviewedDelegator._worker)
        self.assertIn("不得添加任何括注", source)
        self.assertIn("逐字来自用户输入原文", source)

    def test_stage_five_prompt_boundaries(self):
        import inspect

        from caspian.agents.commitment.delegation import ReviewedDelegator

        source = inspect.getsource(ReviewedDelegator)
        # Evaluator 边界：能力/质量要求不属于阶段 5 范围，不得要求臆造技术名
        self.assertIn("没有独立技术名的能力或质量要求不属于", source)
        self.assertIn("不得要求补入未指名的臆造技术名", source)
        # selector 加固：未指名具体库的能力不提取，反馈要求也不臆造
        self.assertIn("不得臆造技术名", source)

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

    async def test_middleware_only_runs_for_explicit_commit_command(self):
        middleware = object.__new__(CommitmentMiddleware)
        supervisor = FakeSupervisor({})
        middleware._supervisor = supervisor
        runtime = SimpleNamespace()

        messages = [
            HumanMessage(content="普通对话", id="plain"),
            HumanMessage(content="/commit", id="empty"),
            HumanMessage(content="/commit   ", id="blank"),
            HumanMessage(content="请稍后 /commit 执行", id="middle"),
            HumanMessage(content="/commitment 执行", id="long-command"),
            HumanMessage(content="/Commit 执行", id="wrong-case"),
            AIMessage(content="/commit 执行", id="assistant"),
        ]
        for message in messages:
            with self.subTest(content=message.content):
                self.assertIsNone(
                    await middleware._run({"messages": [message]}, runtime)
                )
        self.assertEqual(supervisor.inputs, [])

    async def test_middleware_preserves_history_and_replaces_commit_message(self):
        middleware = object.__new__(CommitmentMiddleware)
        supervisor = FakeSupervisor(
            {
                "stage": 9,
                "task_contract": "# Contract",
                "final_message": "<task_contract># Contract</task_contract>",
            }
        )
        middleware._supervisor = supervisor
        runtime = SimpleNamespace(
            execution_info=SimpleNamespace(thread_id="thread-1")
        )
        original = [
            HumanMessage(content="先讨论目标", id="history-human"),
            AIMessage(content="先澄清约束", id="history-ai"),
            HumanMessage(
                content="  /commit   实现登录流程  ",
                id="commit-message",
            ),
        ]
        batches = []
        with patch(
            "caspian.agents.commitment.middleware._write_commitment_messages",
            side_effect=batches.append,
        ):
            update = await middleware._run(
                {"messages": original},
                runtime,
            )
        self.assertEqual(update["task_contract"], "# Contract")
        self.assertEqual(len(update["messages"]), 1)
        self.assertIsInstance(update["messages"][0], HumanMessage)
        self.assertEqual(update["messages"][0].id, "commit-message")
        self.assertNotIn("Worker", update["messages"][0].content)
        subgraph_seed = supervisor.inputs[0]
        supervisor_messages = subgraph_seed["messages"]
        self.assertEqual(
            [message.content for message in supervisor_messages],
            ["实现登录流程"],
        )
        self.assertEqual(supervisor_messages[0].id, "commit-message")
        self.assertEqual(subgraph_seed["source_text"], "实现登录流程")
        reduced = add_messages(original, update["messages"])
        self.assertEqual(
            [message.content for message in reduced],
            [
                "先讨论目标",
                "先澄清约束",
                "<task_contract># Contract</task_contract>",
            ],
        )
        self.assertEqual(
            [message.id for message in reduced],
            ["history-human", "history-ai", "commit-message"],
        )
        self.assertTrue(
            all(batch["type"] == "commitment_messages" for batch in batches)
        )

    async def test_commit_with_unavailable_context7_returns_notice(self):
        # /commit 触发但 loader 返回空：应返回说明 HumanMessage，不调用 supervisor、不抛异常
        middleware = object.__new__(CommitmentMiddleware)
        middleware._context7_loader = AsyncMock(return_value=[])
        middleware._supervisor = None
        runtime = SimpleNamespace(
            execution_info=SimpleNamespace(thread_id="thread-c7")
        )
        update = await middleware._run(
            {"messages": [HumanMessage(content="/commit 做X", id="commit-message")]},
            runtime,
        )
        self.assertIsNotNone(update)
        self.assertEqual(len(update["messages"]), 1)
        self.assertIsInstance(update["messages"][0], HumanMessage)
        self.assertEqual(update["messages"][0].id, "commit-message")
        self.assertIn("Context7", update["messages"][0].content)
        self.assertIsNone(middleware._supervisor)

    async def test_commit_with_throwing_context7_loader_returns_notice(self):
        # loader 抛异常：应降级为说明消息，不向外抛异常
        middleware = object.__new__(CommitmentMiddleware)
        middleware._context7_loader = AsyncMock(side_effect=RuntimeError("boom"))
        middleware._supervisor = None
        middleware._model = None
        runtime = SimpleNamespace(
            execution_info=SimpleNamespace(thread_id="thread-c7-2")
        )
        update = await middleware._run(
            {"messages": [HumanMessage(content="/commit 做X", id="commit-message")]},
            runtime,
        )
        self.assertIsNotNone(update)
        self.assertIn("Context7", update["messages"][0].content)
        self.assertIsNone(middleware._supervisor)

    async def test_plain_conversation_does_not_call_context7_loader(self):
        # 普通对话（非 /commit）：应返回 None 且不调用 loader
        middleware = object.__new__(CommitmentMiddleware)
        middleware._context7_loader = AsyncMock(return_value=[])
        middleware._supervisor = None
        middleware._model = None
        runtime = SimpleNamespace(
            execution_info=SimpleNamespace(thread_id="thread-plain")
        )
        update = await middleware._run(
            {"messages": [HumanMessage(content="普通对话", id="plain-message")]},
            runtime,
        )
        self.assertIsNone(update)
        middleware._context7_loader.assert_not_called()
        self.assertIsNone(middleware._supervisor)

    def test_uploads_tag_extracted_from_instruction(self):
        clean, tag = _extract_uploads_tag(
            "做X\n\n<current_uploads>\n- filename: a.md\n  size: 10\n"
            "</current_uploads>"
        )
        self.assertEqual(clean, "做X")
        self.assertIn("a.md", tag or "")
        clean, tag = _extract_uploads_tag("做X")
        self.assertEqual(clean, "做X")
        self.assertIsNone(tag)

    async def test_lead_history_and_tool_messages_never_enter_subgraph(self):
        middleware = object.__new__(CommitmentMiddleware)
        supervisor = FakeSupervisor(
            {
                "stage": 9,
                "task_contract": "# Contract",
                "final_message": "<task_contract># Contract</task_contract>",
            }
        )
        middleware._supervisor = supervisor
        runtime = SimpleNamespace(
            execution_info=SimpleNamespace(thread_id="thread-1")
        )
        lead = [
            HumanMessage(content="先讨论目标", id="history-human"),
            AIMessage(content="先澄清约束", id="history-ai"),
            ToolMessage(
                content='{"stage": 2, "status": "approved", '
                '"result": {"requirements": ["x"]}}',
                tool_call_id="lead-tool",
                id="history-tool",
            ),
            HumanMessage(
                content="/commit 做X\n\n<current_uploads>\n"
                "- filename: a.md\n  size: 10\n</current_uploads>",
                id="commit-message",
            ),
        ]
        with patch(
            "caspian.agents.commitment.middleware._write_commitment_messages",
            side_effect=lambda _payload: None,
        ):
            await middleware._run({"messages": lead}, runtime)
        seed = supervisor.inputs[0]
        self.assertEqual(
            [message.content for message in seed["messages"]],
            ["做X"],
        )
        self.assertEqual(seed["messages"][0].id, "commit-message")
        self.assertEqual(seed["source_text"], "做X")
        self.assertIn("a.md", seed["uploads_tag"])
        self.assertNotIn("先讨论目标", str(seed["messages"]))
        self.assertNotIn("先澄清约束", str(seed["messages"]))
        self.assertNotIn("lead-tool", str(seed["messages"]))
        self.assertNotIn("history-tool", str(seed["messages"]))

    async def test_resume_does_not_replay_completed_stages(self):
        class CountingDelegator(ReviewedDelegator):
            def __init__(self):
                super().__init__(None, [])
                self.calls: list[int] = []

            async def run(self, envelope, supervisor_messages=None):
                self.calls.append(envelope.stage)
                stage = envelope.stage
                if stage == 2:
                    result = {
                        "requirements": ["r1"],
                        "discarded_requirements": [],
                        "compatibility_checks": [
                            {
                                "technology": "t",
                                "application_type": "web",
                                "ui_surface": "browser",
                                "runtime_platform": "server",
                                "host_model": "web server",
                                "status": "verified",
                            }
                        ],
                        "conflicts": [],
                    }
                else:
                    result = {"goal": f"stage-{stage}"}
                return WorkerOutput(result=result), ""

        delegator = CountingDelegator()
        supervisor = _build_supervisor(delegator)
        supervisor.checkpointer = InMemorySaver()
        config = {"configurable": {"thread_id": "no-replay"}}
        state = {
            "messages": [HumanMessage(content="指令", id="inst")],
            "stage": 0,
            "awaiting_human": None,
            "artifacts": {},
            "source_text": "指令",
            "thread_id": "no-replay",
            "knowledge_files": [],
        }
        first = await supervisor.ainvoke(state, config=config)
        self.assertEqual(first["__interrupt__"][0].value["stage"], 3)
        self.assertEqual(delegator.calls, [1, 2, 3])

        resumed = await supervisor.ainvoke(
            Command(resume={"decision": "approve"}),
            config=config,
        )
        self.assertEqual(resumed["__interrupt__"][0].value["stage"], 5)
        # 已完成阶段 1-3 不得重跑；resume 后只新执行 stage 4、5
        self.assertEqual(delegator.calls, [1, 2, 3, 4, 5])

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
            {
                "messages": [
                    HumanMessage(content="先讨论目标", id="history-human"),
                    AIMessage(content="先澄清约束", id="history-ai"),
                    HumanMessage(content="/commit goal", id="commit-message"),
                ]
            },
            config=config,
        )
        self.assertEqual(paused["__interrupt__"][0].value["stage"], 3)
        self.assertEqual(
            [message.content for message in paused["messages"]],
            ["先讨论目标", "先澄清约束", "/commit goal"],
        )

        resumed = await agent.ainvoke(
            Command(resume={"decision": "approve"}),
            config=config,
        )
        self.assertEqual(resumed["task_contract"], "# Contract")
        self.assertEqual(
            [message.content for message in resumed["messages"]],
            [
                "先讨论目标",
                "先澄清约束",
                "<task_contract># Contract</task_contract>",
                "lead done",
            ],
        )
        self.assertEqual(
            [message.id for message in resumed["messages"][:3]],
            ["history-human", "history-ai", "commit-message"],
        )

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
            subagents=SimpleNamespace(enabled=True),
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


class CommitInstructionSkillTokenTests(unittest.TestCase):
    def test_leading_skill_tokens_do_not_block_commit_trigger(self):
        self.assertEqual(
            _commit_instruction(
                HumanMessage(content="/docx /commit 做X"),
                frozenset({"docx"}),
            ),
            "做X",
        )

    def test_multiple_leading_skill_tokens_all_stripped(self):
        self.assertEqual(
            _commit_instruction(
                HumanMessage(content="/docx /data-analysis /commit 做X"),
                frozenset({"docx", "data-analysis"}),
            ),
            "做X",
        )

    def test_unknown_leading_token_is_not_stripped(self):
        self.assertIsNone(
            _commit_instruction(
                HumanMessage(content="/unknown /commit 做X"),
                frozenset({"docx"}),
            )
        )

    def test_plain_commit_without_tokens_unchanged(self):
        self.assertEqual(
            _commit_instruction(
                HumanMessage(content="/commit 做X"),
                frozenset({"docx"}),
            ),
            "做X",
        )

    def test_empty_commit_not_triggered(self):
        self.assertIsNone(
            _commit_instruction(
                HumanMessage(content="/commit   "),
                frozenset({"docx"}),
            )
        )

    def test_empty_skill_names_disables_stripping(self):
        self.assertIsNone(
            _commit_instruction(
                HumanMessage(content="/docx /commit 做X"),
                frozenset(),
            )
        )

    def test_trailing_slash_token_is_instruction_text(self):
        self.assertEqual(
            _commit_instruction(
                HumanMessage(content="/commit 做X /docx"),
                frozenset({"docx"}),
            ),
            "做X /docx",
        )

    def test_enabled_builder_forwards_skill_names_to_commitment(self):
        with patch(
            "caspian.agents.middlewares.builder.CommitmentMiddleware",
            return_value=object(),
        ) as middleware_cls:
            build_general_middlewares(
                commitment_enabled=True,
                model=object(),
                context7_tools=[],
                skill_names=frozenset({"docx"}),
            )
        self.assertEqual(middleware_cls.call_args[0][2], frozenset({"docx"}))

    def test_enabled_builder_forwards_context7_loader(self):
        async def fake_loader():
            return []

        with patch(
            "caspian.agents.middlewares.builder.CommitmentMiddleware",
            return_value=object(),
        ) as middleware_cls:
            build_general_middlewares(
                commitment_enabled=True,
                model=object(),
                context7_loader=fake_loader,
            )
        self.assertIs(middleware_cls.call_args[0][1], fake_loader)

    def test_enabled_builder_context7_tools_compat_wraps_loader(self):
        sentinel = [object()]
        with patch(
            "caspian.agents.middlewares.builder.CommitmentMiddleware",
            return_value=object(),
        ) as middleware_cls:
            build_general_middlewares(
                commitment_enabled=True,
                model=object(),
                context7_tools=sentinel,
            )
        loader = middleware_cls.call_args[0][1]
        self.assertTrue(callable(loader))
        # 兼容路径：被包装的 loader 应返回 context7_tools 内容
        import asyncio as _asyncio

        self.assertEqual(_asyncio.run(loader()), sentinel)


if __name__ == "__main__":
    unittest.main()
