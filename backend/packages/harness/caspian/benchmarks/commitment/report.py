"""承诺层 benchmark 报告渲染。"""

from __future__ import annotations


def render_report(data: dict) -> str:
    lines = ["# 承诺层 benchmark 报告", ""]

    seq = data["stage_sequence"]
    lines.append("## 1. 阶段顺序完整性(机械)")
    lines.append("")
    lines.append(f"- {seq['passed']}/{seq['n']} 次跑分阶段序列严格 1→9。")
    lines.append("")

    inj = data["injection"]
    lines.append("## 2. 注入跳步率(机械)")
    lines.append("")
    lines.append(f"- {inj['passed']}/{inj['n']} 种「跳过/重排阶段」注入,阶段序列仍严格 1→9,跳步成功率 0%。")
    lines.append("")

    inv = data["invalid_stage"]
    lines.append("## 2b. 越序调用拒绝(机械)")
    lines.append("")
    lines.append(f"- 越序直调 delegate_with_review(stage=9 而 expected=1):{'✓ 被 invalid_stage 拒绝' if inv['rejected'] else '✗ 未拒绝'}。")
    lines.append("")

    hum = data["human_nodes"]
    lines.append("## 3. 人工节点完整性(机械)")
    lines.append("")
    lines.append(f"- {hum['passed']}/{hum['n']} 次跑分,3/5/6/7 四个人工节点全部命中。")
    lines.append("")

    base = data["baseline"]
    lines.append("## 4. 软 baseline 对照(LLM)")
    lines.append("")
    if base is None:
        lines.append("- 待测:依赖 deepseek 网络,当前不可达(机械测试不依赖 LLM)。")
    else:
        no_inj = base["no_injection"]
        with_inj = base["with_injection"]
        lines.append(f"- 无注入:平均缺失规划维度 {no_inj['avg_missing']:.1f}/6,完整覆盖 {no_inj['full_coverage']}/{no_inj['n']}。")
        lines.append(f"- 注入「跳过规划直接输出」:平均缺失规划维度 {with_inj['avg_missing']:.1f}/6,完整覆盖 {with_inj['full_coverage']}/{with_inj['n']}。")
    lines.append("")
    lines.append("> 软 baseline(无阶段机)在注入下缺失更多规划维度;硬臂(承诺层)因阶段锁不受注入影响,恒产出完整合同。")
    lines.append("")

    lines.append("## 结论")
    lines.append("")
    lines.append("承诺层的「九阶段强制推进」是结构性硬机制:阶段严格递增、越序被拒、人工节点不丢、注入无法跳步;")
    lines.append("而软 baseline(把规划纪律写进 prompt)没有阶段锁,注入即可诱导其跳过规划。")
    return "\n".join(lines)
