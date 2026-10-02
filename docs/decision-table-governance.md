# 决策等级表治理

决策等级表的生效状态保存在数据库中。旧的 `requirements/.../decision-table.md` 只在首次读取会话时导入为修订 0，之后不再作为运行时写入目标。每次提交携带完整候选表、基准修订、修改理由、Run ID 和幂等键；服务按行 ID 产生 `add`、`modify`、`delete`、`change_priority` 差异。

## 生效与审计

`decision_table_heads` 指向当前修订；`decision_table_revisions` 保存不可变快照。`decision_table_operations`、`decision_table_operation_events` 和 `decision_table_approvals` 保存请求、检查、审批与终态。成功操作、修订和 outbox 事件在同一事务写入。重复幂等键返回原操作；同一键对应不同载荷会被拒绝。旧基准提交返回 `version_conflict`，其事件包含当前修订和介入差异。

权限策略按 `user:<id>` 或 `agent:<id>` 配置，每个主体分别列出 `view`、`propose`、`approve`、`direct_commit` 对四类差异的授权。Agent 默认仅能查看。提案处于 `awaiting_approval` 时原表仍生效；审批绑定操作 ID、候选摘要、基准修订、权限修订和审批者。已授权用户可删除、降级或替换旧决策。

## 模型与执行

主 Agent 和执行子 Agent 在每次模型调用前读取并校验权威表，将完整当前修订或显式空表注入实际模型请求。读取失败阻止调用。模型调用记录保存 Run、表修订、内容摘要、请求摘要与提醒原因。

执行 Run 使用独立的物理 checkpoint 线程。工作消息和改表操作事件进入持久会话事件流，由单写入者投影为共享 checkpoint。模型调用前，执行分支从共享投影补入其他 Run 的已完成记录；关键动作执行前再次读取当前表。旧执行 checkpoint 的恢复不会回退已提交的表修订。

`config.yaml` 的 `decision_review` 配置主 Agent 模型调用轮次提醒间隔、轻量风险判断超时与完整复核超时；`enabled` 控制周期提醒。每次工具调用先按实际动作、参数与工作上下文判断补偿成本和决策固化风险，只有合法的低风险结论可直接进入硬层守卫及执行认领。复合 shell 命令中的高影响部分也会升级复核。高风险、不确定及轻量判断失败均在工具执行前进入完整决策表复核；完整复核超时使动作保持未执行。旧 `critical_tools` 配置已废弃，启动时会明确报配置错误，需要移除。完整复核绑定工具调用、参数、主体、上下文及表修订；人工确认使用可恢复的中断。执行结果不明时不会自动重放。

派生 Context 在创建事务中继承来源 Context 的有效表和权限。多个来源的表或权限不一致时，派生请求返回冲突；继承记录作为一次已提交操作保存。进程重启时，尚处于 `pending` 或 `running` 的 Run 会被标记为执行失败，等待审批的 Run 保持原状态。

## HTTP 查询

会话路径为 `/api/threads/{thread_id}/decision-table`：

| 路径 | 内容 |
| --- | --- |
| `GET /` | 当前修订、内容摘要、行和 `verified_empty`/`verified_current` 状态 |
| `GET /operations` | 按持久事件序号排列的操作历史 |
| `GET /operations/{operation_id}` | 单次请求、差异、审批、状态事件和前后快照 |
| `GET /permissions`、`PUT /permissions` | 权限策略及修订 |
| `GET /model-calls` | 每次模型调用使用的表版本 |
| `GET /action-reviews` | 动作前提醒、结论和最终执行状态 |

`GET /api/threads/{thread_id}/runs/{run_id}` 按认证用户查询持久 Run 身份、来源和状态。单次操作详情也包含其关联 Run 的审计记录。

界面改表通过 `/api/threads/{thread_id}/runs/stream` 创建 Run。中断恢复请求须带 `resume_run_id`，使它回到原执行 checkpoint。操作结果会写入会话事件；未生效状态带有明确标记。

## 数据库迁移

部署新代码前，按项目现有 Alembic 流程升级到 `e9f0a1b2c3d4`。`d8e9f0a1b2c3` 增加治理、事件、模型调用、动作复核和共享投影表；后续增量迁移添加动作参数和 Run 审计，已有治理迁移的数据库也可继续升级。首次读取时执行旧表只读导入；保留旧文件供历史核对。迁移脚本的往返测试位于 `backend/packages/harness/tests/test_decision_governance_migration.py`。
