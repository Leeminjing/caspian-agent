"""
本文件对外提供文档级与原子知识级 Evidence Unit 入库编排。

对外提供:
    put_document — 把带来源的 Markdown/纯文本文档切分、验证、逐单元评级并批量写入
    put_atomic_knowledge — 把调用方确认的单条原子知识写入统一 Evidence Unit 模型
    resolve_level_for_content — 对单个证据执行 L0 短路、软评级和确定性等级映射

输入:
    BaseStore、user_id、DocumentInput 或原子正文、可选模型/TokenCounter/segmenter/域策略。

输出:
    put_document 返回 DocumentIngestionResult；put_atomic_knowledge 返回 (chunk_id, level)。

具体工作流:
    文档路径依次完成身份、结构候选、原子闸门/模型 spans、整份修订机械验证、检索文本、
    独立评级、统一批量写入；任何结构/span/长度错误都发生在首次 Store 写入之前。

示例:
    result = await put_document(store, "u1", DocumentInput(content="事实。", source="官方"))
    key, level = await put_atomic_knowledge(store, "u1", "事实。", source="官方")
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timezone

from langgraph.store.base import BaseStore

from caspian.knowledge.chunking import (
    HARD_TOKEN_MAX,
    TokenCounter,
    needs_semantic_split,
    split_structural_blocks,
    to_absolute_blocks,
    validate_ordered_non_overlapping,
)
from caspian.knowledge.evidence import (
    CandidateBlock,
    DocumentIngestionResult,
    DocumentInput,
    EvidenceUnit,
    EvidenceUnitDraft,
    EvidenceValidationError,
    SourceSpan,
)
from caspian.knowledge.identity import (
    canonical_source,
    content_hash,
    make_chunk_id,
    make_document_id,
    make_revision_id,
)
from caspian.knowledge.provenance import classify_level
from caspian.knowledge.rating import decide_level, rate_level
from caspian.knowledge.retrieval_text import build_retrieval_text
from caspian.knowledge.segmentation import SemanticSpanSegmenter
from caspian.models import create_chat_model

logger = logging.getLogger(__name__)


def _load_domains(domains: Mapping[str, int] | None) -> Mapping[str, int]:
    if domains is not None:
        return domains
    try:
        from caspian.config import get_app_config
        return get_app_config("config.yaml").knowledge.level_policy.domains
    except Exception:
        logger.warning("加载 knowledge.level_policy 失败，按空策略处理", exc_info=True)
        return {}


def _load_rating_config():
    try:
        from caspian.config import get_app_config
        return get_app_config("config.yaml").knowledge.rating
    except Exception:
        logger.warning("加载 knowledge.rating 失败，回退默认", exc_info=True)
        from caspian.config.knowledge_config import RatingConfig
        return RatingConfig()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rated_by(rating_cfg, model=None) -> str:
    if rating_cfg is not None and rating_cfg.model:
        return rating_cfg.model
    return getattr(model, "model_name", None) or getattr(model, "model", None) or "default"


def _blacklist_basis() -> tuple[int, dict]:
    return 0, {
        "rated_by": "mechanical",
        "rated_at": _now_iso(),
        "confidence": None,
        "reason": "domain blacklist",
        "claim_domain": "",
        "dimensions": None,
        "mapping_rule": "domain blacklist → L0",
        "unrated_reason": None,
    }


def _disabled_basis() -> dict:
    return {
        "rated_by": "mechanical",
        "rated_at": _now_iso(),
        "confidence": None,
        "reason": "rating disabled",
        "claim_domain": "",
        "dimensions": None,
        "mapping_rule": "rating disabled → unrated",
        "unrated_reason": None,
    }


async def _rate_entry(content, source, source_url, mechanical, rating_cfg, model):
    try:
        rating = await rate_level(
            content,
            source,
            source_url,
            mechanical,
            model,
            timeout_seconds=rating_cfg.timeout_seconds,
        )
        level, rule = decide_level(
            rating,
            confidence_threshold=rating_cfg.confidence_threshold,
        )
        return level, {
            "rated_by": _rated_by(rating_cfg, model),
            "rated_at": _now_iso(),
            "confidence": rating.confidence,
            "reason": rating.reason,
            "claim_domain": rating.claim_domain,
            "dimensions": rating.dimensions.model_dump(),
            "mapping_rule": rule,
            "unrated_reason": rating.unrated_reason,
        }
    except Exception as exc:
        logger.error("知识评级失败，降级未评级: %s", exc, exc_info=True)
        return None, {
            "rated_by": _rated_by(rating_cfg, model),
            "rated_at": _now_iso(),
            "confidence": None,
            "reason": "rater failed",
            "claim_domain": "",
            "dimensions": None,
            "mapping_rule": "rater failed → unrated",
            "unrated_reason": str(exc),
        }


async def resolve_level_for_content(
    content: str,
    source: str,
    source_url: str | None,
    *,
    domains: Mapping[str, int] | None = None,
    model=None,
    rating_cfg=None,
) -> tuple[int | None, dict, dict]:
    policy = _load_domains(domains)
    mech_level, source_type, matched_domain = classify_level(source_url, policy)
    mechanical = {"source_type": source_type, "matched_domain": matched_domain}
    cfg = rating_cfg or _load_rating_config()
    if mech_level == 0:
        level, basis = _blacklist_basis()
        return level, basis, mechanical
    if not cfg.enabled:
        return None, _disabled_basis(), mechanical
    rating_model = model or create_chat_model(cfg.model)
    level, basis = await _rate_entry(content, source, source_url, mechanical, cfg, rating_model)
    return level, basis, mechanical


def _token_counter(model, token_counter: TokenCounter | None) -> TokenCounter:
    if token_counter is not None:
        return token_counter
    counter = getattr(model, "get_num_tokens", None)
    if not callable(counter):
        raise EvidenceValidationError("token_counter_unavailable", "当前模型不提供 get_num_tokens；请注入 TokenCounter")
    return counter


def _identities(document: DocumentInput, *, allow_manual: bool) -> tuple[str, str]:
    document_hash = content_hash(document.content)
    source_identity = canonical_source(
        external_source_id=document.external_source_id,
        source_url=document.source_url,
        source=document.source,
        manual_content_hash=document_hash if allow_manual else None,
    )
    document_id = make_document_id(source_identity)
    revision_id = make_revision_id(
        document_id,
        document_hash,
        version=document.version,
        published_at=document.published_at,
        effective_at=document.effective_at,
    )
    return document_id, revision_id


def _drafts(
    document: DocumentInput,
    blocks: list[CandidateBlock],
    document_id: str,
    revision_id: str,
) -> list[EvidenceUnitDraft]:
    drafts: list[EvidenceUnitDraft] = []
    for chunk_index, block in enumerate(blocks):
        retrieval_text = build_retrieval_text(
            block.content,
            title=document.title,
            section_path=block.section_path,
            version=document.version,
            published_at=document.published_at,
            effective_at=document.effective_at,
        )
        drafts.append(
            EvidenceUnitDraft(
                chunk_id=make_chunk_id(
                    revision_id,
                    block.source_span,
                    content_hash(block.content),
                ),
                document_id=document_id,
                document_revision_id=revision_id,
                content=block.content,
                retrieval_text=retrieval_text,
                title=document.title,
                section_path=block.section_path,
                chunk_index=chunk_index,
                source_span=block.source_span,
                source=document.source,
                source_url=document.source_url,
                published_at=document.published_at,
                effective_at=document.effective_at,
                version=document.version,
            )
        )
    return drafts


async def _rate_drafts(document, drafts, domains, model, rating_cfg) -> list[EvidenceUnit]:
    units: list[EvidenceUnit] = []
    for draft in drafts:
        level, level_basis, provenance = await resolve_level_for_content(
            draft.content,
            document.source,
            document.source_url,
            domains=domains,
            model=model,
            rating_cfg=rating_cfg,
        )
        units.append(
            EvidenceUnit(
                **draft.model_dump(),
                level=level,
                level_basis=level_basis,
                provenance=provenance,
            )
        )
    return units


async def _persist(store, user_id: str, units: list[EvidenceUnit]) -> None:
    from caspian.knowledge.store_client import put_evidence_units
    await put_evidence_units(store, user_id, units)


async def put_document(
    store: BaseStore,
    user_id: str,
    document: DocumentInput,
    *,
    domains: Mapping[str, int] | None = None,
    model=None,
    token_counter: TokenCounter | None = None,
    segmenter: SemanticSpanSegmenter | None = None,
    rating_cfg=None,
) -> DocumentIngestionResult:
    rating_cfg = rating_cfg or _load_rating_config()
    document_id, revision_id = _identities(document, allow_manual=False)
    working_model = model or create_chat_model(rating_cfg.model)
    count_tokens = _token_counter(working_model, token_counter)
    candidates = split_structural_blocks(document.content, document.format)
    units_as_blocks: list[CandidateBlock] = []
    semantic_segmenter = segmenter or SemanticSpanSegmenter(working_model)
    for candidate in candidates:
        if needs_semantic_split(candidate, count_tokens):
            spans = await semantic_segmenter.split(candidate)
            units_as_blocks.extend(
                to_absolute_blocks(document.content, candidate, spans, count_tokens)
            )
        else:
            units_as_blocks.append(candidate)
    units_as_blocks.sort(key=lambda item: item.source_span.start)
    validate_ordered_non_overlapping(units_as_blocks, count_tokens)
    drafts = _drafts(document, units_as_blocks, document_id, revision_id)
    units = await _rate_drafts(document, drafts, domains, working_model, rating_cfg)
    await _persist(store, user_id, units)
    return DocumentIngestionResult(
        document_id=document_id,
        document_revision_id=revision_id,
        count=len(units),
        chunk_ids=tuple(unit.chunk_id for unit in units),
    )


async def put_atomic_knowledge(
    store: BaseStore,
    user_id: str,
    content: str,
    *,
    source: str = "",
    source_url: str | None = None,
    domains: Mapping[str, int] | None = None,
    model=None,
    token_counter: TokenCounter | None = None,
    rating_cfg=None,
) -> tuple[str, int | None]:
    document = DocumentInput(
        content=str(content),
        format="text",
        source=source,
        source_url=source_url,
    )
    rating_cfg = rating_cfg or _load_rating_config()
    working_model = model or create_chat_model(rating_cfg.model)
    count_tokens = _token_counter(working_model, token_counter)
    if count_tokens(document.content) > HARD_TOKEN_MAX:
        raise EvidenceValidationError("hard_max_exceeded", "原子知识超过 600 tokens，请改用 POST /api/knowledge/documents")
    document_id, revision_id = _identities(document, allow_manual=True)
    span = SourceSpan(start=0, end=len(document.content))
    block = CandidateBlock(kind="paragraph", content=document.content, source_span=span, structural_index=0)
    draft = _drafts(document, [block], document_id, revision_id)[0]
    unit = (await _rate_drafts(document, [draft], domains, working_model, rating_cfg))[0]
    await _persist(store, user_id, [unit])
    return unit.chunk_id, unit.level
