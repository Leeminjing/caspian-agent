"""承诺层 benchmark 编排:机械测试(1/2/3/2b)+ 软 baseline(4)。"""

from __future__ import annotations

import asyncio

from caspian.benchmarks.commitment.baseline import run_baseline
from caspian.benchmarks.commitment.integrity import (
    test_human_nodes,
    test_injection,
    test_invalid_stage,
    test_stage_sequence,
)
from caspian.benchmarks.commitment.report import render_report


def run_all(n_mech: int = 5, n_baseline: int = 5) -> dict:
    data = {
        "stage_sequence": test_stage_sequence(n_mech),
        "injection": test_injection(),
        "invalid_stage": test_invalid_stage(),
        "human_nodes": test_human_nodes(n_mech),
        "baseline": None,
    }
    try:
        data["baseline"] = asyncio.run(run_baseline(n_baseline))
    except Exception as exc:  # noqa: BLE001
        data["baseline"] = None
        data["baseline_error"] = str(exc)
    data["_report"] = render_report(data)
    return data
