"""
本文件提供 CommitmentMiddleware PoC 的标准库 unittest。

输入:
    假 Worker/Evaluator、临时目录、假 StreamBridge 和假 agent stream

输出:
    可运行检查，覆盖开关、审核重试、人工修订、磁盘结果、消息隔离和 interrupt/resume
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from langchain.agents import create_agent
from langchain.messages import HumanMessage, RemoveMessage
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command, Interrupt

from backend.app.gateway.routers.thread_runs import RunCreateRequest
from backend.app.gateway.services import _build_graph_input
from caspian.agents.middlewares.builder import build_general_middlewares
from caspian.agents.middlewares.commitment_middleware import (
    CommitmentMiddleware,
    CommitmentState,
    ReviewOutput,
    ReviewedDelegator,
    TaskEnvelope,
    WorkerOutput,
    _build_final_message,
    _contains_unresolved_versions,
    _review_human_revision,
    _safe_segment,
    _write_contract,
    _write_knowledge,
    build_delegate_with_review_tool,
)
from caspian.config.commitment_config import CommitmentConfig
from caspian.runtime.runs.schemas import RunStatus
from caspian.runtime.runs.worker import _extract_interrupts, run_agent


class StubDelegator(ReviewedDelegator):
    def __init__(self, approvals: list[bool]) -> None:
        super().__init__(None, [])
        self.approvals = approvals
        self.worker_calls = 0

    async def _worker(self, envelope, feedback):
        self.worker_calls += 1
        return WorkerOutput(
            result={"goal": f"attempt-{self.worker_calls}"},
            artifact_ref=None,
        )

    async def _evaluator(self, envelope, worker_output):
        approved = self.approvals[self.worker_calls - 1]
        return ReviewOutput(approved=approved, feedback="retry")


class FakeSupervisor:
    def __init__(self, result):
        self.result = result

    async def ainvoke(self, _input):
        return self.result


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


class AlwaysApprovedDelegator(ReviewedDelegator):
    def __init__(self):
        super().__init__(None, [])

    async def run(self, envelope):
        return WorkerOutput(result={"goal": "approved"}), ""


class FakeAgent:
    checkpointer = None
    store = None

    async def astream(self, *_args, **_kwargs):
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

    async def test_human_approval_resumes_same_tool_call(self):
        model = ScriptedToolModel(next_stage=4)
        agent = create_agent(
            model=model,
            tools=[
                build_delegate_with_review_tool(AlwaysApprovedDelegator())
            ],
            state_schema=CommitmentState,
            checkpointer=InMemorySaver(),
        )
        config = {"configurable": {"thread_id": "approval-thread"}}
        first = await agent.ainvoke(
            {
                "messages": [HumanMessage(content="continue")],
                "stage": 3,
                "awaiting_human": 3,
                "artifacts": {
                    "3": {
                        "requirements": [
                            {"text": "must", "priority": 3}
                        ]
                    }
                },
                "source_text": "goal",
                "thread_id": "approval-thread",
            },
            config=config,
        )
        self.assertEqual(first["__interrupt__"][0].value["stage"], 3)
        resumed = await agent.ainvoke(
            Command(resume={"decision": "approve"}),
            config=config,
        )
        self.assertEqual(resumed["stage"], 3)
        self.assertIsNone(resumed["awaiting_human"])

    async def test_human_replacement_is_validated(self):
        revised, error = await _review_human_revision(
            {
                "decision": "revise",
                "replacement": {
                    "requirements": [{"text": "must", "priority": 3}]
                },
            },
            3,
            {},
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
            {},
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

    def test_knowledge_contract_and_final_message(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "caspian.agents.middlewares.commitment_middleware._PROJECT_ROOT",
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
        update = await middleware._run(
            {"messages": [HumanMessage(content="raw goal")]},
            runtime,
        )
        self.assertEqual(update["task_contract"], "# Contract")
        self.assertEqual(len(update["messages"]), 2)
        self.assertIsInstance(update["messages"][0], RemoveMessage)
        self.assertIsInstance(update["messages"][1], HumanMessage)
        self.assertNotIn("Worker", update["messages"][1].content)

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
        self.assertTrue(bridge.ended)


if __name__ == "__main__":
    unittest.main()
