"""DeepSeek 的 OpenAI 兼容缓存 usage 到 LangChain 标准字段的薄适配，并捕获推理内容。

对外提供:
    _attach_cache_read — 把 DeepSeek 的 prompt_cache_hit_tokens 写入消息 usage_metadata
    _attach_reasoning_content — 把流式 delta.reasoning_content 分片写入消息 additional_kwargs
    _set_reasoning_content — 把 reasoning_content 值写入消息 additional_kwargs
    DeepSeekChatOpenAI — 保留 ChatOpenAI 行为，仅补齐缓存命中 token 与推理内容捕获

示例:
    _attach_reasoning_content(generation.message, chunk)
    _set_reasoning_content(generation.message, "推理分片")
"""

from typing import Any

from langchain_openai import ChatOpenAI


def _attach_cache_read(message: Any, usage: dict[str, Any] | None) -> None:
    if not usage or usage.get("prompt_cache_hit_tokens") is None:
        return
    metadata = getattr(message, "usage_metadata", None)
    if metadata is None:
        return
    details = dict(metadata.get("input_token_details") or {})
    details["cache_read"] = int(usage["prompt_cache_hit_tokens"])
    metadata["input_token_details"] = details


def _choices(chunk: dict[str, Any]) -> list[dict[str, Any]]:
    """从 chunk 提取 choices 列表（兼容普通流式与 beta.chat.completions.stream）。

    输入:
        chunk: dict — OpenAI 兼容流式 chunk，含 choices 或 chunk.choices

    输出:
        list[dict[str, Any]] — choices 列表；无法解析时返回空列表
    """
    result = chunk.get("choices", [])
    if not result and isinstance(chunk.get("chunk"), dict):
        result = chunk["chunk"].get("choices", [])
    return result or []


def _attach_reasoning_content(message: Any, chunk: dict[str, Any]) -> None:
    """把流式 delta.reasoning_content 分片写入消息 additional_kwargs。

    输入:
        message: Any — 流式产物（AIMessageChunk / AIMessage）
        chunk: dict — OpenAI 兼容流式 chunk

    输出:
        None — 原地写入 message.additional_kwargs["reasoning_content"]；为空时不写入

    工作流:
        (1) 取 chunk 的首个 choices[0].delta
        (2) delta 含非空 string reasoning_content 才写入 additional_kwargs
        (3) 聚合时经 AIMessageChunk.__add__ → merge_dicts 对同名 string 拼接累积
    """
    choices = _choices(chunk)
    if not choices:
        return
    delta = choices[0].get("delta")
    if not isinstance(delta, dict):
        return
    _set_reasoning_content(message, delta.get("reasoning_content"))


def _set_reasoning_content(message: Any, reasoning: Any) -> None:
    """把 reasoning_content 分片写入消息 additional_kwargs（受保护 helper）。

    输入:
        message: Any — AIMessage / AIMessageChunk
        reasoning: Any — reasoning_content 值，非空 string 才写入

    输出:
        None — 原地写入 message.additional_kwargs["reasoning_content"]；为空时不写入
    """
    if not isinstance(reasoning, str) or not reasoning:
        return
    additional_kwargs = getattr(message, "additional_kwargs", None)
    if not isinstance(additional_kwargs, dict):
        return
    additional_kwargs["reasoning_content"] = reasoning


class DeepSeekChatOpenAI(ChatOpenAI):
    """保留 ChatOpenAI 行为，仅补齐 DeepSeek 的缓存命中 token 与推理内容捕获。"""

    stream_usage: bool | None = True

    def _convert_chunk_to_generation_chunk(
        self, chunk: dict, default_chunk_class: type, base_generation_info: dict | None
    ):
        generation = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        if generation is not None:
            _attach_cache_read(generation.message, chunk.get("usage"))
            _attach_reasoning_content(generation.message, chunk)
        return generation

    def _create_chat_result(self, response, generation_info: dict | None = None):
        raw = response if isinstance(response, dict) else response.model_dump()
        result = super()._create_chat_result(response, generation_info)
        for generation in result.generations:
            _attach_cache_read(generation.message, raw.get("usage"))
        for message in (gen.message for gen in result.generations):
            reasoning = _non_streaming_reasoning(raw)
            if reasoning:
                _set_reasoning_content(message, reasoning)
        return result


def _non_streaming_reasoning(raw: dict[str, Any]) -> str | None:
    """从非流式响应读取首个 choice 的 message.reasoning_content。

    输入:
        raw: dict — 非流式 ChatCompletion 响应 dict（含 choices[].message）

    输出:
        str | None — reasoning_content 字符串；缺省或为空返回 None
    """
    choices = raw.get("choices") or []
    if not choices:
        return None
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return None
    reasoning = message.get("reasoning_content")
    return reasoning if isinstance(reasoning, str) and reasoning else None
