"""
本文件对外提供知识库在 LangGraph Store 上的薄封装：入库、读取、列表、改等级、向量检索。

对外提供:
    put_knowledge — 入库一条知识（带离散权威等级），返回条目 key
    list_knowledge — 列出当前用户全部条目（updated_at 倒序）
    update_level — 修改条目等级（put 同 key，内容不变）
    search_knowledge — 向量语义召回 top_k 条候选证据

输入:
    store: BaseStore — LangGraph Store 实例（app.state.store 或 runtime.store）
    user_id: str — 用户标识，参与 namespace 隔离
    content / level / source / source_url — 条目字段（level 允许 0-3 或 None）
    query: str — 自然语言查询文本（store index 负责嵌入与相似度排序）
    limit: int — 召回条数

输出:
    put_knowledge → str（条目 key，uuid4().hex）
    list_knowledge → list[Item]
    update_level → bool（False 表示条目不存在）
    search_knowledge → list[EvidenceEntry]（携带 score）

具体工作流:
    (1) namespace 统一为 ("knowledge", user_id)，按用户隔离
    (2) put 时校验 level ∈ {0,1,2,3,None}，content 非空
    (3) update_level 先 get 原 value，仅替换 level 字段后 put 回同 key
    (4) search 直接委托 store.search(query=...)，score 取自 Item.score

示例:
    key = await put_knowledge(store, "u1", "功能 A 已废弃。", level=3, source="官方文档")
    entries = await search_knowledge(store, "u1", "A 是否可用", limit=5)
"""

import logging
import uuid

from langgraph.store.base import BaseStore, Item

from caspian.knowledge.schemas import EvidenceEntry

logger = logging.getLogger(__name__)

_LEVELS: frozenset = frozenset({0, 1, 2, 3})

_SEARCH_LIMIT_MIN = 1
_SEARCH_LIMIT_MAX = 20


def _namespace(user_id: str) -> tuple[str, str]:
    return ("knowledge", str(user_id))


def _validate_level(level: int | None) -> None:
    if level is not None and level not in _LEVELS:
        raise ValueError(f"非法等级: {level}，允许 0-3 或 null（未评级）")


def _clamp_limit(limit: int) -> int:
    return max(_SEARCH_LIMIT_MIN, min(_SEARCH_LIMIT_MAX, int(limit)))


async def put_knowledge(
    store: BaseStore,
    user_id: str,
    content: str,
    level: int | None = None,
    source: str = "",
    source_url: str | None = None,
) -> str:
    content = str(content).strip()
    if not content:
        raise ValueError("content 不能为空")
    _validate_level(level)
    key = uuid.uuid4().hex
    await store.aput(
        _namespace(user_id),
        key,
        {
            "content": content,
            "level": level,
            "source": str(source or ""),
            "source_url": source_url,
        },
    )
    logger.info("知识条目已入库 key=%s level=%s", key, level)
    return key


_LIST_LIMIT_MAX = 500


async def list_knowledge(store: BaseStore, user_id: str) -> list[Item]:
    # ponytail: 全量列表按 updated_at 倒序（store 无 query 时的默认序），
    # 上限 500 条；条目超过上限再做分页
    return list(await store.asearch(_namespace(user_id), limit=_LIST_LIMIT_MAX))


async def update_level(store: BaseStore, user_id: str, key: str, level: int | None) -> bool:
    _validate_level(level)
    item = await store.aget(_namespace(user_id), key)
    if item is None:
        return False
    value = dict(item.value)
    value["level"] = level
    await store.aput(_namespace(user_id), key, value)
    logger.info("知识条目等级已修改 key=%s level=%s", key, level)
    return True


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
