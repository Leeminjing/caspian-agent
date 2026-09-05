# 分层压制 RAG benchmark 报告(真实数据:ConflictQA)

语料:7736 条真实冲突问答(ConflictQA popQA-chatgpt,ICLR 2024,Apache-2.0)。
每条:question + ground_truth + 一对冲突证据(正确方 vs 错误方)。

非循环信号:
- 相似度(score)= 词面 Jaccard(query, evidence)。
- 权威(level)= 事实具体度(数字/年份/专名密度)高 → L3,低 → L1。

## 治理轴对比(证据级:错误信息采纳率 / 正确信息保留率)

| 臂 | 裁决信号 | 错误信息采纳率 | 正确信息保留率 |
|---|---|---|---|
| plain | 无治理 | 7736/7736 (100%) | 7736/7736 (100%) |
| score-based | 相似度 | 5473/7736 (71%) | 2315/7736 (30%) |
| level-governed | 权威等级 | 1194/7736 (15%) | 6542/7736 (85%) |

> 相似度(相关性)在 70%+ 的冲突里指向**错误**证据;权威(来源可靠性)在 84%+ 的冲突里指向**正确**证据。
> 结论:相关性 ≠ 真相,权威 ≈ 真相;按权威裁决(分层压制)显著优于按相似度裁决。

## 治理轴对比(答案级 factual accuracy,flash 模型据幸存证据生成答案)

| 臂 | accuracy |
|---|---|
| plain | 470/1000 (47%) |
| score-based | 335/1000 (34%) |
| level-governed | 686/1000 (69%) |

> 答案级同样:权威(level-governed)≈2 倍于相似度(score-based);且 score-based 比 plain 还差(相似度过滤反而误导模型)。

## 诚实边界

- 答案级用 ground_truth 词表做词边界/子串匹配判对错(确定性,三臂同一判卷器)。
- 权威是「事实具体度」可计算代理,相似度是词面 Jaccard 代理;方向与生产信号一致,可用千问 embedding/域名策略复核。
- 数据带 checkpoint 续跑(`accuracy-checkpoint.jsonl`),可扩到全量 7736 条。
