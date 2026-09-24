"""
本文件对外提供 span-only 事实簇切分、原子性与时态锚点模型协议。

对外提供:
    SemanticTemporalBinding / SemanticSpan / SemanticSegmentationOutput — 受限结构化输出
    SemanticSpanSegmenter — 调用聊天模型并返回 UnitBoundary，不生成或改写正文

输入:
    带 section refs 的 CandidateBlock、聊天模型和超时秒数。

输出:
    tuple[UnitBoundary, ...]；模型协议、时态锚点或 JSON 无效时抛 EvidenceValidationError。

具体工作流:
    固定 prompt 与 few-shot 定义事实簇、atomic/indivisible 和弱长度偏好；优先结构化输出，
    失败后解析相同 JSON 协议；模型 binding 转为绝对原文 span，全覆盖由 chunking 验证。

示例:
    spans = await SemanticSpanSegmenter(model).split(candidate)
"""

from __future__ import annotations

import asyncio
import json

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from caspian.knowledge.evidence import (
    CandidateBlock,
    EvidenceValidationError,
    SourceSpan,
    TemporalBinding,
    UnitBoundary,
)
from caspian.knowledge.json_parsing import parse_fenced_or_raw


class SemanticTemporalBinding(BaseModel):
    field: str
    value: str
    anchor_text: str
    source_kind: str
    start: int
    end: int
    section_index: int | None = None


class SemanticSpan(BaseModel):
    start: int
    end: int
    atomicity: str = "atomic"
    temporal_bindings: list[SemanticTemporalBinding] = Field(default_factory=list)
    reason: str = ""


class SemanticSegmentationOutput(BaseModel):
    spans: list[SemanticSpan] = Field(default_factory=list)


_SYSTEM_PROMPT = """你负责把候选原文划分成 Evidence Unit：可独立评级、召回和冲突治理的最小完整事实簇。
多句话若共同说明同一 API、行为、条件、因果或版本，必须保持一个 span；不同主题或不同版本才拆分。
依赖前后句才能成立的陈述不可孤立。150–400 tokens 只是弱偏好，事实簇完整性优先；每个 span 不得超过 600 tokens。
atomicity=atomic 表示一个完整事实簇；只有继续拆分会破坏独立解释的复合事实簇才能标 indivisible。
只返回原文半开字符坐标、atomicity、可选 temporal_bindings 和简短 reason，禁止返回、改写或摘要 chunk 正文。
content binding 的 start/end 相对 candidate；heading binding 的 start/end 相对 sections[section_index].title。
temporal_bindings 只允许 version/published_at/effective_at，anchor_text 必须精确等于对应原文切片。
spans 必须升序、不重叠并覆盖全部非空白原文。"""

_SAME_CLUSTER = "Foo defaults to 20. This default also applies to retries."
_MULTI_TOPIC = "Foo defaults to 20.\nBar supports Windows."
_DEPENDENT = "A is enabled only when B is enabled; disabling B also disables A."
_VERSIONED = "React 18 uses the old behavior.\nReact 19 uses the new behavior."


def _few_shot_messages() -> list:
    multi_break = _MULTI_TOPIC.index("\n")
    version_break = _VERSIONED.index("\n")
    examples = [
        (_SAME_CLUSTER, [{"start": 0, "end": len(_SAME_CLUSTER), "atomicity": "atomic", "temporal_bindings": [], "reason": "same fact cluster"}]),
        (_MULTI_TOPIC, [
            {"start": 0, "end": multi_break, "atomicity": "atomic", "temporal_bindings": [], "reason": "Foo fact"},
            {"start": multi_break + 1, "end": len(_MULTI_TOPIC), "atomicity": "atomic", "temporal_bindings": [], "reason": "Bar fact"},
        ]),
        (_DEPENDENT, [{"start": 0, "end": len(_DEPENDENT), "atomicity": "indivisible", "temporal_bindings": [], "reason": "mutually dependent statements"}]),
        (_VERSIONED, [
            {"start": 0, "end": version_break, "atomicity": "atomic", "temporal_bindings": [{"field": "version", "value": "React 18", "anchor_text": "React 18", "source_kind": "content", "start": 0, "end": 8, "section_index": None}], "reason": "React 18"},
            {"start": version_break + 1, "end": len(_VERSIONED), "atomicity": "atomic", "temporal_bindings": [{"field": "version", "value": "React 19", "anchor_text": "React 19", "source_kind": "content", "start": version_break + 1, "end": version_break + 9, "section_index": None}], "reason": "React 19"},
        ]),
    ]
    messages: list = []
    for candidate, spans in examples:
        messages.append(HumanMessage(content=json.dumps({"kind": "paragraph", "sections": [], "candidate": candidate}, ensure_ascii=False)))
        messages.append(AIMessage(content=json.dumps({"spans": spans}, ensure_ascii=False)))
    return messages


def _absolute_binding(candidate: CandidateBlock, item: SemanticTemporalBinding) -> TemporalBinding:
    if item.field not in ("version", "published_at", "effective_at"):
        raise EvidenceValidationError("invalid_temporal_field", "模型返回了不支持的时态字段")
    if item.value.strip() != item.anchor_text.strip():
        raise EvidenceValidationError("temporal_value_unanchored", "时态 metadata value 必须等于原文 anchor")
    if item.source_kind == "content":
        if not (0 <= item.start < item.end <= len(candidate.content)) or candidate.content[item.start:item.end] != item.anchor_text:
            raise EvidenceValidationError("temporal_anchor_mismatch", "正文时态 anchor 与候选原文不一致")
        return TemporalBinding(
            field=item.field,
            value=item.value,
            source_kind="content",
            anchor_text=item.anchor_text,
            source_span=SourceSpan(
                start=candidate.source_span.start + item.start,
                end=candidate.source_span.start + item.end,
            ),
        )
    if item.source_kind != "heading" or item.section_index is None:
        raise EvidenceValidationError("invalid_temporal_source", "时态 binding 必须引用 content 或 heading")
    if not (0 <= item.section_index < len(candidate.section_refs)):
        raise EvidenceValidationError("invalid_heading_reference", "时态 binding 引用了不存在的 heading")
    reference = candidate.section_refs[item.section_index]
    if not (0 <= item.start < item.end <= len(reference.title)) or reference.title[item.start:item.end] != item.anchor_text:
        raise EvidenceValidationError("temporal_anchor_mismatch", "heading 时态 anchor 与标题原文不一致")
    return TemporalBinding(
        field=item.field,
        value=item.value,
        source_kind="heading",
        anchor_text=item.anchor_text,
        source_span=SourceSpan(
            start=reference.source_span.start + item.start,
            end=reference.source_span.start + item.end,
        ),
    )


def _boundaries(candidate: CandidateBlock, parsed: SemanticSegmentationOutput) -> tuple[UnitBoundary, ...]:
    result: list[UnitBoundary] = []
    for item in parsed.spans:
        if item.atomicity not in ("atomic", "indivisible"):
            raise EvidenceValidationError("invalid_atomicity", "模型返回了不支持的 atomicity")
        result.append(
            UnitBoundary(
                source_span=SourceSpan(start=item.start, end=item.end),
                atomicity=item.atomicity,
                temporal_bindings=tuple(_absolute_binding(candidate, binding) for binding in item.temporal_bindings),
            )
        )
    return tuple(result)


class SemanticSpanSegmenter:
    def __init__(self, model: BaseChatModel, *, timeout_seconds: float = 120.0):
        self._model = model
        self._timeout_seconds = timeout_seconds

    async def split(self, candidate: CandidateBlock) -> tuple[SourceSpan, ...]:
        payload = json.dumps(
            {
                "kind": candidate.kind,
                "section_path": list(candidate.section_path),
                "sections": [
                    {"index": index, "title": reference.title}
                    for index, reference in enumerate(candidate.section_refs)
                ],
                "length_chars": len(candidate.content),
                "candidate": candidate.content,
            },
            ensure_ascii=False,
        )
        bound = self._model.bind(max_tokens=2048)
        messages = [SystemMessage(content=_SYSTEM_PROMPT), *_few_shot_messages(), HumanMessage(content=payload)]
        try:
            structured = bound.with_structured_output(
                SemanticSegmentationOutput,
                method="function_calling",
            )
            async with asyncio.timeout(self._timeout_seconds):
                parsed = await structured.ainvoke(messages)
            if not isinstance(parsed, SemanticSegmentationOutput):
                raise TypeError("结构化输出类型异常")
            return _boundaries(candidate, parsed)
        except EvidenceValidationError:
            raise
        except Exception as structured_error:
            try:
                async with asyncio.timeout(self._timeout_seconds):
                    raw = await bound.ainvoke(
                        [*messages, HumanMessage(content='只返回 {"spans":[{"start":0,"end":1,"reason":""}]} JSON。')]
                    )
                data = parse_fenced_or_raw(str(raw.content))
                parsed = SemanticSegmentationOutput.model_validate(data)
                return _boundaries(candidate, parsed)
            except Exception as fallback_error:
                raise EvidenceValidationError(
                    "segmentation_failed",
                    "语义切分模型未返回合法 span 协议",
                    details={
                        "structured_error": type(structured_error).__name__,
                        "fallback_error": type(fallback_error).__name__,
                    },
                ) from fallback_error
