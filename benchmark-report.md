| 指标 | hard | soft |
|---|---|---|---|
| 同名降级违规 | 0/20 (0%) | 20/20 (100%) |

> 确定性机制级消融:N=20 任务。hard=submit_decision_table(keep),soft=直写 rewrite_decision_table。
> 不依赖 LLM,结果完全可复现;度量的是「硬机制」在改表边界上相对「软直写」的确定性差异。
