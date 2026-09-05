"""benchmark 统计:Wilson 二项置信区间与均值±标准误。"""

from __future__ import annotations

import math


def wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """二项比例的 Wilson 95% 置信区间(默认 z=1.96)。

    输入:
        k: int — 成功次数
        n: int — 总次数
    输出:
        tuple[float, float] — (下界, 上界);n=0 返回 (0.0, 0.0)
    """
    if n <= 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def mean_sem(values: list[float]) -> tuple[float, float]:
    """均值与均值标准误。

    输出:
        tuple[float, float] — (均值, 标准误);空列表返回 (0.0, 0.0)
    """
    n = len(values)
    if n == 0:
        return (0.0, 0.0)
    mean = sum(values) / n
    if n == 1:
        return (mean, 0.0)
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    return (mean, math.sqrt(variance / n))
