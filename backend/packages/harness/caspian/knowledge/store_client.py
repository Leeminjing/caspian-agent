"""
本文件对外提供知识库在 LangGraph Store 上的薄封装：入库、列表、改来源、向量检索。

对外提供:
    put_knowledge — 入库一条知识（等级由软评级 + 硬映射产生），key 为内容哈希
    list_knowledge — 分页列出当前用户全部条目（updated_at 倒序）
    update_provenance — 带 CAS 修改条目来源归属/等级
    search_knowledge — 向量语义召回 top_k 条候选证据
    ProvenanceUpdateStatus — 修改来源归属的结果状态枚举

输入:
    store: BaseStore — LangGraph Store 实例
    user_id: str — 用户标识，参与 namespace 隔离
    content / source / source_url — 条目字段
    domains: Mapping[str, int] | None — 域名→等级策略表，None 时从 config.yaml 加载
    limit / offset — 列表分页参数
    expected_level: int | None — CAS 期望等级

输出:
    put_knowledge → tuple[str, int | None]（条目 key = sha256(content)[:16]，派生 level）
    list_knowledge → list[Item]
    update_provenance → ProvenanceUpdateStatus
    search_knowledge → list[EvidenceEntry]（携带 score）

具体工作流:
    (1) namespace 统一为 ("knowledge", user_id)，按用户隔离
    (2) put 时由 classify_level(source_url) 提供机械特征与 L0 黑名单，软评级 + 硬映射产生 level，存 provenance 与 level_basis，key=sha256(content)[:16]（同内容 upsert）
    (3) update_provenance 先 aget 原值，expected_level 与当前不符则返回 CONFLICT；否则改来源/覆盖等级后 aput
    (4) search 委托 store.asearch(query=...)，score 取自 Item.score

示例:
    key = await put_knowledge(store, "u1", "功能 A 已废弃。", source="官方文档", source_url="https://docs.example.com/x")
    status = await update_provenance(store, "u1", key, source_url="https://blog.example.com/x", expected_level=3)
"""

import hashlib
import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum

from langgraph.store.base import BaseStore, Item

from caspian.knowledge.provenance import classify_level
from caspian.knowledge.rating import decide_level, rate_level
from caspian.knowledge.schemas import EvidenceEntry
from caspian.models import create_chat_model

logger = logging.getLogger(__name__)

_LEVELS: frozenset = frozenset({0, 1, 2, 3})

_SEARCH_LIMIT_MIN = 1
_SEARCH_LIMIT_MAX = 20

_LIST_LIMIT_MAX = 500


class ProvenanceUpdateStatus(Enum):
    """update_provenance 的结果状态。"""

    OK = "ok"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"


def _namespace(user_id: str) -> tuple[str, str]:
    return ("knowledge", str(user_id))


def _validate_level(level: int | None) -> None:
    if level is not None and level not in _LEVELS:
        raise ValueError(f"非法等级: {level}，允许 0-3 或 null（未评级）")


def _clamp_limit(limit: int) -> int:
    clamped = max(_SEARCH_LIMIT_MIN, min(_SEARCH_LIMIT_MAX, int(limit)))
    if clamped != int(limit):
        logger.warning("search_knowledge top_k 被截断: 请求 %s → 应用 %s", limit, clamped)
    return clamped


def _content_key(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _load_domains(domains: Mapping[str, int] | None) -> Mapping[str, int]:
    if domains is not None:
        return domains
    try:
        from caspian.config import get_app_config

        cfg = get_app_config("config.yaml")
        knowledge = getattr(cfg, "knowledge", None)
        if knowledge is not None:
            return knowledge.level_policy.domains
    except Exception:
        logger.warning("加载 knowledge.level_policy 失败，按空策略处理", exc_info=True)
    return {}


def _load_rating_config():
    """加载 knowledge.rating 配置；失败回退默认（enabled=true, model=None）。"""
    try:
        from caspian.config import get_app_config

        cfg = get_app_config("config.yaml")
        knowledge = getattr(cfg, "knowledge", None)
        if knowledge is not None:
            return knowledge.rating
    except Exception:
        logger.warning("加载 knowledge.rating 失败，回退默认", exc_info=True)
    from caspian.config.knowledge_config import RatingConfig

    return RatingConfig()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rated_by(rating_cfg, model=None) -> str:
    if rating_cfg is not None and rating_cfg.model:
        return rating_cfg.model
    return getattr(model, "model_name", None) or "default"


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


async def _rate_entry(
    content: str,
    source: str,
    source_url: str | None,
    mechanical: dict,
    rating_cfg,
    model=None,
) -> tuple[int | None, dict]:
    """软评级 + 硬映射，失败降级未评级。返回 (level, level_basis)。"""
    try:
        if model is None:
            model = create_chat_model(rating_cfg.model)
        rating = await rate_level(
            content,
            source,
            source_url,
            mechanical,
            model,
            timeout_seconds=rating_cfg.timeout_seconds,
        )
        level, rule = decide_level(
            rating, confidence_threshold=rating_cfg.confidence_threshold
        )
        basis = {
            "rated_by": _rated_by(rating_cfg, model),
            "rated_at": _now_iso(),
            "confidence": rating.confidence,
            "reason": rating.reason,
            "claim_domain": rating.claim_domain,
            "dimensions": rating.dimensions.model_dump(),
            "mapping_rule": rule,
            "unrated_reason": rating.unrated_reason,
        }
        return level, basis
    except Exception as exc:
        logger.error("知识评级失败，降级未评级: %s", exc, exc_info=True)
        return None, {
            "rated_by": _rated_by(rating_cfg),
            "rated_at": _now_iso(),
            "confidence": None,
            "reason": "rater failed",
            "claim_domain": "",
            "dimensions": None,
            "mapping_rule": "rater failed → unrated",
            "unrated_reason": str(exc),
        }


async def _resolve_level(
    content: str,
    source: str,
    source_url: str | None,
    mechanical: dict,
    mech_level: int | None,
    rating_cfg,
    model=None,
) -> tuple[int | None, dict]:
    """统一入口：黑名单 → L0；评级关闭 → 未评级；否则软评级 + 硬映射。"""
    if mech_level == 0:
        return _blacklist_basis()
    if not rating_cfg.enabled:
        return None, _disabled_basis()
    return await _rate_entry(content, source, source_url, mechanical, rating_cfg, model)


async def put_knowledge(
    store: BaseStore,
    user_id: str,
    content: str,
    source: str = "",
    source_url: str | None = None,
    domains: Mapping[str, int] | None = None,
    model=None,
) -> tuple[str, int | None]:
    content = str(content).strip()
    if not content:
        raise ValueError("content 不能为空")
    policy = _load_domains(domains)
    mech_level, source_type, matched_domain = classify_level(source_url, policy)
    key = _content_key(content)
    mechanical = {"source_type": source_type, "matched_domain": matched_domain}
    rating_cfg = _load_rating_config()

    level, level_basis = await _resolve_level(
        content, source, source_url, mechanical, mech_level, rating_cfg, model
    )

    await store.aput(
        _namespace(user_id),
        key,
        {
            "content": content,
            "level": level,
            "source": str(source or ""),
            "source_url": source_url,
            "provenance": {
                "source_type": source_type,
                "matched_domain": matched_domain,
            },
            "level_basis": level_basis,
        },
    )
    logger.info("知识条目已入库 key=%s level=%s source_type=%s", key, level, source_type)
    return key, level


async def list_knowledge(
    store: BaseStore,
    user_id: str,
    limit: int = _LIST_LIMIT_MAX,
    offset: int = 0,
) -> list[Item]:
    return list(await store.asearch(_namespace(user_id), limit=limit, offset=offset))


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
    current_level = value.get("level")
    if expected_level is not None and current_level != expected_level:
        logger.warning(
            "update_provenance CAS 冲突 (key=%s, expected=%s, actual=%s)",
            key, expected_level, current_level,
        )
        return ProvenanceUpdateStatus.CONFLICT

    if level_override is not None:
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
    elif source_url is not None:
        policy = _load_domains(domains)
        mech_level, source_type, matched_domain = classify_level(source_url, policy)
        mechanical = {"source_type": source_type, "matched_domain": matched_domain}
        rating_cfg = _load_rating_config()
        content = str(value.get("content", ""))
        source = str(value.get("source", ""))
        level, level_basis = await _resolve_level(
            content, source, source_url, mechanical, mech_level, rating_cfg, model
        )
        value["level"] = level
        value["source_url"] = source_url
        value["provenance"] = {"source_type": source_type, "matched_domain": matched_domain}
        value["level_basis"] = level_basis
    else:
        raise ValueError("update_provenance 需提供 source_url 或 level_override 之一")

    await store.aput(_namespace(user_id), key, value)
    logger.info("知识条目来源归属已更新 key=%s level=%s", key, value.get("level"))
    return ProvenanceUpdateStatus.OK


async def search_knowledge(
    store: BaseStore,
    user_id: str,
    query: str,
    limit: int = 5,
) -> list[EvidenceEntry]:
    items = await store.asearch(
        _namespace(user_id),
        query=str(query),
        limit=_clamp_limit(limit),
    )
    return [
        EvidenceEntry(
            id=str(item.key),
            content=str(item.value.get("content", "")),
            level=item.value.get("level"),
            score=getattr(item, "score", None),
            source=str(item.value.get("source", "")),
            source_url=item.value.get("source_url"),
        )
        for item in items
    ]
