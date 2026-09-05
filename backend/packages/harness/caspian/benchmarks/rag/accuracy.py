"""答案级 factual accuracy:用 flash 模型据「幸存证据」生成答案,vs ground_truth 判对错。

健壮性 + 断点续跑:
- 每条调用带重试(3 次退避),单条失败返回空串(计为错误、不崩溃)。
- 结果逐条写入 checkpoint 文件(JSONL),重跑时跳过已完成条目,进度不丢。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from langchain_core.messages import HumanMessage

from caspian.benchmarks.rag.arms import arm_level_governed, arm_plain, arm_score_based
from caspian.benchmarks.rag.conflictqa import load_conflictqa
from caspian.models import create_chat_model

_GEN_PROMPT = (
    "Answer the question based ONLY on the provided context. "
    "Reply with just the answer (a few words), no explanation.\n\n"
    "Question: {question}\n\nContext:\n{context}"
)

_ARMS = {
    "plain": arm_plain,
    "score-based": arm_score_based,
    "level-governed": arm_level_governed,
}

_CHECKPOINT = Path(__file__).resolve().parent / "data" / "accuracy-checkpoint.jsonl"


def _judge(answer: str, truths: list[str]) -> bool:
    a = (answer or "").lower()
    for t in truths:
        t = str(t).lower().strip()
        if t and (re.search(r"\b" + re.escape(t) + r"\b", a) or t in a):
            return True
    return False


def _load_done() -> set[tuple[str, str]]:
    done: set[tuple[str, str]] = set()
    if _CHECKPOINT.exists():
        with open(_CHECKPOINT, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        r = json.loads(line)
                        done.add((str(r.get("id")), str(r.get("arm"))))
                    except json.JSONDecodeError:
                        continue
    return done


def _append_result(item_id: str, arm: str, answer: str, correct: bool) -> None:
    with open(_CHECKPOINT, "a", encoding="utf-8") as f:
        f.write(json.dumps({"id": item_id, "arm": arm, "answer": answer, "correct": correct}, ensure_ascii=False) + "\n")


async def _gen(model, sem: asyncio.Semaphore, question: str, evidences: list[str], retries: int = 3) -> str:
    context = "\n".join(f"- {e}" for e in evidences)
    prompt = _GEN_PROMPT.format(question=question, context=context)
    for attempt in range(retries):
        try:
            async with sem:
                async with asyncio.timeout(20):
                    resp = await model.ainvoke([HumanMessage(content=prompt)])
            return str(resp.content).strip()
        except Exception:
            if attempt == retries - 1:
                return ""
            await asyncio.sleep(1.5 * (attempt + 1))
    return ""


async def run_answer_accuracy(sample: int = 1000, concurrency: int = 8) -> dict:
    from dotenv import load_dotenv

    load_dotenv()
    items = load_conflictqa()[:sample]
    model = create_chat_model()  # deepseek-v4-flash
    sem = asyncio.Semaphore(concurrency)
    done = _load_done()

    async def work(item, arm_name, evidences):
        answer = await _gen(model, sem, item.query, evidences)
        correct = bool(answer) and _judge(answer, item.answers)
        _append_result(item.id, arm_name, answer, correct)
        return (arm_name, correct)

    for name, arm_fn in _ARMS.items():
        pending = []
        for item in items:
            if (item.id, name) in done:
                continue
            final_ids = arm_fn(item)
            evidences = [c.content for c in item.candidates if c.id in final_ids]
            pending.append((item, evidences))
        if not pending:
            print(f"[progress] {name}: all {len(items)} already done (from checkpoint)", flush=True)
            continue
        tasks = [asyncio.create_task(work(item, name, ev)) for item, ev in pending]
        done_count = 0
        for t in asyncio.as_completed(tasks):
            await t
            done_count += 1
            if done_count % 100 == 0:
                print(f"[progress] {name}: {done_count}/{len(pending)} new (+{len(items)-len(pending)} cached)", flush=True)
        print(f"[progress] {name}: done (new {len(pending)}, cached {len(items)-len(pending)})", flush=True)

    # 汇总 checkpoint
    agg: dict[str, dict] = {}
    for name in _ARMS:
        agg[name] = {"correct": 0, "n": 0}
    with open(_CHECKPOINT, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            agg[r["arm"]]["n"] += 1
            if r["correct"]:
                agg[r["arm"]]["correct"] += 1
    return {"n": len(items), "arms": agg}


def aggregate_checkpoint() -> dict[str, dict]:
    """从 checkpoint 文件聚合每臂的答案级 accuracy。"""
    agg: dict[str, dict] = {}
    if not _CHECKPOINT.exists():
        return agg
    with open(_CHECKPOINT, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            arm = str(r.get("arm"))
            agg.setdefault(arm, {"correct": 0, "n": 0})
            agg[arm]["n"] += 1
            if r.get("correct"):
                agg[arm]["correct"] += 1
    return agg


if __name__ == "__main__":
    import sys

    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
    c = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    result = asyncio.run(run_answer_accuracy(sample=n, concurrency=c))
    total = result["n"]
    print(f"answer-level accuracy (n={total}):")
    for name, e in result["arms"].items():
        print(f"  {name}: {e['correct']}/{e['n']} ({e['correct']/e['n']:.1%})" if e["n"] else f"  {name}: 0/0")
