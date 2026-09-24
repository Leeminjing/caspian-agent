"""
本文件对外提供既有冲突 RAG 语料与 Evidence Unit 完整性语料的 schema 和加载函数。

对外提供:
    RagCandidate / RagConflict / RagItem / load_rag_corpus — 治理轴既有语料协议
    EvidenceUnitFixture / EvidenceGovernanceProbe / EvidencePartialProbe / EvidenceUnitCorpus —
    文档、身份、时态、partial eligibility 与治理粒度的机械复验协议
    FactClusterCase / load_fact_cluster_corpus — 版本化事实簇边界金标语料

输入:
    YAML 文件路径；候选等级、score、来源数及 Evidence Unit 追踪字段。

输出:
    dataclass 列表或 corpus；缺字段、重复身份、非法边界/等级/关系会抛 ValueError。

具体工作流:
    加载 YAML 后逐层检查对象形状、候选引用与 ground truth；Evidence Unit 语料额外保存完整
    文档原文、source span、atomicity 与 bindings；事实簇语料验证金标覆盖而不调用真实 LLM。

示例:
    items = load_rag_corpus("corpus.yaml")
    evidence = load_evidence_unit_corpus("evidence_units.yaml")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from caspian.knowledge.evidence import TemporalBinding


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


@dataclass
class EvidenceUnitFixture:
    chunk_id: str
    document_id: str
    document_revision_id: str
    document_content: str
    content: str
    retrieval_text: str
    source: str
    source_span: tuple[int, int]
    title: str = ""
    section_path: tuple[str, ...] = ()
    version: str | None = None
    published_at: str | None = None
    effective_at: str | None = None
    level: int | None = None
    level_basis: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)
    atomicity: str = "atomic"
    temporal_bindings: tuple[TemporalBinding, ...] = ()


@dataclass
class EvidenceGovernanceProbe:
    conflict_a: str
    conflict_b: str
    changed_id: str
    changed_level: int
    unrelated_id: str


@dataclass
class EvidencePartialProbe:
    a: str
    b: str
    claim_a: str
    claim_b: str
    claim_a_span: tuple[int, int]
    claim_b_span: tuple[int, int]
    expected_relation: str
    expected_scope: str


@dataclass
class EvidenceUnitCorpus:
    units: list[EvidenceUnitFixture]
    conflicts: list[RagConflict]
    governance_probe: EvidenceGovernanceProbe | None = None
    partial_probes: list[EvidencePartialProbe] = field(default_factory=list)


@dataclass
class FactClusterBoundary:
    source_span: tuple[int, int]
    atomicity: str
    temporal_bindings: tuple[TemporalBinding, ...] = ()


@dataclass
class FactClusterCase:
    id: str
    kind: str
    content: str
    expect_semantic_split: bool
    boundaries: list[FactClusterBoundary]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _parse_candidate(item, item_id: str) -> RagCandidate:
    _require(isinstance(item, dict), f"{item_id}: candidate 必须是对象")
    candidate_id = str(item.get("id", "") or "").strip()
    content = str(item.get("content", "") or "").strip()
    level = item.get("level")
    score = item.get("score")
    source_count = item.get("source_count", 1)
    _require(bool(candidate_id) and bool(content), f"{item_id}: candidate 缺 id/content")
    _require(level is None or isinstance(level, int) and 0 <= level <= 3, f"{item_id}: {candidate_id} level 非法")
    _require(isinstance(score, (int, float)), f"{item_id}: {candidate_id} 缺 score")
    _require(isinstance(source_count, int) and source_count >= 1, f"{item_id}: {candidate_id} source_count 非法")
    return RagCandidate(candidate_id, content, level, float(score), source_count)


def _parse_item(item, index: int) -> RagItem:
    _require(isinstance(item, dict), f"corpus 第 {index} 项必须是对象")
    item_id = str(item.get("id", "") or "").strip()
    query = str(item.get("query", "") or "").strip()
    _require(bool(item_id) and bool(query), f"corpus 第 {index} 项缺 id/query")
    raw_candidates = item.get("candidates")
    _require(isinstance(raw_candidates, list) and len(raw_candidates) >= 2, f"{item_id}: candidates 需 ≥2")
    candidates = [_parse_candidate(candidate, item_id) for candidate in raw_candidates]
    ids = {candidate.id for candidate in candidates}
    _require(len(ids) == len(candidates), f"{item_id}: candidate id 重复")
    ground_truth = str(item.get("ground_truth", "") or "").strip()
    _require(ground_truth in ids, f"{item_id}: ground_truth 不在候选 id 中")
    raw_conflicts = item.get("conflicts")
    _require(isinstance(raw_conflicts, list) and raw_conflicts, f"{item_id}: conflicts 需非空")
    conflicts = [_parse_conflict(conflict, ids, item_id) for conflict in raw_conflicts]
    return RagItem(item_id, query, candidates, conflicts, ground_truth)


def _parse_conflict(item, ids: set[str], context: str) -> RagConflict:
    _require(isinstance(item, dict), f"{context}: conflict 必须是对象")
    a = str(item.get("a", "") or "")
    b = str(item.get("b", "") or "")
    relation = str(item.get("relation", "explicit"))
    scope = str(item.get("scope", "full"))
    _require(a in ids and b in ids and a != b, f"{context}: 非法冲突对 {a}/{b}")
    _require(relation in ("explicit", "potential", "temporal_disjoint"), f"{context}: 非法 relation")
    _require(scope in ("full", "partial"), f"{context}: 非法 scope")
    return RagConflict(a, b, relation, scope)


def load_rag_corpus(path: str | Path) -> list[RagItem]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("items", [])
    _require(isinstance(raw, list) and raw, f"{path}: corpus 需含非空 items")
    return [_parse_item(item, index) for index, item in enumerate(raw)]


def _parse_evidence_unit(item, index: int) -> EvidenceUnitFixture:
    _require(isinstance(item, dict), f"evidence unit 第 {index} 项必须是对象")
    required = (
        "chunk_id",
        "document_id",
        "document_revision_id",
        "document_content",
        "content",
        "retrieval_text",
        "source",
        "source_span",
    )
    _require(all(key in item for key in required), f"evidence unit 第 {index} 项缺必填字段")
    span = item["source_span"]
    _require(
        isinstance(span, list)
        and len(span) == 2
        and all(isinstance(value, int) for value in span),
        f"evidence unit 第 {index} 项 span 非法",
    )
    level = item.get("level")
    _require(level is None or isinstance(level, int) and 0 <= level <= 3, f"evidence unit 第 {index} 项 level 非法")
    atomicity = str(item.get("atomicity", "atomic"))
    _require(atomicity in ("atomic", "indivisible", "legacy_unknown"), f"evidence unit 第 {index} 项 atomicity 非法")
    return EvidenceUnitFixture(
        chunk_id=str(item["chunk_id"]),
        document_id=str(item["document_id"]),
        document_revision_id=str(item["document_revision_id"]),
        document_content=str(item["document_content"]),
        content=str(item["content"]),
        retrieval_text=str(item["retrieval_text"]),
        source=str(item["source"]),
        source_span=(span[0], span[1]),
        title=str(item.get("title", "")),
        section_path=tuple(item.get("section_path") or ()),
        version=item.get("version"),
        published_at=item.get("published_at"),
        effective_at=item.get("effective_at"),
        level=level,
        level_basis=dict(item.get("level_basis") or {}),
        provenance=dict(item.get("provenance") or {}),
        atomicity=atomicity,
        temporal_bindings=tuple(
            TemporalBinding.model_validate(binding)
            for binding in item.get("temporal_bindings") or ()
        ),
    )


def _parse_governance_probe(item, ids: set[str], conflicts: list[RagConflict]) -> EvidenceGovernanceProbe:
    _require(isinstance(item, dict), "governance_probe 必须是对象")
    conflict_a = str(item.get("conflict_a", "") or "")
    conflict_b = str(item.get("conflict_b", "") or "")
    changed_id = str(item.get("changed_id", "") or "")
    unrelated_id = str(item.get("unrelated_id", "") or "")
    changed_level = item.get("changed_level")
    pair = {conflict_a, conflict_b}
    _require(len(pair) == 2 and pair <= ids, "governance_probe 冲突对非法")
    _require(changed_id in pair, "governance_probe changed_id 必须属于冲突对")
    _require(unrelated_id in ids - pair, "governance_probe unrelated_id 必须是独立单元")
    _require(isinstance(changed_level, int) and 0 <= changed_level <= 3, "governance_probe changed_level 非法")
    _require(
        any(
            {conflict.a, conflict.b} == pair
            and conflict.relation == "explicit"
            and conflict.scope == "full"
            for conflict in conflicts
        ),
        "governance_probe 必须引用 explicit full conflict",
    )
    return EvidenceGovernanceProbe(
        conflict_a=conflict_a,
        conflict_b=conflict_b,
        changed_id=changed_id,
        changed_level=changed_level,
        unrelated_id=unrelated_id,
    )


def _parse_partial_probe(item, ids: set[str]) -> EvidencePartialProbe:
    _require(isinstance(item, dict), "partial eligibility probe 必须是对象")
    a = str(item.get("a", ""))
    b = str(item.get("b", ""))
    _require(a in ids and b in ids and a != b, "partial eligibility probe 冲突对非法")
    spans = (item.get("claim_a_span"), item.get("claim_b_span"))
    _require(
        all(isinstance(span, list) and len(span) == 2 and all(isinstance(value, int) for value in span) for span in spans),
        "partial eligibility probe span 非法",
    )
    expected_relation = str(item.get("expected_relation", ""))
    expected_scope = str(item.get("expected_scope", ""))
    _require(expected_relation in ("explicit", "potential"), "partial eligibility probe expected_relation 非法")
    _require(expected_scope in ("full", "partial"), "partial eligibility probe expected_scope 非法")
    return EvidencePartialProbe(
        a=a,
        b=b,
        claim_a=str(item.get("claim_a", "")),
        claim_b=str(item.get("claim_b", "")),
        claim_a_span=(spans[0][0], spans[0][1]),
        claim_b_span=(spans[1][0], spans[1][1]),
        expected_relation=expected_relation,
        expected_scope=expected_scope,
    )


def load_evidence_unit_corpus(path: str | Path) -> EvidenceUnitCorpus:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    _require(isinstance(raw, dict), f"{path}: Evidence Unit corpus 必须是对象")
    raw_units = raw.get("units")
    _require(isinstance(raw_units, list) and raw_units, f"{path}: units 需非空")
    units = [_parse_evidence_unit(item, index) for index, item in enumerate(raw_units)]
    ids = {unit.chunk_id for unit in units}
    raw_conflicts = raw.get("conflicts") or []
    _require(isinstance(raw_conflicts, list), f"{path}: conflicts 必须是数组")
    conflicts = [_parse_conflict(item, ids, str(path)) for item in raw_conflicts]
    probe = _parse_governance_probe(raw.get("governance_probe"), ids, conflicts)
    raw_partial_probes = raw.get("partial_eligibility_probes") or []
    _require(isinstance(raw_partial_probes, list), f"{path}: partial_eligibility_probes 必须是数组")
    partial_probes = [_parse_partial_probe(item, ids) for item in raw_partial_probes]
    return EvidenceUnitCorpus(units, conflicts, probe, partial_probes)


def _parse_fact_cluster_case(item, index: int) -> FactClusterCase:
    _require(isinstance(item, dict), f"fact cluster 第 {index} 项必须是对象")
    case_id = str(item.get("id", "") or "")
    kind = str(item.get("kind", "paragraph") or "paragraph")
    content = str(item.get("content", ""))
    repeat = item.get("repeat", 1)
    _require(bool(case_id) and kind in ("paragraph", "list", "table", "code"), f"fact cluster 第 {index} 项 id/kind 非法")
    _require(isinstance(repeat, int) and repeat >= 1, f"{case_id}: repeat 非法")
    content = " ".join([content] * repeat)
    raw_boundaries = item.get("boundaries")
    _require(isinstance(raw_boundaries, list) and raw_boundaries, f"{case_id}: boundaries 需非空")
    boundaries: list[FactClusterBoundary] = []
    cursor = 0
    for raw in raw_boundaries:
        _require(isinstance(raw, dict), f"{case_id}: boundary 必须是对象")
        span = raw.get("source_span")
        _require(isinstance(span, list) and len(span) == 2, f"{case_id}: boundary span 非法")
        start = span[0]
        end = len(content) if span[1] == "all" else span[1]
        _require(isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(content), f"{case_id}: boundary span 越界")
        _require(not content[cursor:start].strip(), f"{case_id}: boundaries 遗漏非空白正文")
        atomicity = str(raw.get("atomicity", ""))
        _require(atomicity in ("atomic", "indivisible"), f"{case_id}: atomicity 非法")
        bindings = tuple(TemporalBinding.model_validate(binding) for binding in raw.get("temporal_bindings") or ())
        boundaries.append(FactClusterBoundary((start, end), atomicity, bindings))
        cursor = end
    _require(not content[cursor:].strip(), f"{case_id}: boundaries 遗漏尾部正文")
    expect_split = item.get("expect_semantic_split")
    _require(isinstance(expect_split, bool), f"{case_id}: expect_semantic_split 非法")
    return FactClusterCase(case_id, kind, content, expect_split, boundaries)


def load_fact_cluster_corpus(path: str | Path) -> list[FactClusterCase]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    _require(isinstance(raw, dict) and raw.get("version") == 1, f"{path}: fact cluster corpus 版本非法")
    cases = raw.get("cases")
    _require(isinstance(cases, list) and cases, f"{path}: cases 需非空")
    parsed = [_parse_fact_cluster_case(item, index) for index, item in enumerate(cases)]
    _require(len({item.id for item in parsed}) == len(parsed), f"{path}: case id 重复")
    return parsed
