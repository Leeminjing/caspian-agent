"""本文件验证决策表权限对主体、行为和复合差异逐项生效。

输入为所有者、只读 Agent、提案 Agent 及四类改表授权；输出为明确允许或缺失权限。
工作流核对主体身份与每类差异的交集，防止声称获得用户同意绕过授权。
示例：`pytest tests/test_decision_governance_policy.py`。
"""

from caspian.decision_governance.policy import Actor, authorize, default_policy


def test_permissions_are_separate_by_actor_behavior_and_change_type():
    policy = default_policy("u")
    policy["subjects"]["agent:reader"] = {"view": ["add", "modify", "delete", "change_priority"]}
    policy["subjects"]["agent:proposer"] = {"view": ["add"], "propose": ["add", "modify"]}
    policy["subjects"]["agent:editor"] = {"view": ["add"], "direct_commit": ["add"]}
    assert authorize(policy, Actor("reader", "agent"), "view", ())[0]
    assert not authorize(policy, Actor("reader", "agent"), "propose", ("add",))[0]
    assert authorize(policy, Actor("proposer", "agent"), "propose", ("add", "modify"))[0]
    assert authorize(policy, Actor("proposer", "agent"), "direct_commit", ("add",))[0] is False
    assert authorize(policy, Actor("editor", "agent"), "direct_commit", ("add", "delete")) == (False, ("delete",))
    assert authorize(policy, Actor("u", "user"), "direct_commit", ("delete", "change_priority"))[0]
    assert not authorize(policy, Actor("u", "agent"), "direct_commit", ("add",))[0]
