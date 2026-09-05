"""四轴跑分编排:加载语料 → 轴B/轴C/轴A → 汇总。"""

from __future__ import annotations

from pathlib import Path

from caspian.benchmarks.rag.arms import ARMS
from caspian.benchmarks.rag.oracle import correct_info_retained, wrong_info_adopted
from caspian.benchmarks.rag.reliability import reliability_report
from caspian.benchmarks.rag.retrieval import retrieval_axis
from caspian.benchmarks.rag.schema import RagItem, load_rag_corpus

_CORPUS = Path(__file__).resolve().parent / "corpus.yaml"


def governance_axis(items: list[RagItem]) -> dict:
    out: dict[str, dict] = {}
    n = len(items)
    for name, arm_fn in ARMS.items():
        wrong = correct = 0
        for item in items:
            final = arm_fn(item)
            if wrong_info_adopted(item, final):
                wrong += 1
            if correct_info_retained(item, final):
                correct += 1
        out[name] = {"wrong": wrong, "correct": correct, "n": n}
    return out


def run_all(corpus_path: str | Path = _CORPUS) -> dict:
    items = load_rag_corpus(corpus_path)
    return {
        "n": len(items),
        "governance": governance_axis(items),
        "retrieval": retrieval_axis(items),
        "reliability": reliability_report(items),
    }


_REAL_ARMS = {
    "plain": ARMS["plain"],
    "score-based": ARMS["score-based"],
    "level-governed": ARMS["level-governed"],
}


def run_conflictqa() -> dict:
    """在真实 ConflictQA 数据(约 7.7K 条)上跑治理轴三臂。"""
    from caspian.benchmarks.rag.conflictqa import load_conflictqa

    items = load_conflictqa()
    n = len(items)
    arms: dict[str, dict] = {}
    for name, fn in _REAL_ARMS.items():
        wrong = correct = 0
        for item in items:
            final = fn(item)
            if wrong_info_adopted(item, final):
                wrong += 1
            if correct_info_retained(item, final):
                correct += 1
        arms[name] = {"wrong": wrong, "correct": correct, "n": n}
    return {"n": n, "arms": arms}
