"""本文件验证并行 Run 事件投影的顺序、去重和工具配对。

输入为 A 的工作消息、B 的改表事件及 B 的未完成工具调用；输出为安全共享历史。
工作流先投影待执行 AI，再合入操作事件和工具结果，确保候选未生效且待执行动作不进入公共消息。
示例：`pytest tests/test_decision_convergence.py`。
"""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, message_to_dict, messages_from_dict

from caspian.decision_governance.convergence import merge_events


def _work(event_id, run_id, message):
    return {
        "event_id": event_id, "run_id": run_id,
        "event_type": "work_message", "payload": {"message": message_to_dict(message)},
    }


def test_parallel_messages_and_operation_events_preserve_causality():
    a_user = HumanMessage(id="a-user", content="A 开始")
    a_ai = AIMessage(id="a-ai", content="A 进展")
    b_ai = AIMessage(id="b-ai", content="", tool_calls=[{"name": "bash_tool", "args": {"command": "echo b"}, "id": "b-call"}])
    first = [
        _work("e1", "A", a_user),
        _work("e2", "A", a_ai),
        _work("e3", "B", b_ai),
        {
            "event_id": "e4", "run_id": "B", "operation_id": "op-b",
            "event_type": "awaiting_approval",
            "payload": {"changes": [{"kind": "add"}]},
        },
    ]
    messages, pending = merge_events([], [], first)
    decoded = messages_from_dict(messages)
    assert [message.id for message in decoded] == ["a-user", "a-ai", "e4"]
    assert len(pending) == 1
    assert "未生效" in decoded[-1].content

    second = [
        {"event_id": "e5", "run_id": "B", "operation_id": "op-b", "event_type": "committed", "payload": {"revision": 1}},
        _work("e6", "B", ToolMessage(id="b-result", content="done", tool_call_id="b-call")),
    ]
    merged, pending = merge_events(messages, pending, second)
    decoded = messages_from_dict(merged)
    assert [message.id for message in decoded] == ["a-user", "a-ai", "e4", "e5", "b-ai", "b-result"]
    assert "已生效" in decoded[3].content
    assert pending == []
    again, pending_again = merge_events(merged, pending, second)
    assert [message.id for message in messages_from_dict(again)] == [message.id for message in decoded]
    assert pending_again == []


def test_same_tool_call_id_in_two_runs_never_crosses_results():
    events = [
        _work("a-call", "A", AIMessage(
            id="a-ai", content="", tool_calls=[{"name": "bash_tool", "args": {}, "id": "same"}],
        )),
        _work("b-call", "B", AIMessage(
            id="b-ai", content="", tool_calls=[{"name": "bash_tool", "args": {}, "id": "same"}],
        )),
        _work("b-result", "B", ToolMessage(id="b-result", content="B result", tool_call_id="same")),
        _work("a-result", "A", ToolMessage(id="a-result", content="A result", tool_call_id="same")),
    ]
    merged, pending = merge_events([], [], events)
    decoded = messages_from_dict(merged)
    assert [message.id for message in decoded] == ["b-ai", "b-result", "a-ai", "a-result"]
    assert pending == []
