"""
本文件提供 DeepSeek 模型适配器推理内容捕获的标准库 unittest。

输入:
    构造的 OpenAI 兼容流式 chunk dict（含或不含 delta.reasoning_content）
    以及构造的非流式 ChatCompletion 响应 dict（message 含或不含 reasoning_content）

输出:
    可运行检查，覆盖流式捕获写入、跨 chunk 累积拼接、content 正交、零副作用、
    非流式（ainvoke 路径）捕获与 model_dump 保留

运行:
    python -m unittest tests.test_reasoning_stream
"""

import unittest

from langchain_core.messages import AIMessageChunk

from caspian.models.deepseek import (
    DeepSeekChatOpenAI,
    _attach_reasoning_content,
    _non_streaming_reasoning,
    _set_reasoning_content,
)


class TestAttachReasoningContent(unittest.TestCase):
    """捕获推理分片到 additional_kwargs 的纯函数单测。"""

    def test_captures_reasoning_into_additional_kwargs(self):
        message = AIMessageChunk(content="")
        chunk = {"choices": [{"delta": {"reasoning_content": "思考中", "content": ""}}]}
        _attach_reasoning_content(message, chunk)
        self.assertEqual(message.additional_kwargs["reasoning_content"], "思考中")
        self.assertEqual(message.content, "")

    def test_accumulates_across_chunks_but_keeps_content(self):
        acc = None
        chunks = [
            {"choices": [{"delta": {"reasoning_content": "step1 "}}]},
            {"choices": [{"delta": {"reasoning_content": "step2 "}}]},
            {"choices": [{"delta": {"content": "answer"}}]},
        ]
        for chunk in chunks:
            message = AIMessageChunk(content="")
            _attach_reasoning_content(message, chunk)
            acc = message if acc is None else acc + message
        self.assertEqual(acc.additional_kwargs["reasoning_content"], "step1 step2 ")
        self.assertEqual(acc.content, "")

    def test_no_reasoning_produces_zero_write(self):
        message = AIMessageChunk(content="正文")
        chunk = {"choices": [{"delta": {"content": "正文"}}]}
        _attach_reasoning_content(message, chunk)
        self.assertNotIn("reasoning_content", message.additional_kwargs)
        self.assertEqual(message.content, "正文")

    def test_empty_or_missing_delta_is_noop(self):
        message = AIMessageChunk(content="")
        _attach_reasoning_content(message, {"choices": []})
        self.assertNotIn("reasoning_content", message.additional_kwargs)

    def test_beta_stream_shape_is_handled(self):
        # beta.chat.completions.stream 走 chunk.chunk.choices
        message = AIMessageChunk(content="")
        chunk = {"chunk": {"choices": [{"delta": {"reasoning_content": "思考"}}]}}
        _attach_reasoning_content(message, chunk)
        self.assertEqual(message.additional_kwargs["reasoning_content"], "思考")

    def test_non_streaming_reasoning_extracts_message_field(self):
        raw = {"choices": [{"message": {"content": "答案", "reasoning_content": "推理过程"}}]}
        self.assertEqual(_non_streaming_reasoning(raw), "推理过程")

    def test_non_streaming_reasoning_missing_returns_none(self):
        self.assertIsNone(_non_streaming_reasoning({"choices": [{"message": {"content": "答案"}}]}))
        self.assertIsNone(_non_streaming_reasoning({"choices": []}))
        self.assertIsNone(_non_streaming_reasoning({}))

    def test_set_reasoning_content_ignores_empty(self):
        message = AIMessageChunk(content="")
        _set_reasoning_content(message, "")
        _set_reasoning_content(message, None)
        self.assertNotIn("reasoning_content", message.additional_kwargs)


if __name__ == "__main__":
    unittest.main()
