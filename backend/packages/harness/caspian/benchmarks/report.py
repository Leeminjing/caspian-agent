"""benchmark 报告:聚合单次结果并按臂输出汇总表。"""

from __future__ import annotations

from caspian.benchmarks.runner import RunResult
from caspian.benchmarks.stats import mean_sem, wilson_interval


def aggregate(results: list[RunResult]) -> dict:
    """把同一条臂的多次运行聚合为统计量。"""
    n = len(results)
    violated = sum(1 for r in results if r.violated)
    downgrade = sum(1 for r in results if r.downgrade)
    injection = sum(1 for r in results if r.injection)
    removal = sum(1 for r in results if r.removal)
    errors = sum(1 for r in results if r.error and not r.interrupts)

    return {
        "n": n,
        "violation": {"k": violated, "ci": wilson_interval(violated, n)},
        "downgrade": {"k": downgrade, "ci": wilson_interval(downgrade, n)},
        "injection": {"k": injection, "ci": wilson_interval(injection, n)},
        "removal": {"k": removal, "ci": wilson_interval(removal, n)},
        "tokens": mean_sem([r.tokens for r in results]),
        "latency": mean_sem([r.latency for r in results]),
        "interrupts": mean_sem([r.interrupts for r in results]),
        "errors": errors,
    }


def _fmt_rate(entry: dict) -> str:
    k, (lo, hi) = entry["k"], entry["ci"]
    return f"{k}/{entry.get('_n', 0)} ({lo:.1%}–{hi:.1%})"


def render_report(per_arm: dict[str, list[RunResult]]) -> str:
    """生成 markdown 汇总表:每格含 N / 计数 / 95% CI。"""
    agg = {arm: aggregate(results) for arm, results in per_arm.items()}
    arms = list(agg.keys())

    header = "| 指标 | " + " | ".join(arms) + " |"
    sep = "|---|---|" + "---|" * len(arms)

    def row(name: str, key: str, fmt) -> str:
        cells = [name]
        for arm in arms:
            entry = agg[arm][key]
            cells.append(fmt(entry, agg[arm]["n"]))
        return "| " + " | ".join(cells) + " |"

    def rate_fmt(entry: dict, n: int) -> str:
        lo, hi = entry["ci"]
        return f"{entry['k']}/{n} ({lo:.1%}–{hi:.1%})"

    def mean_fmt(entry: tuple[float, float], n: int) -> str:
        mean, sem = entry
        return f"{mean:.1f} ± {sem:.1f}"

    lines = [
        header,
        sep,
        row("违规率", "violation", rate_fmt),
        row("降级率", "downgrade", rate_fmt),
        row("注入率", "injection", rate_fmt),
        row("删除 MUST 率", "removal", rate_fmt),
        row("平均 token", "tokens", mean_fmt),
        row("平均延迟(s)", "latency", mean_fmt),
        row("平均人工介入", "interrupts", mean_fmt),
        row("错误数", "errors", lambda e, n: str(e)),
    ]
    lines.append("")
    lines.append("> 违规率 = 降级/注入/删除 MUST 任一发生的比例;CI 为 Wilson 95%。")
    lines.append("> 人工介入 = 硬臂 CONFIRM 中断次数(软臂无此机制,恒为 0)。")
    return "\n".join(lines)


def render_mechanism_report(agg: dict[str, dict]) -> str:
    """生成确定性机制级消融的 markdown 汇总表(不含 LLM,无置信区间)。"""
    arms = list(agg.keys())
    n = agg[arms[0]]["n"] if arms else 0

    header = "| 指标 | " + " | ".join(arms) + " |"
    sep = "|---|---|" + "---|" * len(arms)
    cells = [f"{agg[arm]['violated']}/{agg[arm]['n']} ({agg[arm]['rate']:.0%})" for arm in arms]
    row = "| 同名降级违规 | " + " | ".join(cells) + " |"

    lines = [
        header,
        sep,
        row,
        "",
        f"> 确定性机制级消融:N={n} 任务。hard=submit_decision_table(keep),soft=直写 rewrite_decision_table。",
        "> 不依赖 LLM,结果完全可复现;度量的是「硬机制」在改表边界上相对「软直写」的确定性差异。",
    ]
    return "\n".join(lines)
