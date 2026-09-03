"""
本文件对外提供知识库在 LangGraph Store 上的薄封装：入库、列表、改来源、向量检索。

对外提供:
    put_knowledge — 入库一条知识（等级由来源归属派生），key 为内容哈希
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
    (2) put 时由 classify_level(source_url) 派生 level，存 provenance，key=sha256(content)[:16]（同内容 upsert）
    (3) update_provenance 先 aget 原值，expected_level 与当前不符则返回 CONFLICT；否则改来源/覆盖等级后 aput
    (4) search 委托 store.asearch(query=...)，score 取自 Item.score

示例:
    key = await put_knowledge(store, "u1", "功能 A 已废弃。", source="官方文档", source_url="https://docs.example.com/x")
    status = await update_provenance(store, "u1", key, source_url="https://blog.example.com/x", expected_level=3)
"""

import hashlib
import logging
from collections.abc import Mapping
from enum import Enum

from langgraph.store.base import BaseStore, Item

from caspian.knowledge.provenance import classify_level
from caspian.knowledge.schemas import EvidenceEntry

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


async def put_knowledge(
    store: BaseStore,
    user_id: str,
    content: str,
    source: str = "",
    source_url: str | None = None,
    domains: Mapping[str, int] | None = None,
) -> tuple[str, int | None]:
    content = str(content).strip()
    if not content:
        raise ValueError("content 不能为空")
    policy = _load_domains(domains)
    level, source_type, matched_domain = classify_level(source_url, policy)
    key = _content_key(content)
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
        value["provenance"] = {
            "source_type": "override",
            "matched_domain": None,
        }
    elif source_url is not None:
        policy = _load_domains(domains)
        level, source_type, matched_domain = classify_level(source_url, policy)
        value["level"] = level
        value["source_url"] = source_url
        value["provenance"] = {
            "source_type": source_type,
            "matched_domain": matched_domain,
        }
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
