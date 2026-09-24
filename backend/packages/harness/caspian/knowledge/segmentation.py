"""
本文件对外提供 span-only 语义切分模型协议与适配器。

对外提供:
    SemanticSpan / SemanticSegmentationOutput — 模型结构化输出，仅含 start、end、reason
    SemanticSpanSegmenter — 调用聊天模型并返回局部 SourceSpan，不生成或改写正文

输入:
    CandidateBlock 原文、聊天模型和超时秒数。

输出:
    tuple[SourceSpan, ...]；模型协议或 JSON 无效时抛 EvidenceValidationError。

具体工作流:
    固定 prompt 要求按独立事实簇给出半开字符边界；优先 function-calling 结构化输出，
    失败后只解析同一 span JSON 协议；原文全覆盖和 hard max 由 chunking 模块验证。

示例:
    spans = await SemanticSpanSegmenter(model).split(candidate)
"""

from __future__ import annotations

import asyncio
import json

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from caspian.knowledge.evidence import CandidateBlock, EvidenceValidationError, SourceSpan
from caspian.knowledge.json_parsing import parse_fenced_or_raw


class SemanticSpan(BaseModel):
    start: int
    end: int
    reason: str = ""


class SemanticSegmentationOutput(BaseModel):
    spans: list[SemanticSpan] = Field(default_factory=list)


_SYSTEM_PROMPT = """你负责把候选原文按可独立评级和冲突治理的完整事实簇划分边界。
只返回原文半开字符坐标 start/end 和简短 reason，禁止返回、重写或摘要 chunk 正文。
spans 必须升序、不重叠，并覆盖全部非空白原文。不要仅为接近理想长度拆开完整事实簇；
每个 span 必须不超过 600 tokens。"""


class SemanticSpanSegmenter:
    def __init__(self, model: BaseChatModel, *, timeout_seconds: float = 120.0):
        self._model = model
        self._timeout_seconds = timeout_seconds

    async def split(self, candidate: CandidateBlock) -> tuple[SourceSpan, ...]:
        payload = json.dumps(
            {
                "kind": candidate.kind,
                "section_path": list(candidate.section_path),
                "length_chars": len(candidate.content),
                "candidate": candidate.content,
            },
            ensure_ascii=False,
        )
        bound = self._model.bind(max_tokens=2048)
        messages = [SystemMessage(content=_SYSTEM_PROMPT), HumanMessage(content=payload)]
        try:
            structured = bound.with_structured_output(
                SemanticSegmentationOutput,
                method="function_calling",
            )
            async with asyncio.timeout(self._timeout_seconds):
                parsed = await structured.ainvoke(messages)
            if not isinstance(parsed, SemanticSegmentationOutput):
                raise TypeError("结构化输出类型异常")
            return tuple(SourceSpan(start=item.start, end=item.end) for item in parsed.spans)
        except Exception as structured_error:
            try:
                async with asyncio.timeout(self._timeout_seconds):
                    raw = await bound.ainvoke(
                        [*messages, HumanMessage(content='只返回 {"spans":[{"start":0,"end":1,"reason":""}]} JSON。')]
                    )
                data = parse_fenced_or_raw(str(raw.content))
                parsed = SemanticSegmentationOutput.model_validate(data)
                return tuple(SourceSpan(start=item.start, end=item.end) for item in parsed.spans)
            except Exception as fallback_error:
                raise EvidenceValidationError(
                    "segmentation_failed",
                    "语义切分模型未返回合法 span 协议",
                    details={
                        "structured_error": type(structured_error).__name__,
                        "fallback_error": type(fallback_error).__name__,
                    },
                ) from fallback_error
