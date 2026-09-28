"""本文件验证语义检查事实与修改授权相互独立。

输入为旧表和候选等级变更；输出为需要审批的检查事实。
工作流复用确定性等级差检测，不调用外部模型。
示例：`pytest tests/test_decision_governance_checks.py`。
"""

import asyncio

from caspian.decision_governance.checks import check_candidate
from caspian.decision_governance.domain import Row
from caspian.decision_governance.operations import ApprovalBinding
from caspian.decision_governance.policy import Actor, authorize, default_policy


def test_priority_conflict_is_check_result_not_permission():
    old = (Row("r", "要求", "保留", 3),)
    new = (Row("r", "要求", "保留", 1),)
    findings = asyncio.run(check_candidate(new, old))
    assert findings[0]["requires_approval"]
    assert findings[0]["conflicts"]
    policy = default_policy("u")
    assert not authorize(policy, Actor("a", "agent"), "direct_commit", ("change_priority",))[0]
    assert authorize(policy, Actor("u", "user"), "direct_commit", ("change_priority",))[0]


def test_compound_request_requires_every_permission_and_agent_claim_grants_nothing():
    policy = default_policy("u")
    policy["subjects"]["agent:a"] = {
        "view": ["add"], "propose": ["add", "modify"],
    }
    agent = Actor("a", "agent")
    assert authorize(policy, agent, "view", ())[0]
    assert authorize(policy, agent, "propose", ("add", "modify"))[0]
    allowed, missing = authorize(policy, agent, "propose", ("add", "delete"))
    assert not allowed and missing == ("delete",)
    assert not authorize(policy, agent, "direct_commit", ("add",))[0]
    assert not authorize(policy, agent, "approve", ("add",))[0]


def test_approval_binding_rejects_changed_candidate_base_or_policy():
    binding = ApprovalBinding("op", "hash", 4, 2, "approver", ("add", "delete"))
    match = {
        "operation_id": "op", "candidate_hash": "hash", "base_revision": 4,
        "permission_revision": 2, "allowed_ops": ("add",),
    }
    assert binding.matches(**match)
    for field, changed in (
        ("operation_id", "other"), ("candidate_hash", "changed"),
        ("base_revision", 5), ("permission_revision", 3),
        ("allowed_ops", ("add", "modify")),
    ):
        assert not binding.matches(**{**match, field: changed})
