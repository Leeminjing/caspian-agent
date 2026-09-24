"""
本文件对外提供离散等级治理 RAG 的领域 schema 与等级工具函数。

对外提供:
    EvidenceEntry — 候选证据条目（含离散权威等级、相似度、atomicity 及时态追踪元数据）
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
    level_value: 0-3 → 原值；None → 抛 ValueError（未评级不参与等级比较）

示例:
    from caspian.knowledge.schemas import EvidenceEntry, level_display

    entry = EvidenceEntry(id="k1", content="功能 A 已废弃。", level=3, score=0.61)
    assert level_display(None) == "未评级"
"""

from typing import Literal

from pydantic import BaseModel, Field

from caspian.knowledge.evidence import Atomicity, SourceSpan, TemporalBinding

_LEVEL_NAMES: dict[int, str] = {0: "L0", 1: "L1", 2: "L2", 3: "L3"}

_VALID_LEVELS: frozenset = frozenset(_LEVEL_NAMES)


def level_display(level: int | None) -> str:
    if level is None:
        return "未评级"
    return _LEVEL_NAMES.get(level, str(level))


def level_value(level: int | None) -> int:
    if level not in _VALID_LEVELS:
        raise ValueError(f"非法等级: {level}，允许 0-3；未评级 None 不参与等级比较")
    return level


class EvidenceEntry(BaseModel):
    id: str
    content: str
    level: int | None = None
    score: float | None = None
    source: str = ""
    source_url: str | None = None
    chunk_id: str | None = None
    document_id: str | None = None
    document_revision_id: str | None = None
    title: str = ""
    section_path: tuple[str, ...] = ()
    chunk_index: int | None = None
    source_span: SourceSpan | None = None
    version: str | None = None
    published_at: str | None = None
    effective_at: str | None = None
    temporal_bindings: tuple[TemporalBinding, ...] = ()
    atomicity: Atomicity = "legacy_unknown"
    legacy: bool = False

    @property
    def level_display(self) -> str:
        return level_display(self.level)


class ConflictRelation(BaseModel):
    a: str
    b: str
    relation: Literal["explicit", "potential", "temporal_disjoint"]
    scope: Literal["full", "partial"] = "full"
    claim_a: str = ""
    claim_b: str = ""
    claim_a_span: tuple[int, int] | None = None
    claim_b_span: tuple[int, int] | None = None


class JudgeConflictOutput(BaseModel):
    conflicts: list[ConflictRelation] = Field(default_factory=list)


class LedgerEntry(BaseModel):
    id: str
    level_display: str
    status: Literal[
        "retained",
        "retained_partial",
        "suppressed",
        "conflict_same_level",
        "potential_conflict",
        "unrated",
        "unadjudicated",
    ]
    reason: str = ""
    suppressed_claims: list[str] = Field(default_factory=list)


class FinalEvidence(BaseModel):
    id: str
    content: str
    level_display: str
    suppressed_claims: list[str] = Field(default_factory=list)


class GovernanceResult(BaseModel):
    final_evidence_set: list[FinalEvidence]
    ledger: list[LedgerEntry]
    notes: list[str] = Field(default_factory=list)
    status: Literal["governed", "unadjudicated", "empty"] = "governed"
    candidates: list[EvidenceEntry] = Field(default_factory=list)


class RatingDimensions(BaseModel):
    primary_source: int = Field(ge=1, le=3)
    domain_fit: int = Field(ge=1, le=3)
    evidence: int = Field(ge=1, le=3)
    specificity: int = Field(ge=1, le=3)


class RatingOutput(BaseModel):
    claim_domain: str = ""
    dimensions: RatingDimensions
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = ""
    unrated_reason: str | None = None


class LevelBasis(BaseModel):
    rated_by: str
    rated_at: str
    confidence: float | None = None
    reason: str = ""
    claim_domain: str = ""
    dimensions: RatingDimensions | None = None
    mapping_rule: str = ""
    unrated_reason: str | None = None
