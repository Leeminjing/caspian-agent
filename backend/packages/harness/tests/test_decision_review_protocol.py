"""本文件验证动作前复核结论必须绑定待执行动作并明确处理冲突。

输入为动作、参数摘要、表修订和结构化模型结论；输出为可执行或拒绝的判定。
工作流检查调用绑定、相关行、冲突状态和修改参数，空泛及无法确认结论不能放行。
示例：`pytest tests/test_decision_review_protocol.py`。
"""

import json

import pytest

from caspian.decision_governance.review_protocol import ActionBinding, parse_conclusion


def test_review_requires_exact_action_binding_and_nonempty_reason():
    binding = ActionBinding.create("bash_tool", {"command": "echo hi"}, "lead", 2)
    valid = {
        "action_name": "bash_tool", "args_hash": binding.args_hash,
        "actor_id": "lead", "table_revision": 2, "examined_args": {"command": "echo hi"},
        "related_rows": ["row-1"], "conflict": "none", "decision": "keep",
        "reason": "已逐项检查命令与 row-1，确认不冲突",
    }
    assert parse_conclusion(json.dumps(valid), binding, {"row-1"}, {"command": "echo hi"})["decision"] == "keep"
    for changed in (
        {"args_hash": "wrong"},
        {"actor_id": "other"},
        {"table_revision": 1},
        {"examined_args": {"command": "different"}},
        {"related_rows": ["missing"]},
        {"reason": "已遵守"},
        {"related_rows": [], "reason": "我已遵守当前决策表"},
        {"conflict": "uncertain"},
    ):
        with pytest.raises(ValueError):
            parse_conclusion(json.dumps({**valid, **changed}), binding, {"row-1"}, {"command": "echo hi"})
    with pytest.raises(ValueError):
        parse_conclusion(json.dumps({**valid, "decision": "modify"}), binding, {"row-1"}, {"command": "echo hi"})
