"""
本文件验证 route capability E2E runner 的 opt-in、遥测归一化与无凭据 JSON schema。

输入为确定性 fake provider 响应；输出为 A/B 报告。工作流覆盖缺失 cache 字段的 unavailable、
语义/cache 分离判定、endpoint 清洗和报告中不出现 API key。

示例: pytest tests/test_system_prompt_e2e.py
"""

import asyncio
import json

from langchain_core.messages import AIMessage

from caspian.models.system_prompt_e2e import (
    NEW_SENTINEL,
    OPT_IN_ENV,
    UNAVAILABLE,
    e2e_enabled,
    normalize_usage,
    report_to_dict,
    run_capability_e2e,
)


class FakeProvider:
    def __init__(self, cache_values):
        self.cache_values = iter(cache_values)

    async def ainvoke(self, messages):
        if len(messages) == 2:
            return AIMessage(content="warm")
        cache_read = next(self.cache_values)
        return AIMessage(
            content=NEW_SENTINEL,
            usage_metadata={
                "input_tokens": 200,
                "output_tokens": 1,
                "total_tokens": 201,
                "input_token_details": {"cache_read": cache_read},
            },
            response_metadata={"token_usage": {"prompt_cache_miss_tokens": 5}},
        )


def test_e2e_requires_explicit_opt_in():
    assert e2e_enabled({}) is False
    assert e2e_enabled({OPT_IN_ENV: "1"}) is True


def test_missing_cache_fields_are_unavailable_not_zero():
    usage = normalize_usage(AIMessage(content="x"))
    assert usage.input_tokens == UNAVAILABLE
    assert usage.cache_read_tokens == UNAVAILABLE
    assert usage.cache_miss_tokens == UNAVAILABLE


def test_deterministic_report_separates_semantic_and_cache_conclusions():
    report = asyncio.run(
        run_capability_e2e(
            FakeProvider([80, 10, 90, 20]),
            endpoint="https://user:secret@example.com/v1?api_key=hidden",
            model_id="route-model",
            trials=2,
        )
    )
    payload = report_to_dict(report)
    serialized = json.dumps(payload)
    assert payload["schema_version"] == 1
    assert payload["endpoint"] == "https://example.com/v1"
    assert payload["model"] == "route-model"
    assert payload["semantic_supersede"] == "pass"
    assert payload["cache_benefit"] == "pass"
    assert len(payload["stable_prefix_hashes"]) == 2
    assert len(payload["measurements"]) == 4
    assert "secret" not in serialized
    assert "api_key" not in serialized


def test_missing_cache_in_one_arm_makes_cache_conclusion_indeterminate():
    class MissingProvider(FakeProvider):
        async def ainvoke(self, messages):
            if len(messages) == 2:
                return AIMessage(content="warm")
            return AIMessage(content=NEW_SENTINEL)

    report = asyncio.run(
        run_capability_e2e(
            MissingProvider([]),
            endpoint="https://example.com/v1",
            model_id="route-model",
            trials=1,
        )
    )
    assert report.semantic_supersede == "pass"
    assert report.cache_benefit == "indeterminate"
