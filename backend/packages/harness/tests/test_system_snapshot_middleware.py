"""
本文件验证 SystemSnapshotMiddleware 的热更新、wire 语义、fragment 契约与 legacy 兼容。

输入为显式 route mode、运行状态、authoritative table 和 ModelRequest；输出为状态增量及捕获的
模型请求。工作流覆盖 Replace 单一 leading system、Append 唯一完整事件、下一模型边界热更新与
未标记 SystemMessage 的安全降级。

示例: pytest tests/test_system_snapshot_middleware.py
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph.message import add_messages

from caspian.agents.commitment.decision_table import DecisionRow, DecisionTable
from caspian.agents.system_prompt.metadata import (
    SYSTEM_FRAGMENT_MARKER_KEY,
    is_managed_snapshot,
    make_snapshot_message,
)
from caspian.agents.system_prompt.snapshot import SystemSnapshot
from caspian.agents.system_prompt.middleware import SystemSnapshotMiddleware
from caspian.config.model_config import SystemPromptUpdate


def _runtime():
    return SimpleNamespace(
        execution_info=SimpleNamespace(thread_id="thread-1"),
        context={"user_id": "user-1"},
    )


def _table(version, decision=None):
    return DecisionTable(
        version=version,
        updated="2026-09-23T00:00:00Z",
        rows=[
            DecisionRow(
                id="r1",
                requirement="requirement",
                decision=decision or version,
                priority=3,
            )
        ],
    )


def _apply(state, update):
    if update is None:
        return state
    merged = dict(state)
    if "messages" in update:
        merged["messages"] = add_messages(state.get("messages", []), update["messages"])
    if "effective_system_snapshot" in update:
        merged["effective_system_snapshot"] = update["effective_system_snapshot"]
    return merged


def _request(state, system="BASE"):
    return ModelRequest(
        model=object(),
        messages=list(state.get("messages", [])),
        system_message=SystemMessage(content=system),
        state=state,
        runtime=_runtime(),
    )


def test_replace_sends_one_leading_full_system_and_filters_system_history():
    middleware = SystemSnapshotMiddleware("BASE", SystemPromptUpdate.REPLACE)
    state = {
        "messages": [
            SystemMessage(content="legacy", id="decision-table"),
            HumanMessage(content="hello", id="u1"),
        ]
    }
    with patch(
        "caspian.agents.system_prompt.middleware.read_decision_table",
        return_value=_table("v1"),
    ):
        state = _apply(state, middleware.before_model(state, _runtime()))

    async def handler(request):
        return request

    prepared = asyncio.run(middleware.awrap_model_call(_request(state), handler))
    assert "BASE" in prepared.system_message.content
    assert '<decision_table version="v1"' in prepared.system_message.content
    assert [message.id for message in prepared.messages] == ["u1"]


def test_append_places_full_snapshot_before_unsent_user_and_hot_reloads_after_tool():
    middleware = SystemSnapshotMiddleware("BASE", SystemPromptUpdate.IN_HISTORY)
    state = {"messages": [HumanMessage(content="hello", id="u1")]}
    with patch(
        "caspian.agents.system_prompt.middleware.read_decision_table",
        return_value=_table("v1"),
    ):
        state = _apply(state, middleware.before_agent(state, _runtime()))
        assert middleware.before_model(state, _runtime()) is None
    assert is_managed_snapshot(state["messages"][0])
    assert state["messages"][1].id == "u1"

    state["messages"].extend(
        [
            AIMessage(
                content="",
                id="a1",
                tool_calls=[{"name": "x", "args": {}, "id": "tc1"}],
            ),
            ToolMessage(content="done", id="t1", tool_call_id="tc1"),
        ]
    )
    with patch(
        "caspian.agents.system_prompt.middleware.read_decision_table",
        return_value=_table("v2"),
    ):
        state = _apply(state, middleware.before_model(state, _runtime()))
    snapshots = [message for message in state["messages"] if is_managed_snapshot(message)]
    assert len(snapshots) == 2
    assert '<decision_table version="v1"' in snapshots[0].content
    assert '<decision_table version="v2"' in snapshots[1].content
    assert state["messages"][-1] is snapshots[1]


def test_marked_fragment_is_folded_into_snapshot_and_removed_as_carrier():
    middleware = SystemSnapshotMiddleware("BASE", SystemPromptUpdate.IN_HISTORY)
    fragment = SystemMessage(
        content="PLUGIN POLICY",
        id="plugin-policy",
        additional_kwargs={SYSTEM_FRAGMENT_MARKER_KEY: "plugin-a"},
    )
    state = {"messages": [HumanMessage(content="hello", id="u1"), fragment]}
    with patch(
        "caspian.agents.system_prompt.middleware.read_decision_table",
        return_value=None,
    ):
        state = _apply(state, middleware.before_model(state, _runtime()))
    snapshots = [message for message in state["messages"] if is_managed_snapshot(message)]
    assert len(snapshots) == 1
    assert "BASE" in snapshots[0].content
    assert "PLUGIN POLICY" in snapshots[0].content
    assert fragment not in state["messages"]


def test_unmarked_system_message_forces_replace_and_is_not_sent():
    middleware = SystemSnapshotMiddleware("BASE", SystemPromptUpdate.IN_HISTORY)
    state = {
        "messages": [
            HumanMessage(content="hello", id="u1"),
            SystemMessage(content="unregistered", id="plugin-raw"),
        ]
    }
    with patch(
        "caspian.agents.system_prompt.middleware.read_decision_table",
        return_value=None,
    ):
        state = _apply(state, middleware.before_model(state, _runtime()))
    assert state["effective_system_snapshot"]["mode"] == "replace"
    assert not any(is_managed_snapshot(message) for message in state["messages"])

    async def handler(request):
        return request

    prepared = asyncio.run(middleware.awrap_model_call(_request(state), handler))
    assert prepared.system_message.content == "BASE"
    assert [message.id for message in prepared.messages] == ["u1"]


def test_append_static_baseline_records_state_without_token_bearing_event():
    middleware = SystemSnapshotMiddleware("BASE", SystemPromptUpdate.IN_HISTORY)
    state = {"messages": [HumanMessage(content="hello", id="u1")]}
    with patch(
        "caspian.agents.system_prompt.middleware.read_decision_table",
        return_value=None,
    ):
        update = middleware.before_model(state, _runtime())
    assert update["effective_system_snapshot"]["content"] == "BASE"
    assert "messages" not in update


def test_plan_and_delegation_changes_each_emit_one_complete_snapshot():
    middleware = SystemSnapshotMiddleware(
        "BASE",
        SystemPromptUpdate.IN_HISTORY,
        plan_section="PLAN POLICY",
    )
    state = {"messages": [HumanMessage(content="hello", id="u1")]}
    with patch(
        "caspian.agents.system_prompt.middleware.read_decision_table",
        return_value=None,
    ):
        state = _apply(state, middleware.before_model(state, _runtime()))
        state["plan_active"] = True
        state = _apply(state, middleware.before_model(state, _runtime()))
        assert middleware.before_model(state, _runtime()) is None
        state["delegations"] = [
            {
                "id": "d1",
                "description": "research",
                "subagent_type": "general-purpose",
                "status": "in_progress",
                "created_at": "2026-09-23T00:00:00Z",
            }
        ]
        state = _apply(state, middleware.before_model(state, _runtime()))
    snapshots = [message for message in state["messages"] if is_managed_snapshot(message)]
    assert len(snapshots) == 2
    assert "BASE" in snapshots[0].content and "PLAN POLICY" in snapshots[0].content
    assert "BASE" in snapshots[1].content and "research" in snapshots[1].content


def test_uncommitted_candidate_state_never_enters_snapshot_until_authoritative_read_changes():
    middleware = SystemSnapshotMiddleware("BASE", SystemPromptUpdate.REPLACE)
    state = {
        "messages": [HumanMessage(content="hello", id="u1")],
        "decision_table_candidate": {"version": "candidate-v2", "decision": "UNCOMMITTED"},
        "hitl_decision": "pending",
    }
    with patch(
        "caspian.agents.system_prompt.middleware.read_decision_table",
        return_value=_table("committed-v1", "COMMITTED"),
    ):
        state = _apply(state, middleware.before_model(state, _runtime()))
    assert "COMMITTED" in state["effective_system_snapshot"]["content"]
    assert "UNCOMMITTED" not in state["effective_system_snapshot"]["content"]
    with patch(
        "caspian.agents.system_prompt.middleware.read_decision_table",
        return_value=_table("committed-v2", "ADOPTED"),
    ):
        state = _apply(state, middleware.before_model(state, _runtime()))
    assert "ADOPTED" in state["effective_system_snapshot"]["content"]
    assert state["effective_system_snapshot"]["decision_table_version"] == "committed-v2"


def test_switching_checkpoint_from_append_back_to_replace_needs_no_migration():
    prior = make_snapshot_message(
        SystemSnapshot.from_content("OLD FULL"), SystemPromptUpdate.IN_HISTORY
    )
    middleware = SystemSnapshotMiddleware("BASE", SystemPromptUpdate.REPLACE)
    state = {
        "messages": [prior, HumanMessage(content="continue", id="u1")],
        "effective_system_snapshot": {
            "content": "OLD FULL",
            "fingerprint": prior.additional_kwargs["caspian_system_fingerprint"],
            "decision_table_version": None,
            "mode": "in-history",
        },
    }
    with patch(
        "caspian.agents.system_prompt.middleware.read_decision_table",
        return_value=None,
    ):
        state = _apply(state, middleware.before_model(state, _runtime()))

    async def handler(request):
        return request

    prepared = asyncio.run(middleware.awrap_model_call(_request(state), handler))
    assert prepared.system_message.content == "BASE"
    assert [message.id for message in prepared.messages] == ["u1"]
