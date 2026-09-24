"""
本文件对外提供 Evidence Unit 时态锚点提取、验证与字段级解析函数。

对外提供:
    extract_heading_temporal_bindings — 从最近 heading 的明确版本/日期文本生成原文绑定
    validate_temporal_bindings — 验证正文、heading、document 三类 binding 的归属与原文锚点
    resolve_temporal_metadata — 按 content、最近 heading、document 的优先级解析单元字段
    ResolvedTemporalMetadata — 解析后的版本、发布时间、生效时间与最终 bindings

输入:
    完整文档、DocumentInput、带绝对 source span/section refs 的 CandidateBlock 及可选模型 bindings。

输出:
    冻结的 ResolvedTemporalMetadata；越界、跨单元、错误 heading 或原文不匹配时抛
    EvidenceValidationError。

具体工作流:
    先从最近 heading 提取明确时态文本，再验证全部原文锚点；同字段按 content、heading、
    document 逐级选择。相同优先级出现不同值时拒绝，避免把多版本压成一个单元。

示例:
    metadata = resolve_temporal_metadata(document.content, document, block)
    assert metadata.version == "React 19"
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict

from caspian.knowledge.evidence import (
    CandidateBlock,
    DocumentInput,
    EvidenceValidationError,
    SectionReference,
    TemporalBinding,
    TemporalField,
)


_PRODUCT_VERSION = re.compile(
    r"(?i)(?<![\w.-])(?:[A-Za-z][A-Za-z0-9_.-]*\s+v?\d+(?:\.\d+){0,3}|(?:version|版本)\s*[:：]?\s*v?\d+(?:\.\d+){0,3}|v\d+(?:\.\d+){1,3})(?![\w.-])"
)
_PUBLISHED_DATE = re.compile(r"(?i)(?:published|released|发布(?:于|日期)?)[\s:：]*(\d{4}-\d{2}-\d{2})")
_EFFECTIVE_DATE = re.compile(r"(?i)(?:effective|生效(?:于|日期)?)[\s:：]*(\d{4}-\d{2}-\d{2})")


class ResolvedTemporalMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: str | None = None
    published_at: str | None = None
    effective_at: str | None = None
    bindings: tuple[TemporalBinding, ...] = ()


def _binding_from_match(
    field: TemporalField,
    value: str,
    reference: SectionReference,
    start: int,
    end: int,
) -> TemporalBinding:
    from caspian.knowledge.evidence import SourceSpan

    return TemporalBinding(
        field=field,
        value=value,
        source_kind="heading",
        anchor_text=reference.title[start:end],
        source_span=SourceSpan(
            start=reference.source_span.start + start,
            end=reference.source_span.start + end,
        ),
    )


def _heading_bindings(reference: SectionReference) -> list[TemporalBinding]:
    bindings: list[TemporalBinding] = []
    version = _PRODUCT_VERSION.search(reference.title)
    if version:
        bindings.append(_binding_from_match("version", version.group(0), reference, version.start(), version.end()))
    for field, pattern in (("published_at", _PUBLISHED_DATE), ("effective_at", _EFFECTIVE_DATE)):
        match = pattern.search(reference.title)
        if match:
            bindings.append(_binding_from_match(field, match.group(1), reference, match.start(1), match.end(1)))
    return bindings


def extract_heading_temporal_bindings(block: CandidateBlock) -> tuple[TemporalBinding, ...]:
    selected: dict[TemporalField, TemporalBinding] = {}
    for reference in reversed(block.section_refs):
        for binding in _heading_bindings(reference):
            selected.setdefault(binding.field, binding)
    return tuple(selected[field] for field in ("version", "published_at", "effective_at") if field in selected)


def _inside(inner_start: int, inner_end: int, outer_start: int, outer_end: int) -> bool:
    return outer_start <= inner_start < inner_end <= outer_end


def _validate_binding(document: str, block: CandidateBlock, binding: TemporalBinding) -> None:
    if binding.source_kind == "document":
        return
    span = binding.source_span
    if binding.value.strip() != binding.anchor_text.strip():
        raise EvidenceValidationError("temporal_value_unanchored", "时态 metadata value 必须等于原文 anchor")
    if span is None or span.end > len(document) or document[span.start:span.end] != binding.anchor_text:
        raise EvidenceValidationError("temporal_anchor_mismatch", "时态 metadata anchor 无法精确还原原文")
    if binding.source_kind == "content":
        if not _inside(span.start, span.end, block.source_span.start, block.source_span.end):
            raise EvidenceValidationError("temporal_anchor_outside_unit", "正文时态 anchor 不属于当前 Evidence Unit")
        return
    if not any(
        _inside(span.start, span.end, reference.source_span.start, reference.source_span.end)
        for reference in block.section_refs
    ):
        raise EvidenceValidationError("temporal_heading_not_inherited", "heading 时态 anchor 不属于当前 section path")


def validate_temporal_bindings(
    document: str,
    block: CandidateBlock,
    bindings: tuple[TemporalBinding, ...],
) -> tuple[TemporalBinding, ...]:
    for binding in bindings:
        _validate_binding(document, block, binding)
    return bindings


def _document_bindings(document: DocumentInput) -> tuple[TemporalBinding, ...]:
    values = {
        "version": document.version,
        "published_at": document.published_at,
        "effective_at": document.effective_at,
    }
    return tuple(
        TemporalBinding(field=field, value=value, source_kind="document", anchor_text=value)
        for field, value in values.items()
        if value
    )


def _select_binding(bindings: tuple[TemporalBinding, ...], field: TemporalField) -> TemporalBinding | None:
    ranked = [binding for binding in bindings if binding.field == field]
    if not ranked:
        return None
    priority = {"content": 3, "heading": 2, "document": 1}
    best_priority = max(priority[binding.source_kind] for binding in ranked)
    best = [binding for binding in ranked if priority[binding.source_kind] == best_priority]
    values = {binding.value for binding in best}
    if len(values) > 1:
        raise EvidenceValidationError("temporal_value_conflict", f"同一 Evidence Unit 包含多个 {field} 值")
    return best[0]


def resolve_temporal_metadata(
    document_text: str,
    document: DocumentInput,
    block: CandidateBlock,
) -> ResolvedTemporalMetadata:
    candidates = (
        *validate_temporal_bindings(document_text, block, block.temporal_bindings),
        *validate_temporal_bindings(document_text, block, extract_heading_temporal_bindings(block)),
        *_document_bindings(document),
    )
    selected = tuple(
        binding
        for field in ("version", "published_at", "effective_at")
        if (binding := _select_binding(candidates, field)) is not None
    )
    values = {binding.field: binding.value for binding in selected}
    return ResolvedTemporalMetadata(
        version=values.get("version"),
        published_at=values.get("published_at"),
        effective_at=values.get("effective_at"),
        bindings=selected,
    )
