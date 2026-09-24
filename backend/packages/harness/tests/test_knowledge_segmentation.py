"""
本文件对外提供 span-only 模型适配器与 TokenCounter 契约测试。

输入:
    fake structured/plain chat model、CandidateBlock，以及中英文和代码文本。

输出:
    可运行断言，证明模型输出 schema 不含 chunk content、两条解析路径只返回 spans，且
    入库长度策略复用注入模型的同一确定性 get_num_tokens callable。

具体工作流:
    捕获 with_structured_output schema 和消息，验证 SemanticSpan 字段；再复验纯 JSON fallback
    与中文、英文、代码三类文本的计数调用。

示例:
    python -m pytest backend/packages/harness/tests/test_knowledge_segmentation.py -q
"""

import unittest

from langchain_core.messages import AIMessage

from caspian.knowledge.evidence import CandidateBlock, SourceSpan
from caspian.knowledge.ingestion import _token_counter
from caspian.knowledge.segmentation import SemanticSegmentationOutput, SemanticSpan, SemanticSpanSegmenter


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
        self.assertEqual(result, (SourceSpan(start=0, end=5),))
        self.assertEqual(set(SemanticSpan.model_fields), {"start", "end", "reason"})
        self.assertNotIn("content", SemanticSpan.model_fields)

    async def test_plain_json_fallback_returns_only_spans(self):
        model = _Model(None, '```json\n{"spans":[{"start":0,"end":1,"reason":"a"}]}\n```')
        candidate = CandidateBlock(kind="paragraph", content="A", source_span=SourceSpan(start=0, end=1), structural_index=0)
        self.assertEqual(await SemanticSpanSegmenter(model).split(candidate), (SourceSpan(start=0, end=1),))

    def test_model_token_counter_is_reused_for_chinese_english_and_code(self):
        model = _Model()
        counter = _token_counter(model, None)
        samples = ["中文证据", "English evidence", "def f():\n    return 1"]
        counts = [counter(text) for text in samples]
        self.assertEqual(counts, [len(text.encode("utf-8")) for text in samples])
        self.assertEqual(model.counted, samples)


if __name__ == "__main__":
    unittest.main()
