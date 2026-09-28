"""本文件对外提供 Actor、权限策略规范化及逐差异授权判定。

输入为认证得到的 actor、版本化策略、行为与差异类型；输出为允许或拒绝及缺失权限。
工作流对复合请求逐项求交；用户默认仅管理自己会话，Agent 无默认权利。
示例：`allowed, missing = authorize(default_policy("u"), Actor("u", "user"), "direct_commit", ("add",))`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

BEHAVIORS = frozenset({"view", "propose", "approve", "direct_commit"})
OPERATIONS = frozenset({"add", "modify", "delete", "change_priority"})


@dataclass(frozen=True)
class Actor:
    id: str
    kind: str

    @property
    def subject(self) -> str:
        return f"{self.kind}:{self.id}"


def default_policy(owner_id: str) -> dict[str, Any]:
    return {
        "subjects": {
            f"user:{owner_id}": {behavior: sorted(OPERATIONS) for behavior in BEHAVIORS},
            "agent:lead": {"view": sorted(OPERATIONS)},
            "agent:subagent": {"view": sorted(OPERATIONS)},
        }
    }


def validate_policy(policy: dict[str, Any]) -> None:
    if not isinstance(policy, dict) or not isinstance(policy.get("subjects"), dict):
        raise ValueError("权限策略缺少 subjects")
    for subject, grants in policy["subjects"].items():
        if not isinstance(subject, str) or ":" not in subject or not isinstance(grants, dict):
            raise ValueError("权限主体无效")
        for behavior, operations in grants.items():
            if behavior not in BEHAVIORS or not isinstance(operations, list) or not set(operations) <= OPERATIONS:
                raise ValueError("权限行为或操作类型无效")


def authorize(
    policy: dict[str, Any], actor: Actor, behavior: str, operations: Iterable[str]
) -> tuple[bool, tuple[str, ...]]:
    validate_policy(policy)
    if behavior not in BEHAVIORS:
        raise ValueError("未知权限行为")
    requested = set(operations)
    if not requested <= OPERATIONS:
        raise ValueError("未知改表类型")
    grants = policy["subjects"].get(actor.subject, {})
    permitted = set(grants.get(behavior, []))
    missing = tuple(sorted(requested - permitted))
    return (bool(permitted) if not requested else not missing), missing
