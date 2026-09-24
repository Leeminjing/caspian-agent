## ADDED Requirements

### Requirement: Benchmark 保留同正文多来源的独立证据身份
RAG benchmark SHALL 包含正文完全相同但来源、权威等级或文档修订不同的 Evidence Unit，并机械验证这些单元拥有不同身份且不会在装载或检索准备阶段互相覆盖。

#### Scenario: 同正文不同来源均进入语料
- **WHEN** benchmark 语料包含两个来源相异但正文相同的 Evidence Unit
- **THEN** 两条单元 SHALL 拥有不同 chunk IDs
- **AND** benchmark 候选集中 SHALL 同时保留两条证据

### Requirement: Benchmark 复验检索字段与治理字段隔离
Benchmark SHALL 提供机械检查，证明 embedding 输入只来自 `retrieval_text`，且改变 level、level basis、provenance 或来源数量不会改变同一 Evidence Unit 的 embedding 文本。检查 SHALL 不依赖真实 embedding 服务或 LLM。

#### Scenario: 治理 metadata 不进入 embedding
- **WHEN** benchmark 仅改变一个 Evidence Unit 的等级和评级依据
- **THEN** 捕获到的 embedding 输入 SHALL 保持不变
- **AND** 治理阶段 SHALL 仍使用变更后的等级作机械裁决

### Requirement: Benchmark 覆盖文档到 Evidence Unit 的治理粒度
Benchmark SHALL 包含由多命题文档生成的预期 Evidence Unit 边界，并复验独立事实簇之间不使用正文 overlap、同命题冲突优先表现为 unit-to-unit full conflict、partial conflict 仅在不可进一步合理拆分的复合证据中出现。

#### Scenario: 多命题文档形成独立治理对象
- **WHEN** benchmark 输入文档包含两个相互独立的事实簇
- **THEN** 预处理结果 SHALL 产生两个不重叠的 Evidence Unit
- **AND** 针对其中一个事实簇的冲突 SHALL NOT 修改另一个单元的正文或治理状态

### Requirement: Benchmark 报告 Evidence Unit 完整性指标
RAG benchmark 报告 SHALL 在既有错误信息采纳率与正确信息保留率之外，列出单元数量、身份碰撞数、来源覆盖数、正文 overlap 违规数、hard-max 违规数、非法 span 接受数以及 full/partial conflict 数量。身份碰撞、来源覆盖、overlap、hard-max 和非法 span 接受的合格值 SHALL 为零。

#### Scenario: 报告暴露单元契约违规
- **WHEN** benchmark 完成 Evidence Unit 与治理复验
- **THEN** 报告 SHALL 输出全部单元完整性指标
- **AND** 任一必须为零的违规指标非零时，复验 SHALL 标记失败
