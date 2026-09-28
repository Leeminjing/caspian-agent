"""本文件对外提供操作状态机、请求摘要和审批绑定验证。

输入为操作状态、规范化请求字段及审批凭证；输出为合法状态跃迁或确定性摘要。
工作流拒绝终态再次跃迁，并将候选、基准和权限修订纳入审批绑定。
示例：`transition("processing", "awaiting_approval")`。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

TERMINAL = frozenset({"committed", "rejected", "cancelled", "failed", "version_conflict"})
_NEXT = {
    "processing": frozenset({"awaiting_approval", *TERMINAL}),
    "awaiting_approval": TERMINAL,
}


def transition(current: str, target: str) -> str:
    if target not in _NEXT.get(current, frozenset()):
        raise ValueError(f"非法操作状态跃迁: {current} → {target}")
    return target


def digest(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ApprovalBinding:
    operation_id: str
    candidate_hash: str
    base_revision: int
    permission_revision: int
    approver_id: str
    allowed_ops: tuple[str, ...]

    def matches(
        self,
        *,
        operation_id: str,
        candidate_hash: str,
        base_revision: int,
        permission_revision: int,
        allowed_ops: tuple[str, ...],
    ) -> bool:
        return (
            self.operation_id == operation_id
            and self.candidate_hash == candidate_hash
            and self.base_revision == base_revision
            and self.permission_revision == permission_revision
            and set(self.allowed_ops) >= set(allowed_ops)
        )
