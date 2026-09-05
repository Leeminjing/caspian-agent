"""机械 oracle:错误信息采纳率 / 正确信息保留率。"""

from __future__ import annotations

from caspian.benchmarks.rag.schema import RagItem


def wrong_info_adopted(item: RagItem, final_ids: set[str]) -> bool:
    """最终证据集是否仍含 ground_truth 以外的冲突方(错误信息)。"""
    wrong = {c.id for c in item.candidates if c.id != item.ground_truth}
    return bool(wrong & set(final_ids))


def correct_info_retained(item: RagItem, final_ids: set[str]) -> bool:
    """最终证据集是否仍含 ground_truth(正确信息)。"""
    return item.ground_truth in set(final_ids)
