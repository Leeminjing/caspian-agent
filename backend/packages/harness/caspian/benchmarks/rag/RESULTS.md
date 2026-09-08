# RAG Benchmark Results — Level-Governed vs Baselines (E2E)

Status: one full answer-level E2E run over the conflict-knowledge corpus (Oracle retrieval, pre-seeded candidates).

## Metric

**Authority-violation rate** (answer-level): fraction of cross-level conflict items where the final answer adopts the conclusion of the lower-authority candidate.

## Results

| Arm | Authority-violation rate |
| --- | --- |
| Vanilla (plain) | 22% |
| Score-based | 100% |
| Caspian (level-governed) | 0% |
| LLM soft governance | 0% |

Level-governed eliminates answer-level authority violation. The similarity/source-count baselines do not — score-based is worst (always follows the high-score, low-authority side).

## Known issue: same-level abstention ~0%

On same-level conflict items (two equally-authoritative candidates), the answer-level "abstention" metric — expresses uncertainty / presents both sides instead of picking one — measures ~0%.

- Root cause: the answer-generation prompt requires the model to reply with just the answer, forcing it to pick one side and suppressing uncertainty.
- Impact: cannot yet validate that "same-level → do not adjudicate" holds at the answer layer. This is future work.
