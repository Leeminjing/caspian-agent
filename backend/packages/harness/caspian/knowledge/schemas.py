"""
本文件对外提供离散等级治理 RAG 的领域 schema 与等级工具函数。

对外提供:
    EvidenceEntry — 候选证据条目（含离散权威等级与相似度）
    ConflictRelation — judge 输出的两两冲突关系
    JudgeConflictOutput — judge 结构化输出根 schema
    LedgerEntry — 治理账本条目（状态 + 原因 + 被压命题）
    FinalEvidence — 允许参与回答的最终证据
    GovernanceResult — 单次查询的治理结果
    level_display / level_value — 等级展示与比较值工具函数

输入: 无 — 本文件为纯定义文件

输出: 上述 Pydantic 模型类与纯函数

具体工作流:
    level_display: level 为 None → "未评级"；0-3 → "L0"-"L3"
    level_value: level 为 None → 0（未评级按预定义最低默认等级参与比较）；
        0-3 → 原值

示例:
    from caspian.knowledge.schemas import EvidenceEntry, level_display

    entry = EvidenceEntry(id="k1", content="功能 A 已废弃。", level=3, score=0.61)
    assert level_display(None) == "未评级"
"""

from typing import Literal

from pydantic import BaseModel, Field

_LEVEL_NAMES: dict[int, str] = {0: "L0", 1: "L1", 2: "L2", 3: "L3"}

_VALID_LEVELS: frozenset = frozenset(_LEVEL_NAMES)


def level_display(level: int | None) -> str:
    """将等级值转为展示文本（None → 未评级）。"""
    if level is None:
        return "未评级"
    return _LEVEL_NAMES.get(level, str(level))


def level_value(level: int | None) -> int:
    """将等级值转为参与比较的数值（None 按最低默认等级 0 处理）。"""
    if level is None:
        return 0
    if level not in _VALID_LEVELS:
        raise ValueError(f"非法等级: {level}，允许 0-3 或 null")
    return level


class EvidenceEntry(BaseModel):
    """候选证据条目。等级属于来源属性；score 仅用于召回排序，治理引擎不读取。"""

    id: str
    content: str
    level: int | None = None
    score: float | None = None
    source: str = ""
    source_url: str | None = None

    @property
    def level_display(self) -> str:
        return level_display(self.level)


class ConflictRelation(BaseModel):
    """judge 输出的两两冲突关系。

    relation: explicit=明确冲突（可触发等级压制）；potential=可能冲突（不压制）
    scope: full=整体冲突；partial=仅 claim_a/claim_b 所述命题冲突
    """

    a: str
    b: str
    relation: Literal["explicit", "potential"]
    scope: Literal["full", "partial"] = "full"
    claim_a: str = ""
    claim_b: str = ""


class JudgeConflictOutput(BaseModel):
    """judge 结构化输出根 schema。"""

    conflicts: list[ConflictRelation] = Field(default_factory=list)


class LedgerEntry(BaseModel):
    """治理账本条目：每条候选证据的本次查询决策状态。"""

    id: str
    level_display: str
    status: Literal[
        "retained",
        "retained_partial",
        "suppressed",
        "conflict_same_level",
        "potential_conflict",
    ]
    reason: str = ""
    suppressed_claims: list[str] = Field(default_factory=list)


class FinalEvidence(BaseModel):
    """允许参与回答的最终证据（内容不删改，仅附被压命题注解）。"""

    id: str
    content: str
    level_display: str
    suppressed_claims: list[str] = Field(default_factory=list)


class GovernanceResult(BaseModel):
    """单次查询的治理结果，零持久化（查询级、命题级）。"""

    final_evidence_set: list[FinalEvidence]
    ledger: list[LedgerEntry]
    notes: list[str] = Field(default_factory=list)
