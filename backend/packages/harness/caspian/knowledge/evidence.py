"""
本文件对外提供 Evidence Unit 入库领域值、原子性/时态绑定值与结构化错误。

对外提供:
    DocumentInput — 已提取文档及来源、版本、时间元数据；content 保持原文
    SourceSpan — 原文半开字符区间 [start, end)
    SectionReference / TemporalBinding — 可机械锚定的 heading 与时态元数据
    CandidateBlock / UnitBoundary — 结构候选块与语义边界决定
    EvidenceUnitDraft / EvidenceUnit — 评级前草稿与可持久化证据单元
    DocumentIngestionResult — 文档批量入库的稳定身份和有序 chunk 结果
    EvidenceValidationError / EvidencePersistenceError — 可映射为 API 错误的异常

输入:
    文档正文、格式、来源身份、结构位置、原子性、时态锚点、检索文本、评级和治理元数据。

输出:
    冻结的 Pydantic 领域对象；EvidenceUnit.to_store_value() 输出统一 Store value。

具体工作流:
    文档输入先形成带 heading refs 的 CandidateBlock；语义模型只产生 UnitBoundary，
    时态绑定经机械校验后形成 Draft；评级完成后生成 EvidenceUnit 并持久化。
    所有 span 都使用 Python 字符串半开下标。

示例:
    document = DocumentInput(content="事实。", source="官方文档")
    span = SourceSpan(start=0, end=3)
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


DocumentFormat = Literal["markdown", "text"]
BlockKind = Literal["paragraph", "list", "table", "code"]
Atomicity = Literal["atomic", "indivisible", "legacy_unknown"]
IngestedAtomicity = Literal["atomic", "indivisible"]
TemporalField = Literal["version", "published_at", "effective_at"]
TemporalSourceKind = Literal["content", "heading", "document"]


class SourceSpan(BaseModel):
    model_config = ConfigDict(frozen=True)

    start: int = Field(ge=0, strict=True)
    end: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def _ordered(self) -> SourceSpan:
        if self.end <= self.start:
            raise ValueError("source span 必须满足 start < end")
        return self


class DocumentInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    content: str
    format: DocumentFormat = "markdown"
    external_source_id: str | None = None
    source: str = ""
    source_url: str | None = None
    title: str = ""
    published_at: str | None = None
    effective_at: str | None = None
    version: str | None = None

    @field_validator("content")
    @classmethod
    def _content_present(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("content 不能为空")
        return value


class SectionReference(BaseModel):
    model_config = ConfigDict(frozen=True)

    title: str
    source_span: SourceSpan


class TemporalBinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    field: TemporalField
    value: str
    source_kind: TemporalSourceKind
    anchor_text: str = ""
    source_span: SourceSpan | None = None

    @model_validator(mode="after")
    def _anchor_contract(self) -> TemporalBinding:
        if not self.value.strip():
            raise ValueError("temporal binding value 不能为空")
        if self.source_kind == "document":
            if self.source_span is not None:
                raise ValueError("document temporal binding 不得伪造 source span")
            return self
        if self.source_span is None or not self.anchor_text:
            raise ValueError("content/heading temporal binding 必须包含 anchor_text/source_span")
        return self


class CandidateBlock(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: BlockKind
    content: str
    source_span: SourceSpan
    section_path: tuple[str, ...] = ()
    section_refs: tuple[SectionReference, ...] = ()
    structural_index: int = Field(ge=0)
    atomicity: IngestedAtomicity = "atomic"
    temporal_bindings: tuple[TemporalBinding, ...] = ()


class UnitBoundary(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_span: SourceSpan
    atomicity: IngestedAtomicity = "atomic"
    temporal_bindings: tuple[TemporalBinding, ...] = ()


class EvidenceUnitDraft(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk_id: str
    document_id: str
    document_revision_id: str
    content: str
    retrieval_text: str
    title: str = ""
    section_path: tuple[str, ...] = ()
    chunk_index: int = Field(ge=0)
    source_span: SourceSpan
    source: str = ""
    source_url: str | None = None
    published_at: str | None = None
    effective_at: str | None = None
    version: str | None = None
    temporal_bindings: tuple[TemporalBinding, ...] = ()
    atomicity: IngestedAtomicity = "atomic"


class EvidenceUnit(EvidenceUnitDraft):
    level: int | None = Field(default=None, ge=0, le=3)
    level_basis: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)

    def to_store_value(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "record_type": "evidence_unit",
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "document_revision_id": self.document_revision_id,
            "content": self.content,
            "retrieval_text": self.retrieval_text,
            "title": self.title,
            "section_path": list(self.section_path),
            "chunk_index": self.chunk_index,
            "source_span": self.source_span.model_dump(),
            "source": self.source,
            "source_url": self.source_url,
            "published_at": self.published_at,
            "effective_at": self.effective_at,
            "version": self.version,
            "temporal_bindings": [binding.model_dump(mode="json") for binding in self.temporal_bindings],
            "atomicity": self.atomicity,
            "level": self.level,
            "level_basis": self.level_basis,
            "provenance": self.provenance,
        }


class DocumentIngestionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    document_id: str
    document_revision_id: str
    count: int = Field(ge=0)
    chunk_ids: tuple[str, ...]


class EvidenceValidationError(ValueError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class EvidencePersistenceError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        completed_ids: tuple[str, ...] = (),
        pending_ids: tuple[str, ...] = (),
    ):
        super().__init__(message)
        self.completed_ids = completed_ids
        self.pending_ids = pending_ids
