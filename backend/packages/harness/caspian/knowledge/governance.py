"""
本文件对外提供离散等级治理纯函数引擎 govern。

对外提供:
    govern — 输入候选证据与冲突关系，输出本次查询的治理结果（最终证据集 + 账本 + 提示）

输入:
    candidates: list[EvidenceEntry] — 召回候选（含等级；score 不参与任何治理判定）
    conflicts: list[ConflictRelation] — judge 输出的两两冲突关系

输出:
    GovernanceResult — final_evidence_set / ledger / notes

具体工作流:
    (1) 过滤未知 id、自环的冲突关系；explicit 与 potential 分开处理
    (2) 未评级(None)为独立裁决类：不压制他人、也不被他人压制
    (3) 跨等级 explicit 冲突按等级分组（降序）压制：高等级压制低等级，被压制者
        不再压制他人；scope=partial 记录被压命题及其锚 span
    (4) 同等级 explicit 冲突 → conflict_same_level，等级系统不裁决
    (5) potential 冲突 → 双方保留 + note"潜在分歧"
    (6) 组装 ledger 与 final_evidence_set：排除 suppressed；retained_partial 按
        span 裁掉被压命题（非冲突余文保留）；按等级降序重排
    (7) 结果零持久化：压制只存在于本次返回对象，不影响其他查询

示例:
    result = govern(
        [EvidenceEntry(id="a", content="功能已废弃", level=3, score=0.61),
         EvidenceEntry(id="b", content="功能仍推荐", level=1, score=0.98)],
        [ConflictRelation(a="a", b="b", relation="explicit", scope="full")],
    )
    # → a retained，b suppressed（相似度 0.98 不改变结果）
"""

import logging

from caspian.knowledge.schemas import (
    ConflictRelation,
    EvidenceEntry,
    FinalEvidence,
    GovernanceResult,
    LedgerEntry,
    level_value,
)

logger = logging.getLogger(__name__)


def _sort_key(entry: EvidenceEntry, order: dict[str, int]):
    """排序键：非未评级按等级降序、原序升序；未评级排最后。"""
    return (entry.level is None, -(entry.level or 0), order[entry.id])


def _claim_for(rel: ConflictRelation, loser_id: str) -> str:
    """取冲突关系中属于 loser 一侧的命题文本。"""
    return rel.claim_b if rel.b == loser_id else rel.claim_a


def _span_for(rel: ConflictRelation, loser_id: str) -> tuple[int, int] | None:
    """取冲突关系中属于 loser 一侧的命题锚 span。"""
    return rel.claim_b_span if rel.b == loser_id else rel.claim_a_span


def _splice(content: str, spans: list[tuple[int, int]]) -> str:
    """按锚 span 从原文中裁掉被压命题（span 升序、去重叠）。"""
    ordered = sorted(
        (s for s in spans if s and s[0] < s[1]),
        key=lambda s: s[0],
    )
    result: list[str] = []
    prev = 0
    for start, end in ordered:
        if start < prev:
            continue
        result.append(content[prev:start])
        prev = end
    result.append(content[prev:])
    return "".join(result)


def govern(
    candidates: list[EvidenceEntry],
    conflicts: list[ConflictRelation],
) -> GovernanceResult:
    by_id: dict[str, EvidenceEntry] = {c.id: c for c in candidates}
    order: dict[str, int] = {c.id: i for i, c in enumerate(candidates)}

    explicit: list[ConflictRelation] = []
    potential: list[ConflictRelation] = []
    for rel in conflicts:
        if rel.a not in by_id or rel.b not in by_id or rel.a == rel.b:
            logger.warning("治理忽略无效冲突关系: a=%s b=%s", rel.a, rel.b)
            continue
        if rel.relation == "explicit":
            explicit.append(rel)
        else:
            potential.append(rel)

    # id → {status, reasons, claims, spans, full}
    state: dict[str, dict] = {}
    sorted_entries = sorted(candidates, key=lambda e: _sort_key(e, order))

    # (3) 跨等级压制：按等级降序，未评级不压制/不被压制，被压制者不再压制他人
    for entry in sorted_entries:
        if entry.level is None:
            continue
        if state.get(entry.id, {}).get("status") == "suppressed":
            continue
        my_level = level_value(entry.level)
        for rel in explicit:
            if entry.id not in (rel.a, rel.b):
                continue
            other_id = rel.b if rel.a == entry.id else rel.a
            other = by_id[other_id]
            if other.level is None:
                continue
            other_level = level_value(other.level)
            if other_level >= my_level:
                continue
            st = state.setdefault(other_id, {
                "status": "", "reasons": [], "claims": [], "spans": [], "full": False,
            })
            if st["status"] == "suppressed":
                continue
            st["status"] = "suppressed"
            st["reasons"].append(
                f"与更高等级证据 {entry.id}（{entry.level_display}）冲突"
            )
            if rel.scope == "full":
                st["full"] = True
            else:
                claim = _claim_for(rel, other_id)
                if claim:
                    st["claims"].append(claim)
                    span = _span_for(rel, other_id)
                    if span:
                        st["spans"].append(span)

    # (4) 同等级 explicit 冲突 → conflict_same_level（未评级不参与同级裁决）
    same_level_pairs: list[tuple[str, str, str]] = []
    seen_same_level: set[frozenset] = set()
    for rel in explicit:
        a_lv = by_id[rel.a].level
        b_lv = by_id[rel.b].level
        if a_lv is None or b_lv is None or level_value(a_lv) != level_value(b_lv):
            continue
        pair = frozenset((rel.a, rel.b))
        if pair in seen_same_level:
            continue
        seen_same_level.add(pair)
        same_level_pairs.append((rel.a, rel.b, by_id[rel.a].level_display))

    for a_id, b_id, lv in same_level_pairs:
        for eid in (a_id, b_id):
            st = state.setdefault(eid, {
                "status": "", "reasons": [], "claims": [], "spans": [], "full": False,
            })
            if st["status"] == "suppressed":
                continue
            st["status"] = "conflict_same_level"
            st["reasons"].append(
                f"与同等级证据 {b_id if eid == a_id else a_id}（{lv}）冲突，等级系统不裁决"
            )

    # (5) potential 冲突 → 双方保留 + 提示
    for rel in potential:
        for eid in (rel.a, rel.b):
            st = state.setdefault(eid, {
                "status": "", "reasons": [], "claims": [], "spans": [], "full": False,
            })
            if st["status"] in ("suppressed", "conflict_same_level"):
                continue
            st["status"] = "potential_conflict"
            st["reasons"].append(
                f"与证据 {rel.b if eid == rel.a else rel.a} 可能存在冲突"
            )

    notes: list[str] = []
    for a_id, b_id, lv in same_level_pairs:
        notes.append(
            f"同等级冲突：证据 {a_id}（{lv}）与证据 {b_id}（{lv}）对同一事实给出矛盾结论，等级治理不裁决。"
        )
    for rel in potential:
        notes.append(
            f"证据 {rel.a} 与证据 {rel.b} 可能存在冲突，当前检索结果存在潜在分歧。"
        )

    # (6) 组装 ledger 与 final_evidence_set（按等级降序）
    ledger: list[LedgerEntry] = []
    final_set: list[FinalEvidence] = []
    for entry in sorted_entries:
        st = state.get(entry.id, {})
        status = st.get("status", "")
        reasons = list(dict.fromkeys(st.get("reasons", [])))
        claims = list(dict.fromkeys(st.get("claims", [])))
        spans = list(st.get("spans", []))

        if status == "":
            status = "unrated" if entry.level is None else "retained"
        if status == "unrated" and not reasons:
            reasons.append("未评级，未参与等级压制")

        if status == "suppressed":
            if st.get("full") or not claims:
                ledger.append(LedgerEntry(
                    id=entry.id,
                    level_display=entry.level_display,
                    status="suppressed",
                    reason="；".join(reasons),
                ))
                continue
            status = "retained_partial"

        # partial 真删：裁掉被压命题；无 span 时回退按命题文本子串定位
        effective_spans = list(spans)
        if status == "retained_partial" and not effective_spans:
            for claim in claims:
                idx = entry.content.find(claim)
                if idx >= 0:
                    effective_spans.append((idx, idx + len(claim)))
        content = _splice(entry.content, effective_spans) if status == "retained_partial" else entry.content

        ledger.append(LedgerEntry(
            id=entry.id,
            level_display=entry.level_display,
            status=status,
            reason="；".join(reasons),
            suppressed_claims=claims if status == "retained_partial" else [],
        ))
        final_set.append(FinalEvidence(
            id=entry.id,
            content=content,
            level_display=entry.level_display,
            suppressed_claims=claims if status == "retained_partial" else [],
        ))

    return GovernanceResult(
        final_evidence_set=final_set,
        ledger=ledger,
        notes=list(dict.fromkeys(notes)),
    )
