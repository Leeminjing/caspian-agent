"""
本文件验证完整 system snapshot 的组装、消息元数据、append 去重与纯重基线。

输入为静态 prompt、动态 policy、DecisionTable 和消息历史；输出为完整快照、唯一 append events
及重基线分区。工作流覆盖 A→B→A、连续相同内容、legacy 混合历史和全部基础段保留。

示例: pytest tests/test_system_prompt_snapshot.py
"""

import json

from langchain_core.messages import HumanMessage, RemoveMessage, SystemMessage

from caspian.agents.commitment.decision_table import DecisionRow, DecisionTable
from caspian.agents.system_prompt.metadata import (
    SYSTEM_FRAGMENT_MARKER_KEY,
    is_managed_snapshot,
    make_snapshot_message,
    rebase_system_history,
)
from caspian.agents.system_prompt.snapshot import (
    SystemFragment,
    SystemSnapshot,
    SystemSnapshotBuilder,
)
from caspian.agents.system_prompt.strategy import AppendSnapshotStrategy, snapshot_record
from caspian.config.model_config import SystemPromptUpdate


def _table(version="v1", decision="keep"):
    return DecisionTable(
        version=version,
        updated="2026-09-23T00:00:00Z",
        rows=[DecisionRow(id="r1", requirement="req", decision=decision, priority=3)],
    )


def _event(update):
    return next(
        message
        for message in reversed(update["messages"])
        if is_managed_snapshot(message)
    )


def test_builder_keeps_all_base_sections_and_changes_only_dynamic_content():
    builder = SystemSnapshotBuilder("BASE", plan_section="PLAN")
    first = builder.build(
        plan_active=True,
        delegations=[
            {
                "id": "d1",
                "description": "research",
                "subagent_type": "general-purpose",
                "status": "in_progress",
                "created_at": "2026-09-23T00:00:00Z",
            }
        ],
        fragments=[SystemFragment("plugin-a", "PLUGIN")],
        decision_table=_table("v1", "one"),
    )
    second = builder.build(
        plan_active=True,
        delegations=[
            {
                "id": "d1",
                "description": "research",
                "subagent_type": "general-purpose",
                "status": "in_progress",
                "created_at": "2026-09-23T00:00:00Z",
            }
        ],
        fragments=[SystemFragment("plugin-a", "PLUGIN")],
        decision_table=_table("v2", "two"),
    )
    for content in (first.content, second.content):
        assert content.startswith("BASE")
        assert "PLAN" in content
        assert "research" in content
        assert "PLUGIN" in content
        assert "<decision_table" in content
    assert first.fingerprint != second.fingerprint
    assert first.decision_table_version == "v1"
    assert second.decision_table_version == "v2"


def test_static_only_snapshot_is_exact_static_prompt():
    snapshot = SystemSnapshotBuilder("BASE").build()
    assert snapshot.content == "BASE"


def test_append_strategy_uses_unique_events_for_a_b_a_and_deduplicates_consecutive():
    strategy = AppendSnapshotStrategy()
    history = [HumanMessage(content="u1", id="u1")]
    a = SystemSnapshot.from_content("A")
    first = strategy.update(a, None, history)
    first_event = _event(first)
    first_record = first["effective_system_snapshot"]
    assert isinstance(first["messages"][0], RemoveMessage)
    assert first["messages"][1] is first_event
    assert strategy.update(a, first_record, [first_event, *history]) is None

    b = SystemSnapshot.from_content("B")
    second = strategy.update(b, first_record, [first_event, *history])
    second_event = _event(second)
    third = strategy.update(
        a,
        second["effective_system_snapshot"],
        [first_event, second_event, *history],
    )
    third_event = _event(third)
    assert [first_event.content, second_event.content, third_event.content] == ["A", "B", "A"]
    assert len({first_event.id, second_event.id, third_event.id}) == 3


def test_fragment_marker_is_not_part_of_ordinary_rebased_history():
    fragment = SystemMessage(
        content="fragment",
        id="fragment-a",
        additional_kwargs={SYSTEM_FRAGMENT_MARKER_KEY: "plugin-a"},
    )
    s1 = make_snapshot_message(SystemSnapshot.from_content("S1"), SystemPromptUpdate.IN_HISTORY)
    s2 = make_snapshot_message(SystemSnapshot.from_content("S2"), SystemPromptUpdate.IN_HISTORY)
    user = HumanMessage(content="hello", id="u1")
    rebased = rebase_system_history(
        [
            SystemMessage(content="old table", id="decision-table"),
            s1,
            fragment,
            user,
            s2,
        ]
    )
    assert rebased.latest_snapshot is s2
    assert rebased.ordinary_messages == (user,)


def test_rebase_without_snapshot_keeps_ordinary_and_drops_legacy_patch():
    user = HumanMessage(content="hello", id="u1")
    rebased = rebase_system_history(
        [SystemMessage(content="old", id="decision-table"), user]
    )
    assert rebased.latest_snapshot is None
    assert rebased.ordinary_messages == (user,)


def test_snapshot_record_round_trips_through_json_primitives():
    snapshot = SystemSnapshot.from_content("S", decision_table_version="v7")
    record = snapshot_record(snapshot, SystemPromptUpdate.REPLACE)
    assert json.loads(json.dumps(record)) == record
