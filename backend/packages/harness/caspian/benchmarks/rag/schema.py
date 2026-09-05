"""冲突知识语料的 schema 与加载。

每条语料(RagItem):
    id / query / candidates(≥2,含 level/score/source_count)/ conflicts(≥1 explicit)
    / ground_truth(事实正确方的候选 id)

语料刻意让错误方「相似度更高、来源更多、权威更低」,复现真实「流行但过时」陷阱。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class RagCandidate:
    id: str
    content: str
    level: int | None
    score: float
    source_count: int = 1


@dataclass
class RagConflict:
    a: str
    b: str
    relation: str = "explicit"
    scope: str = "full"


@dataclass
class RagItem:
    id: str
    query: str
    candidates: list[RagCandidate]
    conflicts: list[RagConflict]
    ground_truth: str
    answers: list[str] = field(default_factory=list)


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ValueError(msg)


def _parse_candidate(item, item_id: str) -> RagCandidate:
    if not isinstance(item, dict):
        raise ValueError(f"{item_id}: candidate 必须是对象")
    cid = str(item.get("id", "") or "").strip()
    content = str(item.get("content", "") or "").strip()
    level = item.get("level")
    score = item.get("score")
    source_count = item.get("source_count", 1)
    _require(bool(cid) and bool(content), f"{item_id}: candidate 缺 id/content")
    _require(level is None or (isinstance(level, int) and 0 <= level <= 3), f"{item_id}: {cid} level 非法")
    _require(isinstance(score, (int, float)), f"{item_id}: {cid} 缺 score")
    _require(isinstance(source_count, int) and source_count >= 1, f"{item_id}: {cid} source_count 非法")
    return RagCandidate(id=cid, content=content, level=level, score=float(score), source_count=source_count)


def _parse_item(item, index: int) -> RagItem:
    if not isinstance(item, dict):
        raise ValueError(f"corpus 第 {index} 项必须是对象")
    item_id = str(item.get("id", "") or "").strip()
    query = str(item.get("query", "") or "").strip()
    _require(bool(item_id) and bool(query), f"corpus 第 {index} 项缺 id/query")
    cand_raw = item.get("candidates")
    _require(isinstance(cand_raw, list) and len(cand_raw) >= 2, f"{item_id}: candidates 需 ≥2")
    candidates = [_parse_candidate(c, item_id) for c in cand_raw]
    ids = {c.id for c in candidates}
    _require(len(ids) == len(candidates), f"{item_id}: candidate id 重复")
    ground_truth = str(item.get("ground_truth", "") or "").strip()
    _require(ground_truth in ids, f"{item_id}: ground_truth 不在候选 id 中")
    conf_raw = item.get("conflicts")
    _require(isinstance(conf_raw, list) and conf_raw, f"{item_id}: conflicts 需非空")
    conflicts: list[RagConflict] = []
    for c in conf_raw:
        if not isinstance(c, dict):
            raise ValueError(f"{item_id}: conflict 必须是对象")
        a, b = str(c.get("a", "") or ""), str(c.get("b", "") or "")
        _require(a in ids and b in ids and a != b, f"{item_id}: 非法冲突对 {a}/{b}")
        conflicts.append(RagConflict(a=a, b=b, relation=str(c.get("relation", "explicit")), scope=str(c.get("scope", "full"))))
    return RagItem(id=item_id, query=query, candidates=candidates, conflicts=conflicts, ground_truth=ground_truth)


def load_rag_corpus(path: str | Path) -> list[RagItem]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("items", [])
    _require(isinstance(raw, list) and raw, f"{path}: corpus 需含非空 items")
    return [_parse_item(item, i) for i, item in enumerate(raw)]
