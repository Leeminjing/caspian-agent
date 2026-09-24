"""
本文件对外提供非权威候选投影与 LLM 冲突判定。

对外提供:
    judge_candidate_payload — 仅投影 id/content/title/section_path/version/时间字段
    judge_conflicts — 单次批量判定 explicit、potential、temporal_disjoint 及 full/partial

输入:
    EvidenceEntry 候选、BaseChatModel、查询文本和超时秒数；候选 level、score、来源数量、
    level_basis 与 provenance 不属于 judge_candidate_payload 输入结果。

输出:
    list[ConflictRelation]；partial 必须锚定原文，未锚定时机械降级为 potential。

具体工作流:
    先用字段白名单构造 JSON，优先 function-calling 结构化输出，失败后解析纯 JSON；
    最后过滤未知/重复关系并校验 claim span，全部模型路径失败则向调用方抛错。

示例:
    payload = judge_candidate_payload(entry)
    conflicts = await judge_conflicts(candidates, model, query="功能 A 是否废弃？")
"""

import asyncio
import json
import logging

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from caspian.knowledge.json_parsing import parse_fenced_or_raw
from caspian.knowledge.schemas import ConflictRelation, EvidenceEntry, JudgeConflictOutput

logger = logging.getLogger(__name__)

_JUDGE_SYSTEM_PROMPT = """你是知识证据的冲突判定器。候选只包含非权威语义与时态字段。
只判定与 query 相关、同一时间或版本、同一事实的对立结论。明确相反用 explicit，
无法确认用 potential；不同 version/published_at/effective_at 的同主题事实应省略或标
temporal_disjoint。整体对立用 full。只有不可进一步合理拆分的复合证据才用 partial，
并提供双方原文精确子串 claim 与半开字符 span；不能精确锚定时只能用 potential。
只输出 {"conflicts":[{"a":"id","b":"id","relation":"explicit|potential|temporal_disjoint",
"scope":"full|partial","claim_a":"","claim_b":"","claim_a_span":[0,1],"claim_b_span":[0,1]}]}。"""


def judge_candidate_payload(candidate: EvidenceEntry) -> dict:
    return {
        "id": candidate.id,
        "content": candidate.content,
        "title": candidate.title,
        "section_path": list(candidate.section_path),
        "version": candidate.version,
        "published_at": candidate.published_at,
        "effective_at": candidate.effective_at,
    }


def _anchor(content: str, claim: str, span: tuple[int, int] | None) -> tuple[int, int] | None:
    if not claim:
        return None
    if span is not None:
        try:
            start, end = span
            if (
                isinstance(start, int)
                and isinstance(end, int)
                and 0 <= start <= end <= len(content)
                and content[start:end] == claim
            ):
                return start, end
        except (TypeError, ValueError):
            pass
    index = content.find(claim)
    return (index, index + len(claim)) if index >= 0 else None


def _normalize_partial(
    item: dict,
    content_by_id: dict[str, str],
) -> tuple[str, tuple[int, int] | None, tuple[int, int] | None]:
    relation = str(item.get("relation"))
    if item.get("scope", "full") != "partial":
        return relation, None, None
    a = str(item.get("a"))
    b = str(item.get("b"))
    a_span = _anchor(
        content_by_id.get(a, ""),
        str(item.get("claim_a", "") or ""),
        item.get("claim_a_span") or None,
    )
    b_span = _anchor(
        content_by_id.get(b, ""),
        str(item.get("claim_b", "") or ""),
        item.get("claim_b_span") or None,
    )
    if a_span is None or b_span is None:
        return "potential", None, None
    return relation, a_span, b_span


def _validated_conflicts(
    raw_conflicts: list[dict],
    known_ids: set[str],
    content_by_id: dict[str, str] | None = None,
) -> list[ConflictRelation]:
    result: list[ConflictRelation] = []
    seen_pairs: set[frozenset[str]] = set()
    for item in raw_conflicts or []:
        if not isinstance(item, dict):
            continue
        a, b = item.get("a"), item.get("b")
        relation = item.get("relation")
        if (
            a not in known_ids
            or b not in known_ids
            or a == b
            or relation not in ("explicit", "potential", "temporal_disjoint")
        ):
            continue
        pair = frozenset((str(a), str(b)))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        scope = item.get("scope", "full")
        scope = scope if scope in ("full", "partial") else "full"
        a_span = item.get("claim_a_span") or None
        b_span = item.get("claim_b_span") or None
        if scope == "partial" and content_by_id is not None:
            relation, a_span, b_span = _normalize_partial(item, content_by_id)
        result.append(
            ConflictRelation(
                a=str(a),
                b=str(b),
                relation=relation,
                scope=scope,
                claim_a=str(item.get("claim_a", "") or ""),
                claim_b=str(item.get("claim_b", "") or ""),
                claim_a_span=a_span,
                claim_b_span=b_span,
            )
        )
    return result


def _input_message(candidates: list[EvidenceEntry], query: str) -> HumanMessage:
    payload = {"query": query, "candidates": [judge_candidate_payload(candidate) for candidate in candidates]}
    return HumanMessage(content=json.dumps(payload, ensure_ascii=False))


async def judge_conflicts(
    candidates: list[EvidenceEntry],
    model: BaseChatModel,
    *,
    query: str = "",
    timeout_seconds: float = 120,
) -> list[ConflictRelation]:
    known_ids = {candidate.id for candidate in candidates}
    if len(known_ids) < 2:
        return []
    content_by_id = {candidate.id: candidate.content for candidate in candidates}
    input_message = _input_message(candidates, query)
    bound_model = model.bind(max_tokens=4096)
    messages = [SystemMessage(content=_JUDGE_SYSTEM_PROMPT), input_message]
    try:
        structured_model = bound_model.with_structured_output(JudgeConflictOutput, method="function_calling")
        async with asyncio.timeout(timeout_seconds):
            parsed = await structured_model.ainvoke(messages)
        if not isinstance(parsed, JudgeConflictOutput):
            raise ValueError("结构化输出类型异常")
        return _validated_conflicts([relation.model_dump() for relation in parsed.conflicts], known_ids, content_by_id)
    except Exception as structured_error:
        logger.warning("judge 结构化调用失败（%s），回退纯文本解析", type(structured_error).__name__)
        try:
            async with asyncio.timeout(timeout_seconds):
                raw = await bound_model.ainvoke([*messages, HumanMessage(content="只返回上述 JSON，不要解释、不要 Markdown。")])
            data = parse_fenced_or_raw(str(raw.content))
            conflicts = data.get("conflicts") if isinstance(data, dict) else None
            if not isinstance(conflicts, list):
                raise ValueError("兜底解析结果缺少 conflicts 数组")
            return _validated_conflicts(conflicts, known_ids, content_by_id)
        except Exception:
            logger.error("judge 兜底解析也失败", exc_info=True)
            raise
