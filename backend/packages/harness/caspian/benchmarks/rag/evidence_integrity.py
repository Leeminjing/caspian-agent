"""
本文件对外提供 Evidence Unit benchmark 的机械完整性计数与检索字段隔离复验。

对外提供:
    evidence_integrity_metrics — 统计单元、身份碰撞、来源覆盖、overlap、hard max、非法 span、
    full/partial conflict、retrieval_text 隔离、等级使用与无关单元变更违规
    retrieval_text_isolated — 复算白名单检索文本并确认治理 metadata 不影响结果
    governance_isolation_metrics — 运行生产 govern，验证等级变化生效且无关单元不变

输入:
    EvidenceUnitCorpus 与确定性 TokenCounter。

输出:
    dict[str, int|bool]；身份碰撞、来源覆盖、overlap、hard max、非法 span 和检索隔离违规
    均为零时 passed=True。

具体工作流:
    对 fixture 做分组与原文切片检查，复算 retrieval_text，统计 conflict scope，再以声明的
    probe 运行生产 govern；不调用真实 embedding、LLM 或 Store，因此可在 CI 中稳定复验。

示例:
    metrics = evidence_integrity_metrics(corpus, lambda text: len(text.split()))
"""

from collections import defaultdict
from collections.abc import Callable
from dataclasses import replace

from caspian.benchmarks.rag.schema import EvidenceUnitCorpus, EvidenceUnitFixture
from caspian.knowledge.governance import govern
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


def evidence_integrity_metrics(corpus: EvidenceUnitCorpus, token_counter: Callable[[str], int]) -> dict:
    units = corpus.units
    metrics = {
        "unit_count": len(units),
        "identity_collisions": _identity_collisions(units),
        "source_overwrites": _source_overwrites(units),
        "overlap_violations": _overlaps(units),
        "hard_max_violations": sum(token_counter(unit.content) > 600 for unit in units),
        "invalid_span_acceptances": _invalid_spans(units),
        "embedding_isolation_violations": sum(not retrieval_text_isolated(unit) for unit in units),
        "full_conflicts": sum(conflict.scope == "full" for conflict in corpus.conflicts),
        "partial_conflicts": sum(conflict.scope == "partial" for conflict in corpus.conflicts),
    }
    metrics.update(governance_isolation_metrics(corpus))
    zero_keys = (
        "identity_collisions",
        "source_overwrites",
        "overlap_violations",
        "hard_max_violations",
        "invalid_span_acceptances",
        "embedding_isolation_violations",
        "governance_metadata_embedding_violations",
        "governance_level_usage_violations",
        "unrelated_unit_mutations",
    )
    metrics["passed"] = all(metrics[key] == 0 for key in zero_keys)
    return metrics
