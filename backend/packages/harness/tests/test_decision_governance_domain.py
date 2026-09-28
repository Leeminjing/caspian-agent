"""本文件验证决策表修订身份与逐行差异。

输入为有稳定 ID 的候选行；输出为断言通过或失败。
工作流构造 A→B→A 和复合变更，检验内容相同但修订不同及差异分类。
示例：`pytest tests/test_decision_governance_domain.py`。
"""

from caspian.decision_governance.domain import Row, diff_rows, snapshot


def test_revision_does_not_reuse_identity_after_revert():
    a = (Row("a", "要求 A", "保留", 2),)
    b = (Row("a", "要求 A", "保留", 3),)
    first, middle, restored = snapshot(0, a), snapshot(1, b), snapshot(2, a)
    assert first.content_hash == restored.content_hash
    assert first.version != restored.version
    assert middle.version != first.version


def test_diff_classifies_all_row_operations():
    before = (
        Row("keep", "保留行", "保留", 2),
        Row("priority", "等级行", "保留", 2),
        Row("modify", "修改行", "保留", 2),
        Row("delete", "删除行", "保留", 2),
    )
    after = (
        before[0],
        Row("priority", "等级行", "保留", 1),
        Row("modify", "修改行", "丢弃", 0),
        Row("add", "新增行", "保留", 3),
    )
    changes = diff_rows(before, after)
    assert [(item.row_id, item.kind) for item in changes] == [
        ("priority", "change_priority"),
        ("modify", "modify"),
        ("delete", "delete"),
        ("add", "add"),
    ]
