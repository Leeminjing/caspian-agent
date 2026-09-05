"""对单次运行结果做确定性判分,产出 benchmark 指标(复用 oracle)。"""

from __future__ import annotations

from caspian.benchmarks.oracle import score_final_rows
from caspian.benchmarks.schema import TaskSpec


def score_run(final_rows: list, task: TaskSpec) -> dict:
    """把最终表行判分为单次运行指标。

    输出:
        dict — {violated, downgrade, injection, removal}
    """
    verdict = score_final_rows(final_rows, task)
    return {
        "violated": verdict["violated"],
        "downgrade": bool(verdict["downgrades"]),
        "injection": bool(verdict["injections"]),
        "removal": bool(verdict["removals"]),
    }
