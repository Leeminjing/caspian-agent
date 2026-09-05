"""ConflictQA 真实数据加载器 + 非循环权威/相似度信号。

ConflictQA(ICLR 2024, Apache-2.0)是「RAG 冲突知识」领域的公开 benchmark。
每条:question + ground_truth + 一对冲突证据(memory 答案 vs counter 答案)。

数据文件未入库(见 .gitignore),首次运行前下载:
  https://huggingface.co/datasets/osunlp/ConflictQA/resolve/main/conflictQA-popQA-chatgpt.json
放到 data/conflictqa-popqa-chatgpt.json。

非循环信号(仅由文本性质决定,不依赖 ground_truth):
- score(相似度)= 词面 Jaccard(query, evidence)
- level(权威) = 事实具体度(数字/年份/专名密度)高 → L3,低 → L1
  (事实具体度是「来源可靠性」的可计算代理:Wikipedia/官方文本具体,幻觉文本空泛)
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from caspian.benchmarks.rag.schema import RagCandidate, RagConflict, RagItem

_DATA = Path(__file__).resolve().parent / "data" / "conflictqa-popqa-chatgpt.json"


def _tok(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def _jaccard(query: str, evidence: str) -> float:
    qt, et = _tok(query), _tok(evidence)
    if not qt or not et:
        return 0.0
    return len(qt & et) / len(qt | et)


def _specificity(evidence: str) -> int:
    ev = evidence or ""
    digits = len(re.findall(r"\d", ev))
    years = len(re.findall(r"\b(19|20)\d{2}\b", ev))
    caps = len(re.findall(r"\b[A-Z][a-z]+\b", ev))
    return digits + years * 2 + caps


def _matches(answer: str, truths: list) -> bool:
    a = (answer or "").lower()
    for t in truths:
        t = str(t).lower().strip()
        if t and (re.search(r"\b" + re.escape(t) + r"\b", a) or t in a):
            return True
    return False


def load_conflictqa(path: str | Path = _DATA) -> list[RagItem]:
    raw: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                raw.append(json.loads(line))

    items: list[RagItem] = []
    for i, d in enumerate(raw):
        mem = _matches(d.get("memory_answer"), d.get("ground_truth", []))
        ctr = _matches(d.get("counter_answer"), d.get("ground_truth", []))
        if mem == ctr:
            continue  # 无法判定对错(都匹配或都不匹配),跳过
        pa = d.get("parametric_memory_aligned_evidence") or d.get("parametric_memory") or ""
        ca = d.get("counter_memory_aligned_evidence") or d.get("counter_memory") or ""
        correct_ev, wrong_ev = (pa, ca) if mem else (ca, pa)

        query = d["question"]
        spec_c, spec_w = _specificity(correct_ev), _specificity(wrong_ev)
        # 权威(非循环):事实具体度更高 → L3,更低 → L1
        correct_level = 3 if spec_c >= spec_w else 1
        wrong_level = 1 if spec_c >= spec_w else 3

        items.append(
            RagItem(
                id=f"cq-{i}",
                query=query,
                candidates=[
                    RagCandidate(id="correct", content=correct_ev, level=correct_level, score=_jaccard(query, correct_ev)),
                    RagCandidate(id="wrong", content=wrong_ev, level=wrong_level, score=_jaccard(query, wrong_ev)),
                ],
                conflicts=[RagConflict(a="correct", b="wrong", relation="explicit")],
                ground_truth="correct",
                answers=list(d.get("ground_truth", [])),
            )
        )
    return items
