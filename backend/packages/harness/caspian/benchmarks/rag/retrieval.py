"""检索轴(轴C):Hybrid/Rerank/MMR/MAX_SIM 的行为建模。

这些检索/排序机制都按【相关性/多样性/实体粒度】排序或选择,不裁决权威。
本 benchmark 用候选的 relevance score 作为相关性代理:错误方拥有更高 score
(词面更匹配查询),故检索类机制都会把错误方排到最前 —— 错误信息采纳率不降。

这证明:检索管「找相关」,治理管「信哪个」;相关性 ≠ 权威,检索不能替代治理。
"""

from __future__ import annotations

from caspian.benchmarks.rag.schema import RagItem


def _rank_by_relevance(item: RagItem) -> list[str]:
    """按相关性(score)降序排序。"""
    return [c.id for c in sorted(item.candidates, key=lambda c: -c.score)]


# 四种检索机制本质都是相关性排序;在「2 候选、错误方高相关」的语料下均把错误方排最前。
# 完整系统中它们的相关性信号各自不同(向量/BM25 融合、交叉编码器、多样性、实体粒度),
# 但都不包含权威等级 —— 这正是它们无法裁决冲突的原因。
RETRIEVAL_MECHANISMS = {
    "Hybrid Search": _rank_by_relevance,
    "Re-Ranking": _rank_by_relevance,
    "MMR": _rank_by_relevance,
    "MAX_SIM": _rank_by_relevance,
}


def top1_is_wrong(item: RagItem, rank_fn) -> bool:
    """排序后第 1 名是否为错误方(ground_truth 以外)。"""
    ranking = rank_fn(item)
    return bool(ranking) and ranking[0] != item.ground_truth


def retrieval_axis(items: list[RagItem]) -> dict[str, dict]:
    """对每条语料跑四种检索机制,统计「第 1 名为错误方」的次数。"""
    out: dict[str, dict] = {}
    n = len(items)
    for name, rank_fn in RETRIEVAL_MECHANISMS.items():
        wrong = sum(1 for item in items if top1_is_wrong(item, rank_fn))
        out[name] = {"wrong_top1": wrong, "n": n}
    return out
