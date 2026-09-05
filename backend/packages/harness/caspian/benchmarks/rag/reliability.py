"""轴A:机制科学可靠性复验(语料级)。

- 确定性:govern 纯函数,同输入同输出。
- level-faithful:对每条语料,level-governed 保留 ground_truth(最高等级)、压制错误方。
- 诚实边界:同等级不裁决 / potential 不压制 / 未评级独立(合成样例复验)。
"""

from __future__ import annotations

from caspian.benchmarks.rag.arms import arm_level_governed
from caspian.benchmarks.rag.schema import RagItem
from caspian.knowledge.governance import govern
from caspian.knowledge.schemas import ConflictRelation, EvidenceEntry


def _status_of(result, cid: str) -> str:
    for item in result.ledger:
        if item.id == cid:
            return item.status
    return "missing"


def reliability_report(items: list[RagItem]) -> dict:
    n = len(items)
    # 确定性 + level-faithful(正确信息保留率)
    correct_retained = 0
    deterministic = True
    for item in items:
        r1 = arm_level_governed(item)
        r2 = arm_level_governed(item)
        if r1 != r2:
            deterministic = False
        if item.ground_truth in r1:
            correct_retained += 1

    # 诚实边界(合成样例)
    same_level = govern(
        [EvidenceEntry(id="a", content="A", level=2), EvidenceEntry(id="b", content="非A", level=2)],
        [ConflictRelation(a="a", b="b", relation="explicit", scope="full")],
    )
    potential = govern(
        [EvidenceEntry(id="a", content="A", level=3), EvidenceEntry(id="b", content="非A", level=1)],
        [ConflictRelation(a="a", b="b", relation="potential", scope="full")],
    )
    unrated = govern(
        [EvidenceEntry(id="a", content="A", level=1), EvidenceEntry(id="b", content="非A", level=None)],
        [ConflictRelation(a="a", b="b", relation="explicit", scope="full")],
    )

    honest = {
        "同等级不裁决": _status_of(same_level, "a") == "conflict_same_level"
        and _status_of(same_level, "b") == "conflict_same_level",
        "potential 不压制": _status_of(potential, "b") == "potential_conflict",
        "未评级独立": _status_of(unrated, "b") == "unrated"
        and _status_of(unrated, "a") == "retained",
    }

    return {
        "deterministic": deterministic,
        "correct_retained": correct_retained,
        "n": n,
        "honest": honest,
        "honest_all_pass": all(honest.values()),
    }
