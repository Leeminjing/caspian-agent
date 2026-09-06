"""评级生产质量轴：rater + 硬映射 对带标签样本的准确率。

与治理轴 `accuracy.py`（只测 level 的"消费"）正交——本轴测 level 的"生产"。
语料为 (content, source, source_url) → expected_level 的手工标注小样本。

运行（需真实模型 + 环境变量）:
    python -m caspian.benchmarks.rag.rating
"""

from __future__ import annotations

import asyncio
from typing import Any

from caspian.knowledge.rating import decide_level, rate_level
from caspian.models import create_chat_model

# 手工标注：(id, content, source, source_url, expected_level)。
# expected_level=None 表示"信息不足 → 未评级"。
_RATING_CORPUS: list[dict[str, Any]] = [
    {
        "id": "r-001",
        "content": "PostgreSQL 用 ON CONFLICT ... DO UPDATE 实现原子 upsert。",
        "source": "PostgreSQL 官方文档",
        "source_url": "https://www.postgresql.org/docs/current/sql-insert.html",
        "expected": 3,
    },
    {
        "id": "r-002",
        "content": "React 19 中 forwardRef 已弃用，ref 作为普通 prop 传递。",
        "source": "React 官方文档",
        "source_url": "https://react.dev/reference/react/forwardRef",
        "expected": 3,
    },
    {
        "id": "r-003",
        "content": "PostgreSQL 性能调优经验：shared_buffers 设为内存的 25%。",
        "source": "OpenAI 官方博客",
        "source_url": "https://openai.com/blog/engineering",
        "expected": 1,  # 跨领域：OpenAI 博客谈 PostgreSQL → domain_fit 低
    },
    {
        "id": "r-004",
        "content": "Next.js 15 仍必须使用 getServerSideProps 获取数据。",
        "source": "某个人博客",
        "source_url": None,
        "expected": 1,  # 二手/无来源 → 低等级
    },
    {
        "id": "r-005",
        "content": "大概是这样，我也记不太清。",
        "source": "论坛",
        "source_url": None,
        "expected": None,  # 信息不足 → 未评级
    },
]


async def _rate_one(item: dict[str, Any], model) -> int | None:
    rating = await rate_level(
        item["content"],
        item["source"],
        item["source_url"],
        mechanical={},  # 评级轴不依赖域名表，直接软评级
        model=model,
    )
    level, _ = decide_level(rating, confidence_threshold=0.5)
    return level


async def run_rating_accuracy(model=None, sample: int | None = None) -> dict:
    """跑评级生产轴：逐条 rate+decide 并对比 expected_level。返回准确率与逐条明细。"""
    if model is None:
        model = create_chat_model()
    items = _RATING_CORPUS if sample is None else _RATING_CORPUS[:sample]
    correct = 0
    per_item: list[dict[str, Any]] = []
    for item in items:
        got = await _rate_one(item, model)
        ok = got == item["expected"]
        if ok:
            correct += 1
        per_item.append(
            {"id": item["id"], "expected": item["expected"], "got": got, "ok": ok}
        )
    return {"n": len(items), "correct": correct, "per_item": per_item}


def render_rating_report(data: dict) -> str:
    n = data["n"]
    correct = data["correct"]
    lines = [
        "# 评级生产质量轴报告",
        "",
        f"带标签样本 {n} 条；准确率 {correct}/{n} ({correct / n:.0%})。",
        "",
        "| id | expected | got | ok |",
        "|---|---|---|---|",
    ]
    for p in data["per_item"]:
        exp = "未评级" if p["expected"] is None else f"L{p['expected']}"
        got = "未评级" if p["got"] is None else f"L{p['got']}"
        lines.append(f"| {p['id']} | {exp} | {got} | {'✓' if p['ok'] else '✗'} |")
    lines.append("")
    return "\n".join(lines)


async def _main() -> None:
    from dotenv import load_dotenv

    load_dotenv()
    data = await run_rating_accuracy()
    print(render_rating_report(data))


if __name__ == "__main__":
    asyncio.run(_main())
