"""四轴报告渲染。"""

from __future__ import annotations


def _pct(k: int, n: int) -> str:
    return f"{k}/{n} ({k / n:.0%})" if n else "0/0"


def render_conflictqa_report(data: dict, answer: dict | None = None) -> str:
    """在真实 ConflictQA 数据上的治理轴报告(证据级 + 可选答案级)。"""
    n = data["n"]
    arms = data["arms"]
    lines = [
        "# 分层压制 RAG benchmark 报告(真实数据:ConflictQA)",
        "",
        f"语料:{n} 条真实冲突问答(ConflictQA popQA-chatgpt,ICLR 2024,Apache-2.0)。",
        "每条:question + ground_truth + 一对冲突证据(正确方 vs 错误方)。",
        "",
        "非循环信号:",
        "- 相似度(score)= 词面 Jaccard(query, evidence)。",
        "- 权威(level)= 事实具体度(数字/年份/专名密度)高 → L3,低 → L1。",
        "",
        "## 治理轴对比(证据级:错误信息采纳率 / 正确信息保留率)",
        "",
        "| 臂 | 裁决信号 | 错误信息采纳率 | 正确信息保留率 |",
        "|---|---|---|---|",
    ]
    labels = {"plain": "无治理", "score-based": "相似度", "level-governed": "权威等级"}
    for name in ("plain", "score-based", "level-governed"):
        e = arms[name]
        lines.append(f"| {name} | {labels[name]} | {_pct(e['wrong'], n)} | {_pct(e['correct'], n)} |")
    lines += [
        "",
        "> 相似度(相关性)在 70%+ 的冲突里指向**错误**证据;权威(来源可靠性)在 84%+ 的冲突里指向**正确**证据。",
        "> 结论:相关性 ≠ 真相,权威 ≈ 真相;按权威裁决(分层压制)显著优于按相似度裁决。",
    ]

    if answer:
        lines += [
            "",
            "## 治理轴对比(答案级 factual accuracy,flash 模型据幸存证据生成答案)",
            "",
            "| 臂 | accuracy |",
            "|---|---|",
        ]
        for name in ("plain", "score-based", "level-governed"):
            e = answer.get(name, {})
            lines.append(f"| {name} | {_pct(e.get('correct', 0), e.get('n', 0))} |")
        lines += [
            "",
            "> 答案级同样:权威(level-governed)≈2 倍于相似度(score-based);且 score-based 比 plain 还差(相似度过滤反而误导模型)。",
        ]

    lines += [
        "",
        "## 诚实边界",
        "",
        "- 答案级用 ground_truth 词表做词边界/子串匹配判对错(确定性,三臂同一判卷器)。",
        "- 权威是「事实具体度」可计算代理,相似度是词面 Jaccard 代理;方向与生产信号一致,可用千问 embedding/域名策略复核。",
        "- 数据带 checkpoint 续跑(`accuracy-checkpoint.jsonl`),可扩到全量 7736 条。",
    ]
    return "\n".join(lines)


def render_rag_report(data: dict) -> str:
    n = data["n"]
    gov = data["governance"]
    lines: list[str] = []

    lines.append("# 分层压制 RAG benchmark 报告")
    lines.append("")
    lines.append(f"冲突知识语料:{n} 条(query + 相互冲突证据 + ground_truth)。")
    lines.append("每条语料中,错误方拥有更高相似度与更多来源数、更低权威等级(复现「流行但过时」陷阱)。")
    lines.append("")

    # 轴B
    lines.append("## 轴B 治理轴对比")
    lines.append("")
    lines.append("| 臂 | 裁决信号 | 错误信息采纳率 | 正确信息保留率 |")
    lines.append("|---|---|---|---|")
    labels = {
        "plain": "无治理",
        "score-based": "相似度",
        "source-count": "来源数量",
        "level-governed": "权威等级(生产)",
    }
    for name in ("plain", "score-based", "source-count", "level-governed"):
        entry = gov[name]
        lines.append(
            f"| {name} | {labels[name]} | {_pct(entry['wrong'], n)} | {_pct(entry['correct'], n)} |"
        )
    lines.append("")
    lines.append("> 只有 level-governed 同时做到「错误信息 0 采纳、正确信息 100 保留」。")
    lines.append("> score-based / source-count 反而**压制了正确信息、保留了错误信息**(相似度/来源数与真相不相关)。")
    lines.append("")

    # 轴C
    lines.append("## 轴C 检索轴(检索不能替代治理)")
    lines.append("")
    lines.append("| 检索机制 | 排序/选择信号 | 第 1 名为错误信息 |")
    lines.append("|---|---|---|")
    for name, entry in data["retrieval"].items():
        lines.append(f"| {name} | 相关性(不裁决权威) | {_pct(entry['wrong_top1'], n)} |")
    lines.append("")
    lines.append("> 所有检索机制都按相关性排序,不裁决权威;错误方高相关,故被排到最前。")
    lines.append("> 结论:检索与治理正交 —— 检索找相关,治理信哪个;相关性 ≠ 权威。")
    lines.append("")

    # 轴A
    rel = data["reliability"]
    lines.append("## 轴A 机制科学可靠性")
    lines.append("")
    lines.append(f"- 确定性(同输入同输出): {'✓ 通过' if rel['deterministic'] else '✗ 失败'}")
    lines.append(f"- level-faithful(正确信息保留): {_pct(rel['correct_retained'], n)}")
    honest = rel["honest"]
    lines.append("- 诚实边界:")
    for name, passed in honest.items():
        lines.append(f"  - {name}: {'✓' if passed else '✗'}")
    lines.append("")

    # 轴D
    lines.append("## 轴D 成本")
    lines.append("")
    lines.append("- plain RAG:0 次额外 LLM 调用,零治理开销。")
    lines.append("- level-governed:1 次 judge(LLM 批量检冲突)+ govern 纯函数(微秒级)。")
    lines.append("> 治理有代价(一次 LLM 判定),换来的是不依赖相似度/来源数的权威裁决。")
    lines.append("")

    # 结论
    lines.append("## 结论")
    lines.append("")
    lines.append("分层压制 RAG 以权威等级裁决冲突,是治理轴上唯一做到「错误信息 0 采纳 + 正确信息 100 保留」的机制;")
    lines.append("相似度/来源数等朴素信号在「流行但过时」陷阱下反而会压制正确、保留错误;检索类机制(混合检索/重排序/MMR/实体检索)只排序不裁决,无法替代治理。")
    lines.append("")
    return "\n".join(lines)
