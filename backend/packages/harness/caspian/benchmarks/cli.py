"""benchmark 命令行入口:跑分并输出报告。

用法:
    python -m caspian.benchmarks.cli --n 30
    python -m caspian.benchmarks.cli --n 5 --arm hard --task dt-001

首次运行前需确保模型 API 可用(.env 已加载 OPENAI_API_KEY 等)。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from dotenv import load_dotenv

from caspian.benchmarks.report import render_mechanism_report, render_report
from caspian.benchmarks.runner import run_many
from caspian.benchmarks.schema import load_corpus

logger = logging.getLogger(__name__)

_CORPUS_PATH = Path(__file__).resolve().parent / "tasks" / "corpus.yaml"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Caspian 决策表治理 benchmark")
    parser.add_argument("--n", type=int, default=30, help="每格运行次数(默认 30)")
    parser.add_argument("--arm", choices=["hard", "soft"], default=None, help="只跑单臂")
    parser.add_argument("--task", default=None, help="只跑指定任务 id(如 dt-001)")
    parser.add_argument("--corpus", default=str(_CORPUS_PATH), help="语料路径")
    parser.add_argument("--out", default="benchmark-report.md", help="报告输出路径")
    parser.add_argument("--mechanism", action="store_true", help="跑确定性机制级消融(不调 LLM)")
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> None:
    tasks = load_corpus(args.corpus)
    if args.task:
        tasks = [t for t in tasks if t.id == args.task]
        if not tasks:
            raise SystemExit(f"未找到任务 {args.task}")

    if args.mechanism:
        from caspian.benchmarks.mechanism import aggregate_mechanism, run_mechanism_ablation_all

        results = await run_mechanism_ablation_all(tasks)
        report = render_mechanism_report(aggregate_mechanism(results))
    else:
        arms = [args.arm] if args.arm else ["hard", "soft"]
        logger.info("跑分: %d 任务 × %s × %d 次", len(tasks), arms, args.n)
        results = await run_many(tasks, arms, args.n)
        per_arm = {arm: [r for r in results if r.arm == arm] for arm in arms}
        report = render_report(per_arm)

    Path(args.out).write_text(report + "\n", encoding="utf-8")
    print(report)
    print(f"\n报告已写入 {args.out}")


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = _parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
