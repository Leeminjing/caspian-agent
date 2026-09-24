"""
本文件对外提供确定性结构切分、保守原子性闸门和语义边界机械验证函数。

对外提供:
    split_structural_blocks — 按 Markdown 或纯文本天然结构生成绝对坐标候选块
    evaluate_atomicity_gate — 用 hard max 与高置信多主题信号产生可审计 gate decision
    needs_semantic_split — 兼容返回上述 decision 的布尔值
    validate_semantic_spans — 验证局部 UnitBoundary 全覆盖、无重叠、非空且不超长
    to_absolute_blocks — 把合法局部边界转为带 atomicity/metadata 的绝对坐标候选块
    validate_ordered_non_overlapping — 验证最终 Evidence Unit 零正文 overlap

输入:
    原文、文档格式、CandidateBlock、模型返回的 UnitBoundary 和 TokenCounter。

输出:
    保持原文精确子串的 CandidateBlock 列表；非法边界抛 EvidenceValidationError。

具体工作流:
    单遍读取行并维护带 span 的标题栈，先形成结构候选；只有超限或高置信多主题候选
    进入模型，其余直接标记 atomic；模型边界经全覆盖验证后转为绝对坐标。

示例:
    blocks = split_structural_blocks("# API\n\n默认值为 20。", "markdown")
    assert blocks[0].section_path == ("API",)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from collections.abc import Callable, Sequence

from caspian.knowledge.evidence import (
    CandidateBlock,
    DocumentFormat,
    EvidenceValidationError,
    SectionReference,
    SourceSpan,
    UnitBoundary,
)


TokenCounter = Callable[[str], int]
HARD_TOKEN_MAX = 600

_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*(?:\r?\n|\r)?$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_LIST = re.compile(r"^ {0,3}(?:[-+*]|\d+[.)])[ \t]+")
_TABLE_DIVIDER = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*(?:\r?\n|\r)?$")
_TOPIC_SHIFT = re.compile(
    r"(?im)(?:^|[。！？!?\n])\s*(?:另一方面|另一个(?:独立)?(?:主题|问题)|与此无关|"
    r"第二个(?:主题|问题)|separately|another\s+(?:topic|issue))\b"
)
_LABELED_LIST_ITEM = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+([^：:\n]{1,40})[：:]")
_TOPIC_TABLE_HEADER = frozenset({"topic", "subject", "主题", "对象"})


@dataclass(frozen=True)
class AtomicityGateDecision:
    needs_semantic_split: bool
    reason: str


def _lines_with_spans(content: str) -> list[tuple[int, int, str]]:
    matches = re.finditer(r".*?(?:\r\n|\n|\r|$)", content)
    return [
        (match.start(), match.end(), match.group(0))
        for match in matches
        if match.end() > match.start()
    ]


def _line_body(line: str) -> str:
    return line.rstrip("\r\n")


def _is_blank(line: str) -> bool:
    return not _line_body(line).strip()


def _is_table_start(lines: list[tuple[int, int, str]], index: int) -> bool:
    return (
        index + 1 < len(lines)
        and "|" in _line_body(lines[index][2])
        and bool(_TABLE_DIVIDER.match(lines[index + 1][2]))
    )


def _block(
    content: str,
    kind: str,
    start: int,
    end: int,
    path: tuple[str, ...],
    refs: tuple[SectionReference, ...],
    index: int,
) -> CandidateBlock:
    return CandidateBlock(
        kind=kind,
        content=content[start:end],
        source_span=SourceSpan(start=start, end=end),
        section_path=path,
        section_refs=refs,
        structural_index=index,
    )


def _split_markdown(content: str) -> list[CandidateBlock]:
    lines = _lines_with_spans(content)
    result: list[CandidateBlock] = []
    headings: list[str] = []
    heading_refs: list[SectionReference] = []
    i = 0
    while i < len(lines):
        start, end, line = lines[i]
        if _is_blank(line):
            i += 1
            continue
        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            raw_title = heading.group(2)
            left_trim = len(raw_title) - len(raw_title.lstrip())
            title = raw_title.strip()
            title_start = start + heading.start(2) + left_trim
            headings = headings[: level - 1]
            heading_refs = heading_refs[: level - 1]
            headings.append(title)
            heading_refs.append(
                SectionReference(
                    title=title,
                    source_span=SourceSpan(start=title_start, end=title_start + len(title)),
                )
            )
            i += 1
            continue
        fence = _FENCE.match(line)
        if fence:
            marker = fence.group(1)
            j = i + 1
            closing = re.compile(rf"^ {{0,3}}{re.escape(marker[0])}{{{len(marker)},}}\s*(?:\r?\n|\r)?$")
            while j < len(lines):
                if closing.match(lines[j][2]):
                    j += 1
                    break
                j += 1
            end = lines[j - 1][1]
            result.append(_block(content, "code", start, end, tuple(headings), tuple(heading_refs), len(result)))
            i = j
            continue
        if _LIST.match(line):
            j = i + 1
            while (
                j < len(lines)
                and not _is_blank(lines[j][2])
                and (_LIST.match(lines[j][2]) or lines[j][2][:1].isspace())
            ):
                j += 1
            result.append(_block(content, "list", start, lines[j - 1][1], tuple(headings), tuple(heading_refs), len(result)))
            i = j
            continue
        if _is_table_start(lines, i):
            j = i + 2
            while j < len(lines) and not _is_blank(lines[j][2]) and "|" in _line_body(lines[j][2]):
                j += 1
            result.append(_block(content, "table", start, lines[j - 1][1], tuple(headings), tuple(heading_refs), len(result)))
            i = j
            continue
        j = i + 1
        while j < len(lines):
            candidate_line = lines[j][2]
            if (
                _is_blank(candidate_line)
                or _HEADING.match(candidate_line)
                or _FENCE.match(candidate_line)
                or _LIST.match(candidate_line)
                or _is_table_start(lines, j)
            ):
                break
            j += 1
        result.append(_block(content, "paragraph", start, lines[j - 1][1], tuple(headings), tuple(heading_refs), len(result)))
        i = j
    return result


def _split_text(content: str) -> list[CandidateBlock]:
    lines = _lines_with_spans(content)
    result: list[CandidateBlock] = []
    i = 0
    while i < len(lines):
        if _is_blank(lines[i][2]):
            i += 1
            continue
        start = lines[i][0]
        j = i + 1
        while j < len(lines) and not _is_blank(lines[j][2]):
            j += 1
        result.append(_block(content, "paragraph", start, lines[j - 1][1], (), (), len(result)))
        i = j
    return result


def split_structural_blocks(content: str, document_format: DocumentFormat) -> list[CandidateBlock]:
    blocks = _split_markdown(content) if document_format == "markdown" else _split_text(content)
    if not blocks:
        raise EvidenceValidationError("no_evidence", "文档没有可入库的正文证据")
    return blocks


def _labeled_list_topics(content: str) -> set[str]:
    return {
        match.group(1).strip().casefold()
        for line in content.splitlines()
        if (match := _LABELED_LIST_ITEM.match(line))
    }


def _table_has_explicit_topic_axis(content: str) -> bool:
    rows = [line for line in content.splitlines() if "|" in line and not _TABLE_DIVIDER.match(line)]
    if len(rows) <= 2:
        return False
    headers = [cell.strip().casefold() for cell in rows[0].strip().strip("|").split("|")]
    return bool(headers and headers[0] in _TOPIC_TABLE_HEADER)


def evaluate_atomicity_gate(block: CandidateBlock, token_counter: TokenCounter) -> AtomicityGateDecision:
    if token_counter(block.content) > HARD_TOKEN_MAX:
        return AtomicityGateDecision(True, "hard_max_exceeded")
    if block.kind == "list" and len(_labeled_list_topics(block.content)) > 1:
        return AtomicityGateDecision(True, "explicit_labeled_topics")
    if block.kind == "table" and _table_has_explicit_topic_axis(block.content):
        return AtomicityGateDecision(True, "explicit_topic_table")
    if _TOPIC_SHIFT.search(block.content):
        return AtomicityGateDecision(True, "explicit_topic_shift")
    return AtomicityGateDecision(False, "no_high_confidence_multi_topic_signal")


def needs_semantic_split(block: CandidateBlock, token_counter: TokenCounter) -> bool:
    return evaluate_atomicity_gate(block, token_counter).needs_semantic_split


def _as_boundary(value: UnitBoundary | SourceSpan) -> UnitBoundary:
    return value if isinstance(value, UnitBoundary) else UnitBoundary(source_span=value)


def validate_semantic_spans(
    candidate: CandidateBlock,
    spans: Sequence[UnitBoundary | SourceSpan],
    token_counter: TokenCounter,
) -> tuple[UnitBoundary, ...]:
    if not spans:
        raise EvidenceValidationError("empty_spans", "语义切分没有返回任何 span")
    validated: list[UnitBoundary] = []
    cursor = 0
    for raw_boundary in spans:
        try:
            boundary = _as_boundary(raw_boundary)
            span = boundary.source_span
        except Exception as exc:
            raise EvidenceValidationError("invalid_span", "span 必须包含合法整数 start/end") from exc
        if span.end > len(candidate.content):
            raise EvidenceValidationError("span_out_of_range", "span 超出候选正文范围")
        if span.start < cursor:
            raise EvidenceValidationError("span_overlap", "spans 必须升序且不重叠")
        if candidate.content[cursor:span.start].strip():
            raise EvidenceValidationError("span_gap", "spans 遗漏了非空白原文")
        unit_content = candidate.content[span.start:span.end]
        if not unit_content.strip():
            raise EvidenceValidationError("blank_span", "span 不能只包含空白")
        if token_counter(unit_content) > HARD_TOKEN_MAX:
            raise EvidenceValidationError("hard_max_exceeded", "Evidence Unit 超过 600 tokens")
        validated.append(boundary)
        cursor = span.end
    if candidate.content[cursor:].strip():
        raise EvidenceValidationError("span_gap", "spans 遗漏了候选尾部非空白原文")
    return tuple(validated)


def to_absolute_blocks(
    document: str,
    candidate: CandidateBlock,
    spans: Sequence[UnitBoundary | SourceSpan],
    token_counter: TokenCounter,
) -> list[CandidateBlock]:
    validated = validate_semantic_spans(candidate, spans, token_counter)
    result: list[CandidateBlock] = []
    for index, boundary in enumerate(validated):
        span = boundary.source_span
        absolute = SourceSpan(
            start=candidate.source_span.start + span.start,
            end=candidate.source_span.start + span.end,
        )
        unit_content = candidate.content[span.start:span.end]
        if document[absolute.start:absolute.end] != unit_content:
            raise EvidenceValidationError("source_mismatch", "绝对 span 无法还原原文")
        result.append(
            CandidateBlock(
                kind=candidate.kind,
                content=unit_content,
                source_span=absolute,
                section_path=candidate.section_path,
                section_refs=candidate.section_refs,
                structural_index=index,
                atomicity=boundary.atomicity,
                temporal_bindings=boundary.temporal_bindings,
            )
        )
    return result


def validate_ordered_non_overlapping(blocks: Sequence[CandidateBlock], token_counter: TokenCounter) -> None:
    previous_end = -1
    for block in blocks:
        if block.source_span.start < previous_end:
            raise EvidenceValidationError("overlap", "Evidence Unit 正文 span 不得重叠")
        if token_counter(block.content) > HARD_TOKEN_MAX:
            raise EvidenceValidationError("hard_max_exceeded", "Evidence Unit 超过 600 tokens")
        previous_end = block.source_span.end
