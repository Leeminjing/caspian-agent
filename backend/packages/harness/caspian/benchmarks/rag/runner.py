"""
本文件对外提供 RAG benchmark 编排函数。

对外提供:
    governance_axis — 运行治理四臂并统计错误采纳/正确保留
    run_all — 加载既有冲突语料和 Evidence Unit 语料，汇总治理、检索、可靠性与完整性
    run_conflictqa — 在可选真实 ConflictQA 数据上运行三臂

输入:
    corpus YAML 路径或内置语料路径。

输出:
    可 JSON 序列化的 benchmark 指标字典。

具体工作流:
    加载语料后分别运行治理、检索、可靠性和 Evidence Unit 机械复验；不把完整性臂接入
    真实 embedding 或 LLM。

示例:
    data = run_all()
"""

from __future__ import annotations

from pathlib import Path

from caspian.benchmarks.rag.arms import ARMS
from caspian.benchmarks.rag.evidence_integrity import evidence_integrity_metrics
from caspian.benchmarks.rag.oracle import correct_info_retained, wrong_info_adopted
from caspian.benchmarks.rag.reliability import reliability_report
from caspian.benchmarks.rag.retrieval import retrieval_axis
from caspian.benchmarks.rag.schema import RagItem, load_evidence_unit_corpus, load_rag_corpus

_CORPUS = Path(__file__).resolve().parent / "corpus.yaml"
_EVIDENCE_CORPUS = Path(__file__).resolve().parent / "evidence_units.yaml"


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
    evidence = load_evidence_unit_corpus(_EVIDENCE_CORPUS)
    return {
        "n": len(items),
        "governance": governance_axis(items),
        "retrieval": retrieval_axis(items),
        "reliability": reliability_report(items),
        "evidence_units": evidence_integrity_metrics(evidence, lambda text: len(text.split())),
    }


_REAL_ARMS = {
    "plain": ARMS["plain"],
    "score-based": ARMS["score-based"],
    "level-governed": ARMS["level-governed"],
}


def run_conflictqa() -> dict:
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
