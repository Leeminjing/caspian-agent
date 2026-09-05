"""治理轴四臂:plain / score-based / source-count / level-governed。

level-governed 复用生产 `caspian.knowledge.governance.govern`;
score-based / source-count 是 benchmark 侧朴素基线(用相似度/来源数替代等级裁决)。
"""

from __future__ import annotations

from caspian.benchmarks.rag.schema import RagItem
from caspian.knowledge.governance import govern
from caspian.knowledge.schemas import ConflictRelation, EvidenceEntry


def arm_plain(item: RagItem) -> set[str]:
    """无治理:冲突双方都保留。"""
    return {c.id for c in item.candidates}


def _suppress_by_metric(item: RagItem, metric: dict[str, float]) -> set[str]:
    """对每条 explicit 冲突,压制 metric 值较低的一方(值相等不压制)。"""
    suppressed: set[str] = set()
    for rel in item.conflicts:
        if rel.relation != "explicit":
            continue
        a, b = rel.a, rel.b
        if metric[a] == metric[b]:
            continue
        suppressed.add(a if metric[a] < metric[b] else b)
    return suppressed


def arm_score_based(item: RagItem) -> set[str]:
    """按相似度裁决:压制相似度较低的一方。"""
    metric = {c.id: c.score for c in item.candidates}
    suppressed = _suppress_by_metric(item, metric)
    return {c.id for c in item.candidates} - suppressed


def arm_source_count(item: RagItem) -> set[str]:
    """按来源数裁决:压制来源较少的一方。"""
    metric = {c.id: float(c.source_count) for c in item.candidates}
    suppressed = _suppress_by_metric(item, metric)
    return {c.id for c in item.candidates} - suppressed


def arm_level_governed(item: RagItem) -> set[str]:
    """按权威等级裁决(生产实现):高等级压制低等级。"""
    candidates = [
        EvidenceEntry(id=c.id, content=c.content, level=c.level, score=c.score)
        for c in item.candidates
    ]
    conflicts = [
        ConflictRelation(a=rel.a, b=rel.b, relation=rel.relation, scope=rel.scope)
        for rel in item.conflicts
    ]
    result = govern(candidates, conflicts)
    return {e.id for e in result.final_evidence_set}


ARMS = {
    "plain": arm_plain,
    "score-based": arm_score_based,
    "source-count": arm_source_count,
    "level-governed": arm_level_governed,
}
