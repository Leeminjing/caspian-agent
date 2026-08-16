"""验证 DeepSeek 的缓存 token 字段映射到 LangChain 标准 usage。"""

from langchain_core.messages import AIMessageChunk

from caspian.models.deepseek import DeepSeekChatOpenAI


def test_deepseek_stream_usage_maps_cache_hit_tokens():
    model = DeepSeekChatOpenAI(
        model="deepseek-v4-flash",
        api_key="test",
        base_url="https://api.deepseek.com",
    )
    generation = model._convert_chunk_to_generation_chunk(
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "total_tokens": 110,
                "prompt_cache_hit_tokens": 35,
                "prompt_cache_miss_tokens": 65,
            },
        },
        AIMessageChunk,
        None,
    )

    assert model.stream_usage is True
    assert generation.message.usage_metadata["input_tokens"] == 100
    assert generation.message.usage_metadata["input_token_details"]["cache_read"] == 35


def test_deepseek_usage_without_cache_field_leaves_metadata_untouched():
    model = DeepSeekChatOpenAI(
        model="deepseek-v4-flash",
        api_key="test",
        base_url="https://api.deepseek.com",
    )
    generation = model._convert_chunk_to_generation_chunk(
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 50,
                "completion_tokens": 5,
                "total_tokens": 55,
            },
        },
        AIMessageChunk,
        None,
    )

    assert generation.message.usage_metadata["input_tokens"] == 50
    assert "cache_read" not in (generation.message.usage_metadata.get("input_token_details") or {})


def test_worker_accumulates_usage_from_values_chunks_once_per_message():
    """values 模式（Caspian 当前实际流的模式）下按消息 id 去重累计 usage。"""
    from caspian.runtime.runs.manager import RunRecord, RunManager
    from caspian.runtime.runs.worker import _accumulate_usage

    record = RunManager().create("th-usage")
    record._usage_seen_ids = set()

    def ai_message(content, usage):
        return AIMessageChunk(
            content=content,
            id=f"msg-{content}",
            usage_metadata={
                "input_tokens": usage[0],
                "output_tokens": usage[1],
                "total_tokens": usage[0] + usage[1],
                "input_token_details": {"cache_read": usage[2]},
            },
        )

    first = ai_message("第一轮", (100, 10, 35))
    second = ai_message("第二轮", (50, 8, 10))

    # 状态快照重复出现：第二轮快照包含第一轮消息，不得重复累计
    _accumulate_usage(record, "values", {"messages": [first]})
    _accumulate_usage(record, "values", {"messages": [first, second]})
    _accumulate_usage(record, "values", {"messages": [first, second]})

    assert record.prompt_input_tokens == 150
    assert record.prompt_cache_hit_tokens == 45

    # messages 模式兼容原有路径
    record2 = RunManager().create("th-usage-2")
    _accumulate_usage(record2, "messages", (ai_message("chunk", (20, 2, 5)), {}))
    assert record2.prompt_input_tokens == 20
    assert record2.prompt_cache_hit_tokens == 5

    # 其他模式不累计
    record3 = RunManager().create("th-usage-3")
    _accumulate_usage(record3, "custom", {"type": "task_started"})
    assert record3.prompt_input_tokens == 0
