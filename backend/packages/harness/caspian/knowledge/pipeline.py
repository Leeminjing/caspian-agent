"""
本文件对外提供 run_governed_query 异步函数，作为知识治理查询三段管线的唯一共享实现。

对外提供:
    run_governed_query — 按"向量召回 → judge 冲突判定 → govern 等级治理"顺序执行查询，
        供内置工具 knowledge_query 与 REST /api/knowledge/query 共用

输入:
    store: BaseStore — LangGraph Store 实例
    user_id: str — 用户标识，参与 namespace 隔离
    query: str — 自然语言查询文本
    top_k: int — 向量召回条数（内部钳制 [1,20]）
    model_name: str | None — judge 使用的模型名，None 时取默认模型

输出:
    tuple[GovernanceResult | None, list[EvidenceEntry] | None, str | None] —
        (治理结果, 召回候选, 错误信息)；错误信息非空表示 judge 失败、治理未执行，
        错误语义由调用层决定（内置工具返回错误文本，REST 返回 502）

具体工作流:
    (1) search_knowledge 向量召回候选（等级不参与召回）
    (2) 候选为空 → 返回 (None, [], None)
    (3) judge_conflicts 单次批量冲突判定；失败 → 记录日志并返回 (None, candidates, 错误类型名)
    (4) govern 确定性等级治理 → 返回 (result, candidates, None)

示例:
    result, candidates, error = await run_governed_query(
        store, "u1", "功能 A 是否可用", 5, None)
"""

import logging

from langgraph.store.base import BaseStore

from caspian.knowledge.governance import govern
from caspian.knowledge.judge import judge_conflicts
from caspian.knowledge.schemas import EvidenceEntry, GovernanceResult
from caspian.knowledge.store_client import search_knowledge
from caspian.models import create_chat_model

logger = logging.getLogger(__name__)


async def run_governed_query(
    store: BaseStore,
    user_id: str,
    query: str,
    top_k: int = 5,
    model_name: str | None = None,
) -> tuple[GovernanceResult | None, list[EvidenceEntry] | None, str | None]:
    candidates = await search_knowledge(store, user_id, query, top_k)
    if not candidates:
        return None, [], None

    model = create_chat_model(model_name or None)
    try:
        conflicts = await judge_conflicts(candidates, model, query=query)
    except Exception as exc:
        logger.error("知识检索治理 judge 失败: %s", exc, exc_info=True)
        return None, candidates, type(exc).__name__

    return govern(candidates, conflicts), candidates, None
