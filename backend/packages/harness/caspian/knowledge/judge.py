"""
本文件对外提供 LLM 冲突判定函数 judge_conflicts。

对外提供:
    judge_conflicts — 单次批量调用模型，判定候选证据两两之间与查询命题相关的冲突关系

输入:
    candidates: list[EvidenceEntry] — 候选证据（仅用 id 与 content；等级不参与判定）
    model: BaseChatModel — 已构造的聊天模型
    query: str — 用户查询文本，冲突判定只针对与查询命题相关的关系
    timeout_seconds: float — 单次模型请求超时，默认 120

输出:
    list[ConflictRelation] — 两两冲突关系（explicit/potential × full/partial）；
        partial 冲突必须给出锚定原文的 claim 与 span，未锚定者降级为 potential

具体工作流:
    (1) 组装 candidates JSON 与判定 system prompt（含用户查询）
    (2) 结构化输出（json_mode）单次批量调用，解析失败回退纯文本 json.loads
    (3) _validated_conflicts 过滤非法关系并校验 partial 命题锚定原文（claim ⊆ content）
    (4) 全部失败 → 抛异常，由调用方降级为"未治理"

示例:
    conflicts = await judge_conflicts(candidates, model, query="功能 A 是否已废弃？")
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
   并给出各自冲突命题的原文 claim_a（对应 a）与 claim_b（对应 b）。
   claim_a/claim_b 必须是证据原文中的**精确子串**，并同时给出其在原文中的
   claim_a_span/claim_b_span（[起, 止) 字符下标，止不含）；若无法给出精确子串
   与 span，则只能标 relation="potential"。
   scope="full" 仅用于整条证据整体与对方对立的情况（此时 claim 留空）。
6. 输出必须是一个 JSON 对象，格式：
{"conflicts": [{"a": "<id>", "b": "<id>", "relation": "explicit|potential", "scope": "full|partial", "claim_a": "...", "claim_b": "...", "claim_a_span": [起,止], "claim_b_span": [起,止]}]}"""

_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)


def _parse_fenced_or_raw(text: str) -> dict | None:
    """从模型文本中提取 JSON 对象（fenced 优先，其次整段解析）。"""
    fenced = _FENCED_JSON_RE.search(text)
    if fenced:
        return json.loads(fenced.group(1))
    return json.loads(text)


def _anchor(
    content: str, claim: str, span: tuple[int, int] | None
) -> tuple[int, int] | None:
    """校验命题锚定原文：claim 必须命中 content 的精确子串。

    输入:
        content: str — 证据原文
        claim: str — 待校验的冲突命题
        span: tuple[int, int] | None — judge 给出的 [起,止) 下标

    输出:
        tuple[int, int] | None — 命中返回 (起,止)，未命中或空 claim 返回 None
    """
    if not claim:
        return None
    if span is not None:
        try:
            start, end = span
            if isinstance(start, int) and isinstance(end, int) and 0 <= start <= end <= len(content):
                if content[start:end] == claim:
                    return (start, end)
        except (TypeError, ValueError):
            pass
    idx = content.find(claim)
    if idx >= 0:
        return (idx, idx + len(claim))
    return None


def _validated_conflicts(
    raw_conflicts: list[dict],
    known_ids: set[str],
    content_by_id: dict[str, str] | None = None,
) -> list[ConflictRelation]:
    """过滤未知 id、非法枚举、自环与重复对，并校验 partial 命题锚定原文。

    输入:
        raw_conflicts: list[dict] — judge 原始输出
        known_ids: set[str] — 候选证据 id 集合
        content_by_id: dict[str, str] | None — id → 原文映射；None 时跳过锚定校验

    输出:
        list[ConflictRelation] — 合法冲突关系；partial 且无法锚定者降级为 potential
    """
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

        claim_a = str(item.get("claim_a", "") or "")
        claim_b = str(item.get("claim_b", "") or "")
        claim_a_span = item.get("claim_a_span")
        claim_b_span = item.get("claim_b_span")
        # 模型可能输出 [] / "" / null 表示"无锚"（full 冲突常见），统一归一化为 None
        if not claim_a_span:
            claim_a_span = None
        if not claim_b_span:
            claim_b_span = None

        # partial 冲突必须锚定原文：claim ⊆ content；未锚定 → 降级 potential，不压制
        if scope == "partial" and content_by_id is not None:
            a_content = content_by_id.get(a, "")
            b_content = content_by_id.get(b, "")
            anchored_a = _anchor(a_content, claim_a, claim_a_span)
            anchored_b = _anchor(b_content, claim_b, claim_b_span)
            if anchored_a is None or anchored_b is None:
                logger.warning(
                    "judge partial 冲突命题未锚定原文，降级为 potential: a=%s b=%s", a, b
                )
                relation = "potential"
                claim_a_span = None
                claim_b_span = None
            else:
                claim_a_span = anchored_a
                claim_b_span = anchored_b

        result.append(
            ConflictRelation(
                a=str(a),
                b=str(b),
                relation=relation,
                scope=scope,
                claim_a=claim_a,
                claim_b=claim_b,
                claim_a_span=claim_a_span,
                claim_b_span=claim_b_span,
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

    content_by_id = {c.id: c.content for c in candidates}
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
                [rel.model_dump() for rel in parsed.conflicts], known_ids, content_by_id
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
            return _validated_conflicts(conflicts, known_ids, content_by_id)
        except Exception:
            logger.error("judge 兜底解析也失败", exc_info=True)
            raise
