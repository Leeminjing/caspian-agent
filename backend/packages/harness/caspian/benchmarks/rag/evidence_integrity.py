"""
本文件对外提供 Evidence Unit benchmark 的机械完整性计数与检索字段隔离复验。

对外提供:
    evidence_integrity_metrics — 统计单元、身份碰撞、来源覆盖、overlap、hard max、非法 span、
    full/partial conflict、事实簇 gate、时态 binding、partial eligibility、检索隔离与等级使用
    retrieval_text_isolated — 复算白名单检索文本并确认治理 metadata 不影响结果
    governance_isolation_metrics — 运行生产 govern，验证等级变化生效且无关单元不变

输入:
    EvidenceUnitCorpus、FactClusterCase 列表与确定性 TokenCounter。

输出:
    dict[str, int|bool]；身份碰撞、来源覆盖、overlap、hard max、非法 span 和检索隔离违规
    均为零时 passed=True。

具体工作流:
    对 fixture 做原文/时态检查，复算 retrieval_text，运行生产 partial 归一化与 govern，
    并把事实簇金标和 gate decision 对照；不调用真实 embedding、LLM 或 Store。

示例:
    metrics = evidence_integrity_metrics(corpus, lambda text: len(text.split()))
"""

from collections import defaultdict
from collections.abc import Callable
from dataclasses import replace

from caspian.benchmarks.rag.schema import EvidenceUnitCorpus, EvidenceUnitFixture, FactClusterCase
from caspian.knowledge.chunking import evaluate_atomicity_gate
from caspian.knowledge.governance import govern
from caspian.knowledge.evidence import CandidateBlock, SourceSpan
from caspian.knowledge.judge import _validated_conflicts
from caspian.knowledge.retrieval_text import build_retrieval_text
from caspian.knowledge.schemas import ConflictRelation, EvidenceEntry


def retrieval_text_isolated(unit: EvidenceUnitFixture) -> bool:
    expected = build_retrieval_text(
        unit.content,
        title=unit.title,
        section_path=unit.section_path,
        version=unit.version,
        published_at=unit.published_at,
        effective_at=unit.effective_at,
    )
    return expected == unit.retrieval_text


def _identity_collisions(units: list[EvidenceUnitFixture]) -> int:
    fingerprints: dict[str, set[tuple]] = defaultdict(set)
    for unit in units:
        fingerprints[unit.chunk_id].add(
            (
                unit.document_id,
                unit.document_revision_id,
                unit.source_span,
                unit.content,
                unit.source,
            )
        )
    return sum(len(values) - 1 for values in fingerprints.values() if len(values) > 1)


def _source_overwrites(units: list[EvidenceUnitFixture]) -> int:
    groups: dict[str, list[EvidenceUnitFixture]] = defaultdict(list)
    for unit in units:
        groups[unit.content].append(unit)
    violations = 0
    for group in groups.values():
        if len({unit.source for unit in group}) > 1:
            violations += len(group) - len({unit.chunk_id for unit in group})
    return violations


def _overlaps(units: list[EvidenceUnitFixture]) -> int:
    revisions: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for unit in units:
        revisions[unit.document_revision_id].append(unit.source_span)
    violations = 0
    for spans in revisions.values():
        previous_end = -1
        for start, end in sorted(spans):
            violations += int(start < previous_end)
            previous_end = max(previous_end, end)
    return violations


def _invalid_spans(units: list[EvidenceUnitFixture]) -> int:
    violations = 0
    for unit in units:
        start, end = unit.source_span
        valid = 0 <= start < end <= len(unit.document_content) and unit.document_content[start:end] == unit.content
        violations += int(not valid)
    return violations


def _entry(unit: EvidenceUnitFixture) -> EvidenceEntry:
    return EvidenceEntry(
        id=unit.chunk_id,
        content=unit.content,
        level=unit.level,
        atomicity=unit.atomicity,
    )


def _expected_pair(a: EvidenceUnitFixture, b: EvidenceUnitFixture) -> set[str]:
    if a.level is None or b.level is None or a.level == b.level:
        return {a.chunk_id, b.chunk_id}
    return {a.chunk_id if a.level > b.level else b.chunk_id}


def _run_governance(
    a: EvidenceUnitFixture,
    b: EvidenceUnitFixture,
    unrelated: EvidenceUnitFixture,
):
    return govern(
        [_entry(a), _entry(b), _entry(unrelated)],
        [ConflictRelation(a=a.chunk_id, b=b.chunk_id, relation="explicit", scope="full")],
    )


def governance_isolation_metrics(corpus: EvidenceUnitCorpus) -> dict[str, int]:
    probe = corpus.governance_probe
    if probe is None:
        return {
            "governance_metadata_embedding_violations": 1,
            "governance_level_usage_violations": 1,
            "unrelated_unit_mutations": 1,
        }
    units = {unit.chunk_id: unit for unit in corpus.units}
    a = units[probe.conflict_a]
    b = units[probe.conflict_b]
    unrelated = units[probe.unrelated_id]
    original = _run_governance(a, b, unrelated)
    changed_unit = replace(
        units[probe.changed_id],
        level=probe.changed_level,
        level_basis={"reason": "governance probe"},
        provenance={"source_count": 99},
    )
    metadata_embedding_violations = int(changed_unit.retrieval_text != units[probe.changed_id].retrieval_text)
    metadata_embedding_violations += int(not retrieval_text_isolated(changed_unit))
    changed_a = changed_unit if a.chunk_id == probe.changed_id else a
    changed_b = changed_unit if b.chunk_id == probe.changed_id else b
    changed = _run_governance(changed_a, changed_b, unrelated)
    pair = {a.chunk_id, b.chunk_id}
    original_ids = {item.id for item in original.final_evidence_set} & pair
    changed_ids = {item.id for item in changed.final_evidence_set} & pair
    expected_original = _expected_pair(a, b)
    expected_changed = _expected_pair(changed_a, changed_b)
    level_violations = int(original_ids != expected_original)
    level_violations += int(changed_ids != expected_changed)
    level_violations += int(expected_original == expected_changed)
    original_content = {item.id: item.content for item in original.final_evidence_set}
    changed_content = {item.id: item.content for item in changed.final_evidence_set}
    original_status = {item.id: item.status for item in original.ledger}
    changed_status = {item.id: item.status for item in changed.ledger}
    unrelated_mutations = int(original_content.get(unrelated.chunk_id) != unrelated.content)
    unrelated_mutations += int(changed_content.get(unrelated.chunk_id) != unrelated.content)
    unrelated_mutations += int(original_status.get(unrelated.chunk_id) != changed_status.get(unrelated.chunk_id))
    return {
        "governance_metadata_embedding_violations": metadata_embedding_violations,
        "governance_level_usage_violations": level_violations,
        "unrelated_unit_mutations": unrelated_mutations,
    }


def _temporal_binding_violations(units: list[EvidenceUnitFixture]) -> int:
    violations = 0
    for unit in units:
        for binding in unit.temporal_bindings:
            violations += int(getattr(unit, binding.field) != binding.value)
            if binding.source_kind == "document":
                violations += int(binding.source_span is not None)
                continue
            span = binding.source_span
            valid = (
                span is not None
                and 0 <= span.start < span.end <= len(unit.document_content)
                and unit.document_content[span.start:span.end] == binding.anchor_text
            )
            if binding.source_kind == "content" and span is not None:
                valid = valid and unit.source_span[0] <= span.start < span.end <= unit.source_span[1]
            violations += int(not valid)
    return violations


def _partial_eligibility_violations(corpus: EvidenceUnitCorpus) -> int:
    units = {unit.chunk_id: unit for unit in corpus.units}
    violations = 0
    for probe in corpus.partial_probes:
        raw = {
            "a": probe.a,
            "b": probe.b,
            "relation": "explicit",
            "scope": "partial",
            "claim_a": probe.claim_a,
            "claim_b": probe.claim_b,
            "claim_a_span": probe.claim_a_span,
            "claim_b_span": probe.claim_b_span,
        }
        normalized = _validated_conflicts(
            [raw],
            {probe.a, probe.b},
            {probe.a: units[probe.a].content, probe.b: units[probe.b].content},
            {probe.a: units[probe.a].atomicity, probe.b: units[probe.b].atomicity},
        )
        violations += int(len(normalized) != 1)
        if normalized:
            violations += int(normalized[0].relation != probe.expected_relation)
            violations += int(normalized[0].scope != probe.expected_scope)
    return violations


def _fact_cluster_metrics(cases: list[FactClusterCase], token_counter: Callable[[str], int]) -> dict[str, int]:
    gate_mismatches = 0
    binding_violations = 0
    boundary_count = 0
    atomicity_matches = 0
    for case in cases:
        block = CandidateBlock(
            kind=case.kind,
            content=case.content,
            source_span=SourceSpan(start=0, end=len(case.content)),
            structural_index=0,
        )
        decision = evaluate_atomicity_gate(block, token_counter)
        gate_mismatches += int(decision.needs_semantic_split != case.expect_semantic_split)
        boundary_count += len(case.boundaries)
        atomicity_matches += sum(boundary.atomicity in ("atomic", "indivisible") for boundary in case.boundaries)
        for boundary in case.boundaries:
            for binding in boundary.temporal_bindings:
                span = binding.source_span
                valid = (
                    span is not None
                    and boundary.source_span[0] <= span.start < span.end <= boundary.source_span[1]
                    and case.content[span.start:span.end] == binding.anchor_text
                )
                binding_violations += int(not valid)
    return {
        "fact_cluster_cases": len(cases),
        "fact_cluster_boundary_matches": len(cases) - gate_mismatches,
        "fact_cluster_gate_mismatches": gate_mismatches,
        "atomicity_classification_matches": atomicity_matches,
        "fact_cluster_temporal_binding_violations": binding_violations,
    }


def evidence_integrity_metrics(
    corpus: EvidenceUnitCorpus,
    token_counter: Callable[[str], int],
    fact_cluster_cases: list[FactClusterCase] | None = None,
) -> dict:
    units = corpus.units
    token_counts = [token_counter(unit.content) for unit in units]
    metrics = {
        "unit_count": len(units),
        "identity_collisions": _identity_collisions(units),
        "source_overwrites": _source_overwrites(units),
        "overlap_violations": _overlaps(units),
        "hard_max_violations": sum(count > 600 for count in token_counts),
        "ideal_range_below": sum(count < 150 for count in token_counts),
        "ideal_range_within": sum(150 <= count <= 400 for count in token_counts),
        "ideal_range_above": sum(400 < count <= 600 for count in token_counts),
        "invalid_span_acceptances": _invalid_spans(units),
        "temporal_binding_violations": _temporal_binding_violations(units),
        "partial_eligibility_violations": _partial_eligibility_violations(corpus),
        "embedding_isolation_violations": sum(not retrieval_text_isolated(unit) for unit in units),
        "full_conflicts": sum(conflict.scope == "full" for conflict in corpus.conflicts),
        "partial_conflicts": sum(conflict.scope == "partial" for conflict in corpus.conflicts),
    }
    metrics.update(governance_isolation_metrics(corpus))
    metrics.update(_fact_cluster_metrics(fact_cluster_cases or [], token_counter))
    zero_keys = (
        "identity_collisions",
        "source_overwrites",
        "overlap_violations",
        "hard_max_violations",
        "invalid_span_acceptances",
        "temporal_binding_violations",
        "partial_eligibility_violations",
        "fact_cluster_gate_mismatches",
        "fact_cluster_temporal_binding_violations",
        "embedding_isolation_violations",
        "governance_metadata_embedding_violations",
        "governance_level_usage_violations",
        "unrelated_unit_mutations",
    )
    metrics["passed"] = all(metrics[key] == 0 for key in zero_keys)
    return metrics
