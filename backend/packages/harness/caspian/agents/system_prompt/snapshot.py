"""
本文件对外提供 SystemFragment、SystemSnapshot 与 SystemSnapshotBuilder。

输入:
    静态 Caspian prompt、plan 状态、delegations、已注册 runtime fragments、authoritative table

输出:
    SystemSnapshot — 不可变的完整 system 内容、SHA-256 fingerprint 与决策表版本

具体工作流:
    builder 按 static → plan → delegation → fragments → Decision Table 的固定顺序组装；
    fingerprint 覆盖精确模型可见文本，任何动态段变化都会生成新值，空动态段不改变静态 prompt。

示例:
    snapshot = SystemSnapshotBuilder("base", plan_section="plan").build(plan_active=True)
"""

from dataclasses import dataclass
from hashlib import sha256
from typing import Iterable, Mapping, Sequence

from caspian.agents.commitment.decision_table import DecisionTable
from caspian.agents.middlewares.delegation_ledger import render_delegation_ledger
from caspian.agents.system_prompt.decision_table import render_decision_table_section


@dataclass(frozen=True, slots=True)
class SystemFragment:
    """带稳定 identity 的可合并 system 片段。"""

    identity: str
    content: str


@dataclass(frozen=True, slots=True)
class SystemSnapshot:
    """一次完整、可审计的有效 system 快照。"""

    content: str
    fingerprint: str
    decision_table_version: str | None

    @classmethod
    def from_content(
        cls,
        content: str,
        *,
        decision_table_version: str | None = None,
    ) -> "SystemSnapshot":
        return cls(
            content=content,
            fingerprint=sha256(content.encode("utf-8")).hexdigest(),
            decision_table_version=decision_table_version,
        )


class SystemSnapshotBuilder:
    """只负责按稳定顺序组装完整 snapshot。"""

    def __init__(self, static_prompt: str, *, plan_section: str = "") -> None:
        self._static_prompt = static_prompt.strip()
        self._plan_section = plan_section.strip()

    @property
    def static_prompt(self) -> str:
        return self._static_prompt

    def build(
        self,
        *,
        plan_active: bool = False,
        delegations: Sequence[Mapping[str, object]] = (),
        fragments: Iterable[SystemFragment] = (),
        decision_table: DecisionTable | None = None,
    ) -> SystemSnapshot:
        sections = [self._static_prompt] if self._static_prompt else []
        if plan_active and self._plan_section:
            sections.append(
                f"<plan_mode_policy>\n{self._plan_section}\n</plan_mode_policy>"
            )
        ledger = render_delegation_ledger(list(delegations))
        if ledger:
            sections.append(ledger.strip())
        sections.extend(
            fragment.content.strip()
            for fragment in fragments
            if fragment.content.strip()
        )
        if decision_table is not None:
            sections.append(render_decision_table_section(decision_table))
        content = "\n\n".join(sections)
        return SystemSnapshot.from_content(
            content,
            decision_table_version=(decision_table.version if decision_table else None),
        )
