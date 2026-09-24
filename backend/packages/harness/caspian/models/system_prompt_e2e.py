"""
本文件对外提供 run_capability_e2e、normalize_usage、report_to_dict 与命令行入口。

输入:
    一个可 ainvoke 的 Caspian chat model、route endpoint/model identity、trial 数与显式 opt-in 环境变量

输出:
    不含凭据的 CapabilityE2EReport；cache 字段缺失时输出 unavailable，语义与 cache 分别判定

具体工作流:
    每个 trial 用相同长前缀和完整 S1 预热；Append arm 在原前缀后追加完整 S2，Replace arm
    把 leading S1 改写为完整 S2；记录 sentinel 遵循、input/cache tokens、单调时钟延迟和输入哈希。

示例:
    CASPIAN_SYSTEM_PROMPT_E2E=1 python -m caspian.models.system_prompt_e2e --output report.json
"""

import argparse
import asyncio
import json
import os
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from time import monotonic
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

UNAVAILABLE = "unavailable"
OPT_IN_ENV = "CASPIAN_SYSTEM_PROMPT_E2E"
OLD_SENTINEL = "CASPIAN_POLICY_OLD"
NEW_SENTINEL = "CASPIAN_POLICY_NEW"


@dataclass(frozen=True, slots=True)
class UsageTelemetry:
    input_tokens: int | Literal["unavailable"]
    cache_read_tokens: int | Literal["unavailable"]
    cache_miss_tokens: int | Literal["unavailable"]


@dataclass(frozen=True, slots=True)
class ArmMeasurement:
    trial: int
    arm: Literal["append", "replace"]
    input_hash: str
    adheres_to_s2: bool
    input_tokens: int | Literal["unavailable"]
    cache_read_tokens: int | Literal["unavailable"]
    cache_miss_tokens: int | Literal["unavailable"]
    latency_ms: float


@dataclass(frozen=True, slots=True)
class CapabilityE2EReport:
    schema_version: int
    endpoint: str
    model: str
    stable_prefix_hashes: tuple[str, ...]
    measurements: tuple[ArmMeasurement, ...]
    semantic_supersede: Literal["pass", "fail"]
    cache_benefit: Literal["pass", "fail", "indeterminate"]


def e2e_enabled(environ: dict[str, str] | None = None) -> bool:
    value = (environ or os.environ).get(OPT_IN_ENV, "")
    return value.strip().lower() in {"1", "true", "yes"}


def endpoint_identity(base_url: str) -> str:
    parts = urlsplit(base_url)
    hostname = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    return urlunsplit((parts.scheme, f"{hostname}{port}", parts.path, "", ""))


def normalize_usage(message: AIMessage) -> UsageTelemetry:
    usage = message.usage_metadata or {}
    details = usage.get("input_token_details") or {}
    raw = message.response_metadata or {}
    token_usage = raw.get("token_usage") or raw.get("usage") or {}
    return UsageTelemetry(
        input_tokens=_first_int(usage.get("input_tokens"), token_usage.get("prompt_tokens")),
        cache_read_tokens=_first_int(
            details.get("cache_read"),
            token_usage.get("prompt_cache_hit_tokens"),
        ),
        cache_miss_tokens=_first_int(
            details.get("cache_miss"),
            token_usage.get("prompt_cache_miss_tokens"),
        ),
    )


def report_to_dict(report: CapabilityE2EReport) -> dict[str, Any]:
    return asdict(report)


async def run_capability_e2e(
    model: Any,
    *,
    endpoint: str,
    model_id: str,
    trials: int = 3,
) -> CapabilityE2EReport:
    if trials < 1:
        raise ValueError("trials must be at least 1")
    measurements: list[ArmMeasurement] = []
    prefix_hashes: list[str] = []
    for trial in range(trials):
        prefix = _stable_prefix(trial)
        prefix_hashes.append(_messages_hash(prefix))
        for arm in ("append", "replace"):
            await model.ainvoke(prefix)
            request = _second_request(prefix, arm)
            started = monotonic()
            response = await model.ainvoke(request)
            elapsed = (monotonic() - started) * 1000
            text = _message_text(response)
            usage = normalize_usage(response)
            measurements.append(
                ArmMeasurement(
                    trial=trial,
                    arm=arm,
                    input_hash=_messages_hash(request),
                    adheres_to_s2=(NEW_SENTINEL in text and OLD_SENTINEL not in text),
                    input_tokens=usage.input_tokens,
                    cache_read_tokens=usage.cache_read_tokens,
                    cache_miss_tokens=usage.cache_miss_tokens,
                    latency_ms=round(elapsed, 3),
                )
            )
    append_rows = [row for row in measurements if row.arm == "append"]
    replace_rows = [row for row in measurements if row.arm == "replace"]
    semantic = "pass" if append_rows and all(row.adheres_to_s2 for row in append_rows) else "fail"
    cache = _cache_conclusion(append_rows, replace_rows)
    return CapabilityE2EReport(
        schema_version=1,
        endpoint=endpoint_identity(endpoint),
        model=model_id,
        stable_prefix_hashes=tuple(prefix_hashes),
        measurements=tuple(measurements),
        semantic_supersede=semantic,
        cache_benefit=cache,
    )


def _first_int(*values: Any) -> int | Literal["unavailable"]:
    for value in values:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return UNAVAILABLE


def _complete_snapshot(sentinel: str) -> str:
    return (
        "<identity>You are the Caspian route capability verifier.</identity>\n"
        "<workflow>Follow the newest complete system snapshot.</workflow>\n"
        "<decision_table version=\"e2e\">No tools; reply exactly as required.</decision_table>\n"
        f"<response_policy>Reply with exactly {sentinel}.</response_policy>"
    )


def _stable_prefix(trial: int) -> list[BaseMessage]:
    filler = (f"stable-prefix-trial-{trial}-" + "0123456789abcdef" * 64 + "\n") * 8
    return [
        SystemMessage(content=_complete_snapshot(OLD_SENTINEL)),
        HumanMessage(content=f"Retain this deterministic cache prefix:\n{filler}"),
    ]


def _second_request(
    prefix: list[BaseMessage], arm: Literal["append", "replace"]
) -> list[BaseMessage]:
    query = HumanMessage(content="Apply the current response policy now.")
    s2 = SystemMessage(content=_complete_snapshot(NEW_SENTINEL))
    if arm == "append":
        return [*prefix, s2, query]
    return [s2, *prefix[1:], query]


def _messages_hash(messages: list[BaseMessage]) -> str:
    payload = [
        {"type": message.type, "content": message.content}
        for message in messages
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def _message_text(message: AIMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    return "".join(
        block if isinstance(block, str) else str(block.get("text", ""))
        for block in message.content
    )


def _cache_conclusion(
    append_rows: list[ArmMeasurement],
    replace_rows: list[ArmMeasurement],
) -> Literal["pass", "fail", "indeterminate"]:
    append_values = [row.cache_read_tokens for row in append_rows]
    replace_values = [row.cache_read_tokens for row in replace_rows]
    if any(value == UNAVAILABLE for value in [*append_values, *replace_values]):
        return "indeterminate"
    append_mean = sum(int(value) for value in append_values) / len(append_values)
    replace_mean = sum(int(value) for value in replace_values) / len(replace_values)
    return "pass" if append_mean > replace_mean else "fail"


async def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-name", default=None)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not e2e_enabled():
        print(f"skipped: set {OPT_IN_ENV}=1 to authorize real-provider calls")
        return 0
    from caspian.config import get_app_config
    from caspian.models import create_chat_model, resolve_model_config

    try:
        app_config = get_app_config("config.yaml")
    except KeyError:
        print("skipped: configured route credential is unavailable")
        return 0
    route = resolve_model_config(args.model_name, app_config=app_config)
    if not route.api_key:
        print("skipped: configured route has no API credential")
        return 0
    model = create_chat_model(args.model_name, app_config=app_config, temperature=0)
    report = await run_capability_e2e(
        model,
        endpoint=route.base_url,
        model_id=route.model,
        trials=max(1, args.trials),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report_to_dict(report), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"report written: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
