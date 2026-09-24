"""
本文件通过真实 create_agent 图捕获模型 wire 消息，验证完整 system snapshot 的装配顺序。

输入为 Replace/Append route、Plan/Delegation 状态、legacy checkpoint 与工具回合中的表更新；
输出为 CapturingModel 收到的消息数组。工作流断言仅完整 snapshot 具权威性、未变化零增长、
工具提交后的下一模型边界立刻读取新表。

示例: pytest tests/test_system_snapshot_wire.py
"""

import asyncio
from unittest.mock import patch

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from caspian.agents.commitment.decision_table import DecisionRow, DecisionTable
from caspian.agents.lead_agent_state import LeadAgentState
from caspian.agents.plan import PlanModeMiddleware
from caspian.agents.system_prompt.metadata import is_managed_snapshot
from caspian.agents.system_prompt.middleware import SystemSnapshotMiddleware
from caspian.config.model_config import SystemPromptUpdate
from caspian.config.plan_mode_config import PlanModeConfig


class CapturingModel(BaseChatModel):
    requests: list[list] = []
    replies: list[AIMessage] = []

    @property
    def _llm_type(self):
        return "capturing-system-snapshot"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.requests.append(list(messages))
        reply = self.replies.pop(0) if self.replies else AIMessage(content="done")
        return ChatResult(generations=[ChatGeneration(message=reply)])

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self


def _table(version):
    return DecisionTable(
        version=version,
        updated="2026-09-23T00:00:00Z",
        rows=[DecisionRow(id="r1", requirement="req", decision=version, priority=3)],
    )


def _invoke(agent, state, thread="wire-thread"):
    return asyncio.run(
        agent.ainvoke(state, config={"configurable": {"thread_id": thread}})
    )


def test_replace_wire_has_one_complete_leading_system_and_filters_legacy():
    model = CapturingModel()
    agent = create_agent(
        model=model,
        tools=[],
        middleware=[SystemSnapshotMiddleware("BASE", SystemPromptUpdate.REPLACE)],
        system_prompt="BASE",
        state_schema=LeadAgentState,
    )
    with patch(
        "caspian.agents.system_prompt.middleware.read_decision_table",
        return_value=_table("v1"),
    ):
        _invoke(
            agent,
            {
                "messages": [
                    SystemMessage(content="old partial", id="decision-table"),
                    HumanMessage(content="hello", id="u1"),
                ]
            },
        )
    systems = [message for message in model.requests[0] if isinstance(message, SystemMessage)]
    assert len(systems) == 1
    assert systems[0].content.startswith("BASE")
    assert '<decision_table version="v1"' in systems[0].content


def test_append_wire_places_plan_and_delegation_snapshot_before_user():
    model = CapturingModel()
    plan = PlanModeConfig(enabled=True, section="PLAN POLICY")
    agent = create_agent(
        model=model,
        tools=[],
        middleware=[
            PlanModeMiddleware(plan),
            SystemSnapshotMiddleware(
                "BASE",
                SystemPromptUpdate.IN_HISTORY,
                plan_section=plan.section,
            ),
        ],
        system_prompt="BASE",
        state_schema=LeadAgentState,
    )
    with patch(
        "caspian.agents.system_prompt.middleware.read_decision_table",
        return_value=None,
    ):
        _invoke(
            agent,
            {
                "messages": [HumanMessage(content="/plan research", id="u1")],
                "delegations": [
                    {
                        "id": "d1",
                        "description": "delegated research",
                        "subagent_type": "general-purpose",
                        "status": "in_progress",
                        "created_at": "2026-09-23T00:00:00Z",
                    }
                ],
            },
        )
    request = model.requests[0]
    assert [message.type for message in request[:3]] == ["system", "system", "human"]
    assert request[0].content == "BASE"
    assert is_managed_snapshot(request[1])
    assert "PLAN POLICY" in request[1].content
    assert "delegated research" in request[1].content
    assert request[2].content == "research"


def test_append_hot_reloads_committed_table_between_tool_and_next_model_call():
    current = {"table": _table("v1")}

    @tool
    def commit_new_table() -> str:
        """Commit the next authoritative Decision Table version."""
        current["table"] = _table("v2")
        return "committed"

    model = CapturingModel(
        replies=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "commit_new_table",
                        "args": {},
                        "id": "tc1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    agent = create_agent(
        model=model,
        tools=[commit_new_table],
        middleware=[
            SystemSnapshotMiddleware("BASE", SystemPromptUpdate.IN_HISTORY)
        ],
        system_prompt="BASE",
        state_schema=LeadAgentState,
    )
    with patch(
        "caspian.agents.system_prompt.middleware.read_decision_table",
        side_effect=lambda *args, **kwargs: current["table"],
    ):
        final = _invoke(
            agent,
            {"messages": [HumanMessage(content="update it", id="u1")]},
            thread="wire-hot-reload",
        )
    assert len(model.requests) == 2
    first_snapshots = [message for message in model.requests[0] if is_managed_snapshot(message)]
    second_snapshots = [message for message in model.requests[1] if is_managed_snapshot(message)]
    assert len(first_snapshots) == 1
    assert len(second_snapshots) == 2
    assert '<decision_table version="v1"' in first_snapshots[-1].content
    assert '<decision_table version="v2"' in second_snapshots[-1].content
    assert final["effective_system_snapshot"]["decision_table_version"] == "v2"


def test_append_legacy_checkpoint_is_superseded_without_sending_partial_patch():
    model = CapturingModel()
    agent = create_agent(
        model=model,
        tools=[],
        middleware=[SystemSnapshotMiddleware("BASE", SystemPromptUpdate.IN_HISTORY)],
        system_prompt="BASE",
        state_schema=LeadAgentState,
    )
    with patch(
        "caspian.agents.system_prompt.middleware.read_decision_table",
        return_value=None,
    ):
        _invoke(
            agent,
            {
                "messages": [
                    SystemMessage(content="old partial", id="decision-table"),
                    HumanMessage(content="hello", id="u1"),
                ]
            },
            thread="wire-legacy-append",
        )
    systems = [message for message in model.requests[0] if isinstance(message, SystemMessage)]
    assert [message.content for message in systems] == ["BASE", "BASE"]
    assert is_managed_snapshot(systems[1])
