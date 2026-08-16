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
    (2) 候选按等级降序（同级保持原序）单遍处理，规则:
        - 当前条目已被压制 → 跳过（被压制者不能继续压制他人）
        - 与更低等级的 explicit 冲突伙伴 → 压制对方；scope=partial 记录被压命题
          文本（内容不删改），scope=full 则整体失去决策资格
        - 与同等级 explicit 冲突伙伴 → 双方保留并标记 conflict_same_level，
          等级系统不裁决
    (3) potential 冲突 → 双方保留 + note"潜在分歧"（不确定不压制）
    (4) 组装 ledger（状态 + 原因 + 被压命题）与 final_evidence_set（排除
        suppressed，含 retained_partial 的 suppressed_claims 注解）
    (5) 结果零持久化：压制只存在于本次返回对象，不影响其他查询

    压制依赖 judge 的直接冲突边：同一命题的对立结论应由 judge 产出直接边。
    # ponytail: 按等级降序单遍 + 同级原序，O(n²)；候选≤20，无需更复杂结构

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
    level_display,
    level_value,
)

logger = logging.getLogger(__name__)


def _key(order: dict[str, int], entry: EvidenceEntry) -> tuple[int, int]:
    """按等级降序、原序升序的排序键。"""
    return (-level_value(entry.level), order[entry.id])


def _claim_for(
    rel: ConflictRelation, loser_id: str
) -> str:
    """取冲突关系中属于 loser 一侧的命题文本。"""
    return rel.claim_b if rel.b == loser_id else rel.claim_a


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

    # id → {status, reasons: [str], claims: [str], full: bool}
    state: dict[str, dict] = {}
    same_level_pairs: list[tuple[str, str, str]] = []  # (a, b, level_display)
    seen_same_level: set[frozenset] = set()

    sorted_entries = sorted(candidates, key=lambda e: _key(order, e))
    for entry in sorted_entries:
        if entry.id in state and state[entry.id]["status"] == "suppressed":
            continue

        for rel in explicit:
            if entry.id not in (rel.a, rel.b):
                continue
            other_id = rel.b if rel.a == entry.id else rel.a
            other = by_id[other_id]
            if state.get(other_id, {}).get("status") == "suppressed":
                continue  # 被压制者不能再压制他人

            my_level = level_value(entry.level)
            other_level = level_value(other.level)
            if other_level > my_level:
                continue  # 更高等级的对手会先于本条目处理，此处不应出现
            if other_level < my_level:
                st = state.setdefault(other_id, {
                    "status": "suppressed",
                    "reasons": [],
                    "claims": [],
                    "full": False,
                })
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
            else:  # 同等级
                pair = frozenset((entry.id, other_id))
                if pair not in seen_same_level:
                    seen_same_level.add(pair)
                    same_level_pairs.append(
                        (entry.id, other_id, entry.level_display)
                    )

    # 同等级冲突标记（保留、不裁决）
    for a_id, b_id, lv in same_level_pairs:
        for eid in (a_id, b_id):
            st = state.setdefault(eid, {"status": "", "reasons": [], "claims": [], "full": False})
            if st["status"] == "suppressed":
                continue
            st["status"] = "conflict_same_level"
            st["reasons"].append(f"与同等级证据 {b_id if eid == a_id else a_id}（{lv}）冲突，等级系统不裁决")

    # potential 冲突（保留 + 提示）
    for rel in potential:
        for eid in (rel.a, rel.b):
            st = state.setdefault(eid, {"status": "", "reasons": [], "claims": [], "full": False})
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

    ledger: list[LedgerEntry] = []
    final_set: list[FinalEvidence] = []
    for entry in candidates:
        st = state.get(entry.id, {})
        status = st.get("status", "") or "retained"
        reasons = list(dict.fromkeys(st.get("reasons", [])))
        claims = list(dict.fromkeys(st.get("claims", [])))
        if status == "suppressed":
            if st.get("full") or not claims:
                ledger.append(
                    LedgerEntry(
                        id=entry.id,
                        level_display=entry.level_display,
                        status="suppressed",
                        reason="；".join(reasons),
                    )
                )
                continue
            status = "retained_partial"
        ledger.append(
            LedgerEntry(
                id=entry.id,
                level_display=entry.level_display,
                status=status,
                reason="；".join(reasons),
                suppressed_claims=claims if status == "retained_partial" else [],
            )
        )
        final_set.append(
            FinalEvidence(
                id=entry.id,
                content=entry.content,
                level_display=entry.level_display,
                suppressed_claims=claims if status == "retained_partial" else [],
            )
        )

    return GovernanceResult(
        final_evidence_set=final_set,
        ledger=ledger,
        notes=list(dict.fromkeys(notes)),
    )
