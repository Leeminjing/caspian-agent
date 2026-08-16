"""
本文件对外提供 LLM 冲突判定函数 judge_conflicts。

对外提供:
    judge_conflicts — 单次批量调用模型，判定候选证据两两之间与查询命题相关的冲突关系

输入:
    candidates: list[EvidenceEntry] — 候选证据（仅用 id 与 content；等级不参与判定，
        等级裁决由 governance 确定性完成）
    model: BaseChatModel — 已构造的聊天模型
    query: str — 用户查询文本，冲突判定只针对与查询命题相关的关系（规格第 10 节：
        压制针对具体查询与具体命题发生）
    timeout_seconds: float — 单次模型请求超时，默认 120

输出:
    list[ConflictRelation] — 两两冲突关系（explicit/potential × full/partial）

具体工作流:
    (1) 组装 candidates JSON 与判定 system prompt（含用户查询）
    (2) 结构化输出（json_mode）单次批量调用（先例 delegation.py 的
        with_structured_output 模式）
    (3) 结构化调用异常/解析失败 → 纯文本兜底 + json.loads 解析（先例
        stage_rules._extract_structured 的 fenced 提取模式）
    (4) 校验关系合法性（未知 id、非法枚举丢弃并 WARNING）
    (5) 全部失败 → 抛异常，由调用方返回 502（绝不静默跳过治理）

示例:
    conflicts = await judge_conflicts(candidates, model, query="功能 A 是否已废弃？")
    result = govern(candidates, conflicts)
"""

import asyncio
import json
import logging
import re

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from caspian.knowledge.schemas import (
    ConflictRelation,
    EvidenceEntry,
    JudgeConflictOutput,
)

logger = logging.getLogger(__name__)

_JUDGE_SYSTEM_PROMPT = """你是知识证据的冲突判定器。给定用户查询与若干候选证据（每条含 id 与 content），判断哪些证据对之间存在与查询命题相关的事实性冲突。

判定规则：
1. 只判定与用户查询命题相关的冲突：与查询无关的矛盾不列入输出。
2. 只有双方针对同一事实给出明确相反的结论，才标 relation="explicit"。
3. 疑似相反但无法可靠确认的，只能标 relation="potential"，不得标 explicit。
4. 无冲突的证据对不列入输出。
5. 若证据包含多个可分离命题，仅部分命题与对方冲突，必须标 scope="partial"，
   并给出各自冲突命题的原文 claim_a（对应 a 的命题）与 claim_b（对应 b 的命题）；
   scope="full" 仅用于整条证据整体与对方对立的情况。
6. 输出必须是一个 JSON 对象，格式：
{"conflicts": [{"a": "<id>", "b": "<id>", "relation": "explicit|potential", "scope": "full|partial", "claim_a": "...", "claim_b": "..."}]}"""

_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)


def _parse_fenced_or_raw(text: str) -> dict | None:
    """从模型文本中提取 JSON 对象（fenced 优先，其次整段解析）。"""
    fenced = _FENCED_JSON_RE.search(text)
    if fenced:
        return json.loads(fenced.group(1))
    return json.loads(text)


def _validated_conflicts(
    raw_conflicts: list[dict], known_ids: set[str]
) -> list[ConflictRelation]:
    """过滤未知 id、非法枚举、自环与重复对。"""
    result: list[ConflictRelation] = []
    seen_pairs: set[frozenset] = set()
    for item in raw_conflicts or []:
        if not isinstance(item, dict):
            continue
        a, b = item.get("a"), item.get("b")
        if a not in known_ids or b not in known_ids or a == b:
            logger.warning("judge 输出非法证据对，已丢弃: %s", item)
            continue
        relation = item.get("relation")
        scope = item.get("scope", "full")
        if relation not in ("explicit", "potential"):
            logger.warning("judge 输出非法 relation，已丢弃: %s", item)
            continue
        if scope not in ("full", "partial"):
            scope = "full"
        pair = frozenset((a, b))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        result.append(
            ConflictRelation(
                a=str(a),
                b=str(b),
                relation=relation,
                scope=scope,
                claim_a=str(item.get("claim_a", "") or ""),
                claim_b=str(item.get("claim_b", "") or ""),
            )
        )
    return result


async def judge_conflicts(
    candidates: list[EvidenceEntry],
    model: BaseChatModel,
    *,
    query: str = "",
    timeout_seconds: float = 120,
) -> list[ConflictRelation]:
    known_ids = {c.id for c in candidates}
    if len(known_ids) < 2:
        return []

    payload = json.dumps(
        {
            "query": query,
            "candidates": [{"id": c.id, "content": c.content} for c in candidates],
        },
        ensure_ascii=False,
    )
    input_message = HumanMessage(content=payload)
    bound_model = model.bind(max_tokens=4096)

    try:
        structured_model = bound_model.with_structured_output(
            JudgeConflictOutput,
            method="json_mode",
        )
        async with asyncio.timeout(timeout_seconds):
            parsed = await structured_model.ainvoke(
                [SystemMessage(content=_JUDGE_SYSTEM_PROMPT), input_message]
            )
        if isinstance(parsed, JudgeConflictOutput):
            return _validated_conflicts(
                [rel.model_dump() for rel in parsed.conflicts], known_ids
            )
        raise ValueError("结构化输出类型异常")
    except Exception as exc:
        logger.warning(
            "judge 结构化调用失败（%s），回退纯文本解析", type(exc).__name__
        )
        try:
            async with asyncio.timeout(timeout_seconds):
                raw = await bound_model.ainvoke(
                    [
                        SystemMessage(content=_JUDGE_SYSTEM_PROMPT),
                        input_message,
                        HumanMessage(content="只返回上述 JSON，不要解释、不要 Markdown。"),
                    ]
                )
            data = _parse_fenced_or_raw(str(raw.content))
            conflicts = data.get("conflicts") if isinstance(data, dict) else None
            if not isinstance(conflicts, list):
                raise ValueError("兜底解析结果缺少 conflicts 数组")
            return _validated_conflicts(conflicts, known_ids)
        except Exception:
            logger.error("judge 兜底解析也失败", exc_info=True)
            raise
