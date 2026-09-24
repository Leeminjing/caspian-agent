"""
本文件对外提供 Evidence Unit 的 LangGraph Store 持久化、兼容读取与原子知识入口。

对外提供:
    put_evidence_units — 以 retrieval_text 字段索引批量 upsert 已验证的证据单元
    put_knowledge — 保持旧签名和 (id, level) 返回形状的原子知识薄兼容层
    list_knowledge — 分页列出当前用户条目
    evidence_from_item — 把新旧 Store Item 集中投影为 EvidenceEntry
    update_provenance — 带 CAS 更新等级/legacy 来源且不重算未变化的 retrieval_text 向量
    search_knowledge — 召回并投影 Evidence Unit
    ProvenanceUpdateStatus — 更新结果状态

输入:
    BaseStore、user_id、EvidenceUnit 或原子 content、来源字段及 CAS 参数。

输出:
    Store 写入结果、Item 列表、EvidenceEntry 列表或 ProvenanceUpdateStatus。

具体工作流:
    namespace 固定为 ("knowledge", user_id)；新记录用 PutOp(index=["retrieval_text"])
    批量写入；读取统一适配 schema v2 与 legacy；仅治理字段更新使用 index=False 保留向量；
    新格式来源绑定身份且不可原地改写，legacy 仍允许补充来源并重新评级。

示例:
    key, level = await put_knowledge(store, "u1", "功能 A 已废弃。", source="官方")
    entries = await search_knowledge(store, "u1", "功能 A", 5)
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum

from langgraph.store.base import BaseStore, Item, PutOp

from caspian.knowledge.evidence import EvidencePersistenceError, EvidenceUnit, SourceSpan
from caspian.knowledge.schemas import EvidenceEntry

_LEVELS: frozenset = frozenset({0, 1, 2, 3})
_SEARCH_LIMIT_MIN = 1
_SEARCH_LIMIT_MAX = 20
_LIST_LIMIT_MAX = 500


class ProvenanceUpdateStatus(Enum):
    OK = "ok"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"


def _namespace(user_id: str) -> tuple[str, str]:
    return ("knowledge", str(user_id))


def _validate_level(level: int | None) -> None:
    if level is not None and level not in _LEVELS:
        raise ValueError(f"非法等级: {level}，允许 0-3 或 null（未评级）")


def _clamp_limit(limit: int) -> int:
    return max(_SEARCH_LIMIT_MIN, min(_SEARCH_LIMIT_MAX, int(limit)))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_rating_config():
    from caspian.knowledge.ingestion import _load_rating_config as load_rating_config

    return load_rating_config()


async def _put_sequentially(store, namespace, units: Sequence[EvidenceUnit]) -> tuple[str, ...]:
    completed: list[str] = []
    for unit in units:
        try:
            await store.aput(namespace, unit.chunk_id, unit.to_store_value(), index=["retrieval_text"])
        except TypeError:
            await store.aput(namespace, unit.chunk_id, unit.to_store_value())
        completed.append(unit.chunk_id)
    return tuple(completed)


async def put_evidence_units(store: BaseStore, user_id: str, units: Sequence[EvidenceUnit]) -> None:
    if not units:
        return
    namespace = _namespace(user_id)
    operations = [PutOp(namespace, unit.chunk_id, unit.to_store_value(), index=["retrieval_text"]) for unit in units]
    completed_ids: tuple[str, ...] = ()
    try:
        if hasattr(store, "abatch"):
            await store.abatch(operations)
        else:
            completed_ids = await _put_sequentially(store, namespace, units)
    except Exception as exc:
        pending = tuple(unit.chunk_id for unit in units if unit.chunk_id not in completed_ids)
        raise EvidencePersistenceError(
            "Evidence Unit 批量写入失败",
            completed_ids=completed_ids,
            pending_ids=pending,
        ) from exc


async def put_knowledge(
    store: BaseStore,
    user_id: str,
    content: str,
    source: str = "",
    source_url: str | None = None,
    domains: Mapping[str, int] | None = None,
    model=None,
) -> tuple[str, int | None]:
    from caspian.knowledge.ingestion import put_atomic_knowledge

    return await put_atomic_knowledge(
        store,
        user_id,
        content,
        source=source,
        source_url=source_url,
        domains=domains,
        model=model,
        rating_cfg=_load_rating_config(),
    )


async def list_knowledge(store: BaseStore, user_id: str, limit: int = _LIST_LIMIT_MAX, offset: int = 0) -> list[Item]:
    return list(await store.asearch(_namespace(user_id), limit=limit, offset=offset))


def evidence_from_item(item: Item) -> EvidenceEntry:
    value = item.value or {}
    span_value = value.get("source_span")
    span = SourceSpan.model_validate(span_value) if isinstance(span_value, dict) else None
    is_new = value.get("record_type") == "evidence_unit"
    return EvidenceEntry(
        id=str(item.key),
        content=str(value.get("content", "")),
        level=value.get("level"),
        score=getattr(item, "score", None),
        source=str(value.get("source", "")),
        source_url=value.get("source_url"),
        chunk_id=value.get("chunk_id") if is_new else None,
        document_id=value.get("document_id") if is_new else None,
        document_revision_id=value.get("document_revision_id") if is_new else None,
        title=str(value.get("title", "")) if is_new else "",
        section_path=tuple(value.get("section_path") or ()) if is_new else (),
        chunk_index=value.get("chunk_index") if is_new else None,
        source_span=span,
        version=value.get("version") if is_new else None,
        published_at=value.get("published_at") if is_new else None,
        effective_at=value.get("effective_at") if is_new else None,
        legacy=not is_new,
    )


def _override_value(value: dict, level_override: int) -> None:
    value["level"] = level_override
    value["provenance"] = {"source_type": "override", "matched_domain": None}
    value["level_basis"] = {
        "rated_by": "manual",
        "rated_at": _now_iso(),
        "confidence": None,
        "reason": "level_override",
        "claim_domain": "",
        "dimensions": None,
        "mapping_rule": "level_override",
        "unrated_reason": None,
    }


def _validate_source_mutation(value: dict, source_url: str | None) -> None:
    if source_url is not None and value.get("record_type") == "evidence_unit":
        raise ValueError("Evidence Unit 的来源身份不可原地修改，请以新来源重新入库")


async def _rerate_value(value: dict, source_url: str, domains, model) -> None:
    from caspian.knowledge.ingestion import resolve_level_for_content

    level, basis, provenance = await resolve_level_for_content(
        str(value.get("content", "")),
        str(value.get("source", "")),
        source_url,
        domains=domains,
        model=model,
        rating_cfg=_load_rating_config(),
    )
    value["level"] = level
    value["source_url"] = source_url
    value["provenance"] = provenance
    value["level_basis"] = basis


async def update_provenance(
    store: BaseStore,
    user_id: str,
    key: str,
    *,
    source_url: str | None = None,
    level_override: int | None = None,
    expected_level: int | None = None,
    domains: Mapping[str, int] | None = None,
    model=None,
) -> ProvenanceUpdateStatus:
    _validate_level(level_override)
    item = await store.aget(_namespace(user_id), key)
    if item is None:
        return ProvenanceUpdateStatus.NOT_FOUND
    value = dict(item.value)
    if expected_level is not None and value.get("level") != expected_level:
        return ProvenanceUpdateStatus.CONFLICT
    _validate_source_mutation(value, source_url)
    if level_override is not None:
        _override_value(value, level_override)
    elif source_url is not None:
        await _rerate_value(value, source_url, domains, model)
    else:
        raise ValueError("update_provenance 需提供 source_url 或 level_override 之一")
    try:
        await store.aput(_namespace(user_id), key, value, index=False)
    except TypeError:
        await store.aput(_namespace(user_id), key, value)
    return ProvenanceUpdateStatus.OK


async def search_knowledge(store: BaseStore, user_id: str, query: str, limit: int = 5) -> list[EvidenceEntry]:
    items = await store.asearch(_namespace(user_id), query=str(query), limit=_clamp_limit(limit))
    return [evidence_from_item(item) for item in items]
