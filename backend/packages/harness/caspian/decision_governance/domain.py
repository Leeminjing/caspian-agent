"""本文件对外提供 Row、TableSnapshot、RowChange、diff_rows 和摘要函数。

输入为带稳定 ID 的完整候选行与当前快照；输出为不可变行差异、内容摘要及单调修订身份。
工作流先规范化并校验每行，再按 ID 比较新增、修改、删除和等级调整；修订身份由序号和内容摘要组成。
示例：`changes = diff_rows((Row("r1", "要求", "保留", 2),), ())`。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

ChangeKind = Literal["add", "modify", "delete", "change_priority"]


@dataclass(frozen=True)
class Row:
    id: str
    requirement: str
    decision: str
    priority: int
    guards: tuple[dict[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.requirement.strip():
            raise ValueError("决策行 ID 与要求不能为空")
        if self.decision not in {"保留", "丢弃"}:
            raise ValueError("决策值无效")
        if isinstance(self.priority, bool) or self.priority not in range(4):
            raise ValueError("决策等级必须是 0 到 3")
        if any(not isinstance(guard, dict) for guard in self.guards):
            raise ValueError("守卫必须是对象")
        if self.guards:
            from caspian.agents.commitment.decision_table import Guard
            if any(Guard.from_dict(guard) is None for guard in self.guards):
                raise ValueError("守卫规则无效")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "requirement": self.requirement,
            "decision": self.decision,
            "priority": self.priority,
            "guards": [dict(guard) for guard in self.guards],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Row:
        if not isinstance(value, dict):
            raise ValueError("决策行必须是对象")
        row_id = value.get("id")
        requirement = value.get("requirement")
        decision = value.get("decision")
        priority = value.get("priority")
        guards = value.get("guards", [])
        if not isinstance(row_id, str) or not row_id.strip():
            raise ValueError("决策行必须包含稳定 ID")
        if not isinstance(requirement, str) or not requirement.strip():
            raise ValueError("决策要求不能为空")
        if decision not in {"保留", "丢弃"}:
            raise ValueError("决策值无效")
        if isinstance(priority, bool) or not isinstance(priority, int) or priority not in range(4):
            raise ValueError("决策等级必须是 0 到 3")
        if not isinstance(guards, list) or any(not isinstance(item, dict) for item in guards):
            raise ValueError("守卫必须是对象列表")
        return cls(row_id, requirement, decision, priority, tuple(dict(item) for item in guards))


@dataclass(frozen=True)
class TableSnapshot:
    revision: int
    content_hash: str
    rows: tuple[Row, ...]

    @property
    def version(self) -> str:
        return f"{self.revision}:{self.content_hash}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "version": self.version,
            "content_hash": self.content_hash,
            "rows": [row.to_dict() for row in self.rows],
        }


@dataclass(frozen=True)
class RowChange:
    kind: ChangeKind
    row_id: str
    before: Row | None
    after: Row | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "row_id": self.row_id,
            "before": self.before.to_dict() if self.before else None,
            "after": self.after.to_dict() if self.after else None,
        }


def content_hash(rows: tuple[Row, ...]) -> str:
    canonical = json.dumps(
        [row.to_dict() for row in rows],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def snapshot(revision: int, rows: tuple[Row, ...]) -> TableSnapshot:
    if revision < 0:
        raise ValueError("修订序号不能为负")
    ids = [row.id for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("决策行 ID 重复")
    return TableSnapshot(revision, content_hash(rows), rows)


def diff_rows(before: tuple[Row, ...], after: tuple[Row, ...]) -> tuple[RowChange, ...]:
    if len({row.id for row in before}) != len(before) or len({row.id for row in after}) != len(after):
        raise ValueError("决策行 ID 重复")
    old = {row.id: row for row in before}
    new = {row.id: row for row in after}
    changes: list[RowChange] = []
    for row in before:
        candidate = new.get(row.id)
        if candidate is None:
            changes.append(RowChange("delete", row.id, row, None))
        elif candidate != row:
            kind: ChangeKind = (
                "change_priority"
                if row.priority != candidate.priority
                and row.requirement == candidate.requirement
                and row.decision == candidate.decision
                and row.guards == candidate.guards
                else "modify"
            )
            changes.append(RowChange(kind, row.id, row, candidate))
    for row in after:
        if row.id not in old:
            changes.append(RowChange("add", row.id, None, row))
    return tuple(changes)
