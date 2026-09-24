"""
本文件对外提供确定性结构切分、原子性闸门和语义 span 机械验证函数。

对外提供:
    split_structural_blocks — 按 Markdown 或纯文本天然结构生成绝对坐标候选块
    needs_semantic_split — 用结构、句界、并列信号与 hard max 判断候选是否进入模型切分
    validate_semantic_spans — 验证局部 spans 全覆盖、无重叠、非空且不超长
    to_absolute_blocks — 把合法局部 spans 转为文档绝对坐标候选块
    validate_ordered_non_overlapping — 验证最终 Evidence Unit 零正文 overlap

输入:
    原文、文档格式、CandidateBlock、模型返回的 SourceSpan 和 TokenCounter。

输出:
    保持原文精确子串的 CandidateBlock 列表；非法边界抛 EvidenceValidationError。

具体工作流:
    单遍读取行并维护标题栈，先形成 paragraph/list/table/code 候选；短而明确的单结构
    候选直接通过，其余只接受可机械验证的模型 spans，最后转换绝对坐标并复验原文。

示例:
    blocks = split_structural_blocks("# API\n\n默认值为 20。", "markdown")
    assert blocks[0].section_path == ("API",)
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from caspian.knowledge.evidence import CandidateBlock, DocumentFormat, EvidenceValidationError, SourceSpan


TokenCounter = Callable[[str], int]
IDEAL_TOKEN_MIN = 150
IDEAL_TOKEN_MAX = 400
HARD_TOKEN_MAX = 600

_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*(?:\r?\n|\r)?$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_LIST = re.compile(r"^ {0,3}(?:[-+*]|\d+[.)])[ \t]+")
_TABLE_DIVIDER = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*(?:\r?\n|\r)?$")
_SENTENCE_END = re.compile(r"[。！？!?.](?:[\"'”’）)])?")
_CLAUSE_SEPARATOR = re.compile(r"[；;]")
_PARALLEL_MARKER = re.compile(r"(?:^|[。；;]\s*)(?:另外|此外|同时|另一方面|其二|第二)[，,:：\s]")


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


def _block(content: str, kind: str, start: int, end: int, path: tuple[str, ...], index: int) -> CandidateBlock:
    return CandidateBlock(
        kind=kind,
        content=content[start:end],
        source_span=SourceSpan(start=start, end=end),
        section_path=path,
        structural_index=index,
    )


def _split_markdown(content: str) -> list[CandidateBlock]:
    lines = _lines_with_spans(content)
    result: list[CandidateBlock] = []
    headings: list[str] = []
    i = 0
    while i < len(lines):
        start, end, line = lines[i]
        if _is_blank(line):
            i += 1
            continue
        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip()
            headings = headings[: level - 1]
            headings.append(title)
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
            result.append(_block(content, "code", start, end, tuple(headings), len(result)))
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
            result.append(_block(content, "list", start, lines[j - 1][1], tuple(headings), len(result)))
            i = j
            continue
        if _is_table_start(lines, i):
            j = i + 2
            while j < len(lines) and not _is_blank(lines[j][2]) and "|" in _line_body(lines[j][2]):
                j += 1
            result.append(_block(content, "table", start, lines[j - 1][1], tuple(headings), len(result)))
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
        result.append(_block(content, "paragraph", start, lines[j - 1][1], tuple(headings), len(result)))
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
        result.append(_block(content, "paragraph", start, lines[j - 1][1], (), len(result)))
        i = j
    return result


def split_structural_blocks(content: str, document_format: DocumentFormat) -> list[CandidateBlock]:
    blocks = _split_markdown(content) if document_format == "markdown" else _split_text(content)
    if not blocks:
        raise EvidenceValidationError("no_evidence", "文档没有可入库的正文证据")
    return blocks


def needs_semantic_split(block: CandidateBlock, token_counter: TokenCounter) -> bool:
    if token_counter(block.content) > HARD_TOKEN_MAX:
        return True
    if block.kind == "list":
        return sum(bool(_LIST.match(line)) for line in block.content.splitlines()) > 1
    if block.kind == "table":
        rows = [line for line in block.content.splitlines() if "|" in line and not _TABLE_DIVIDER.match(line)]
        return len(rows) > 2
    if block.kind == "paragraph":
        return (
            len(_SENTENCE_END.findall(block.content)) > 1
            or bool(_CLAUSE_SEPARATOR.search(block.content))
            or bool(_PARALLEL_MARKER.search(block.content))
        )
    if block.kind == "code":
        declarations = re.findall(r"(?m)^\s*(?:async\s+def|def|class|function|export\s+function)\s+", block.content)
        return len(declarations) > 1
    return False


def validate_semantic_spans(
    candidate: CandidateBlock,
    spans: Sequence[SourceSpan],
    token_counter: TokenCounter,
) -> tuple[SourceSpan, ...]:
    if not spans:
        raise EvidenceValidationError("empty_spans", "语义切分没有返回任何 span")
    validated: list[SourceSpan] = []
    cursor = 0
    for raw_span in spans:
        try:
            span = raw_span if isinstance(raw_span, SourceSpan) else SourceSpan.model_validate(raw_span)
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
        validated.append(span)
        cursor = span.end
    if candidate.content[cursor:].strip():
        raise EvidenceValidationError("span_gap", "spans 遗漏了候选尾部非空白原文")
    return tuple(validated)


def to_absolute_blocks(
    document: str,
    candidate: CandidateBlock,
    spans: Sequence[SourceSpan],
    token_counter: TokenCounter,
) -> list[CandidateBlock]:
    validated = validate_semantic_spans(candidate, spans, token_counter)
    result: list[CandidateBlock] = []
    for index, span in enumerate(validated):
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
                structural_index=index,
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
