"""
本文件对外提供事实簇 span、atomicity、时态锚点模型适配器与 TokenCounter 契约测试。

输入:
    fake structured/plain chat model、CandidateBlock，以及中英文和代码文本。

输出:
    可运行断言，证明 schema 不含 chunk content、few-shot 同时覆盖合并/拆分、时态 anchor
    被机械转成绝对坐标，且长度策略复用同一 get_num_tokens callable。

具体工作流:
    捕获 with_structured_output schema 和消息，验证 SemanticSpan 字段；再复验纯 JSON fallback
    与中文、英文、代码三类文本的计数调用。

示例:
    python -m pytest backend/packages/harness/tests/test_knowledge_segmentation.py -q
"""

import unittest

from langchain_core.messages import AIMessage

from caspian.knowledge.evidence import CandidateBlock, SectionReference, SourceSpan, UnitBoundary
from caspian.knowledge.ingestion import _token_counter
from caspian.knowledge.segmentation import (
    SemanticSegmentationOutput,
    SemanticSpan,
    SemanticSpanSegmenter,
    SemanticTemporalBinding,
)


class _Structured:
    def __init__(self, output):
        self.output = output
        self.messages = None

    async def ainvoke(self, messages):
        self.messages = messages
        return self.output


class _Bound:
    def __init__(self, output=None, plain='{"spans":[]}'):
        self.output = output
        self.plain = plain
        self.schema = None
        self.structured = None

    def with_structured_output(self, schema, method="function_calling"):
        self.schema = schema
        if self.output is None:
            raise RuntimeError("no structured")
        self.structured = _Structured(self.output)
        return self.structured

    async def ainvoke(self, messages):
        return AIMessage(content=self.plain)


class _Model:
    def __init__(self, output=None, plain='{"spans":[]}'):
        self.bound = _Bound(output, plain)
        self.counted = []

    def bind(self, max_tokens):
        return self.bound

    def get_num_tokens(self, text):
        self.counted.append(text)
        return len(text.encode("utf-8"))


class SegmentationTests(unittest.IsolatedAsyncioTestCase):
    async def test_structured_schema_contains_only_span_fields(self):
        model = _Model(SemanticSegmentationOutput(spans=[SemanticSpan(start=0, end=5, reason="fact")]))
        candidate = CandidateBlock(kind="paragraph", content="Alpha", source_span=SourceSpan(start=0, end=5), structural_index=0)
        result = await SemanticSpanSegmenter(model).split(candidate)
        self.assertEqual(result, (UnitBoundary(source_span=SourceSpan(start=0, end=5)),))
        self.assertEqual(set(SemanticSpan.model_fields), {"start", "end", "atomicity", "temporal_bindings", "reason"})
        self.assertNotIn("content", SemanticSpan.model_fields)
        self.assertGreaterEqual(len(model.bound.structured.messages), 10)

    async def test_plain_json_fallback_returns_only_spans(self):
        model = _Model(None, '```json\n{"spans":[{"start":0,"end":1,"reason":"a"}]}\n```')
        candidate = CandidateBlock(kind="paragraph", content="A", source_span=SourceSpan(start=0, end=1), structural_index=0)
        self.assertEqual(await SemanticSpanSegmenter(model).split(candidate), (UnitBoundary(source_span=SourceSpan(start=0, end=1)),))

    async def test_temporal_binding_is_absolutized_and_atomicity_preserved(self):
        output = SemanticSegmentationOutput(spans=[SemanticSpan(
            start=0,
            end=12,
            atomicity="indivisible",
            temporal_bindings=[SemanticTemporalBinding(
                field="version",
                value="React 19",
                anchor_text="React 19",
                source_kind="heading",
                start=0,
                end=8,
                section_index=0,
            )],
        )])
        candidate = CandidateBlock(
            kind="paragraph",
            content="uses new API",
            source_span=SourceSpan(start=20, end=32),
            section_path=("React 19",),
            section_refs=(SectionReference(title="React 19", source_span=SourceSpan(start=2, end=10)),),
            structural_index=0,
        )
        boundary = (await SemanticSpanSegmenter(_Model(output)).split(candidate))[0]
        self.assertEqual(boundary.atomicity, "indivisible")
        self.assertEqual(boundary.temporal_bindings[0].source_span, SourceSpan(start=2, end=10))

    async def test_invalid_temporal_anchor_is_rejected(self):
        output = SemanticSegmentationOutput(spans=[SemanticSpan(
            start=0,
            end=1,
            temporal_bindings=[SemanticTemporalBinding(
                field="version", value="2", anchor_text="2", source_kind="content", start=0, end=1,
            )],
        )])
        with self.assertRaisesRegex(ValueError, "anchor"):
            await SemanticSpanSegmenter(_Model(output)).split(
                CandidateBlock(kind="paragraph", content="A", source_span=SourceSpan(start=0, end=1), structural_index=0)
            )

    def test_model_token_counter_is_reused_for_chinese_english_and_code(self):
        model = _Model()
        counter = _token_counter(model, None)
        samples = ["中文证据", "English evidence", "def f():\n    return 1"]
        counts = [counter(text) for text in samples]
        self.assertEqual(counts, [len(text.encode("utf-8")) for text in samples])
        self.assertEqual(model.counted, samples)


if __name__ == "__main__":
    unittest.main()
