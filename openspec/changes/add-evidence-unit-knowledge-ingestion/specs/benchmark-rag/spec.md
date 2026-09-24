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

### Requirement: Benchmark 提供事实簇边界金标语料
RAG benchmark SHALL 提供版本化的事实簇边界语料，每个样例包含原文、结构上下文、预期 span、atomicity 与可选时态绑定。语料 SHALL 至少覆盖同一事实簇多句话、明确多主题、依赖上下文不可拆、列表与代码、同文档多版本以及 150–400 理想区间两侧的完整事实簇。句子数量本身 SHALL NOT 作为期望边界。

#### Scenario: 多句同事实簇保持一个边界
- **WHEN** 金标样例用多句话描述同一 API 行为及其直接后果
- **THEN** 预期结果 SHALL 是一个完整 span
- **AND** 快速闸门或语义切分结果 SHALL NOT 仅按句号拆开该事实簇

#### Scenario: 多版本单元拥有独立时态绑定
- **WHEN** 金标文档在不同 heading 或段落讨论不同版本
- **THEN** 每个预期 Evidence Unit SHALL 具有对应的 version binding
- **AND** binding anchor SHALL 能机械还原到原文或 heading span

### Requirement: Benchmark 复验 partial eligibility
RAG benchmark SHALL 同时包含 atomic、indivisible 与 legacy-unknown 候选，并机械验证只有 partial claim 所在的真子区间一侧为 indivisible 时，explicit partial 才可进入 governance；其它 partial 输出 SHALL 降级为 potential。

#### Scenario: 非 indivisible partial 不触发压制
- **WHEN** judge fixture 对 atomic 或 legacy-unknown 单元返回合法锚定的 partial span
- **THEN** benchmark SHALL 观察到 relation 被降级为 potential
- **AND** 单元正文 SHALL 保持不变

### Requirement: Benchmark 报告 Evidence Unit 完整性指标
RAG benchmark 报告 SHALL 在既有错误信息采纳率与正确信息保留率之外，列出单元数量、事实簇边界匹配结果、atomicity 分类结果、时态绑定违规数、partial eligibility 违规数、理想区间内/外数量、身份碰撞数、来源覆盖数、正文 overlap 违规数、hard-max 违规数、非法 span 接受数以及 full/partial conflict 数量。理想区间内/外数量 SHALL 仅为观测指标；时态绑定、partial eligibility、身份碰撞、来源覆盖、overlap、hard-max 和非法 span 接受的违规值 SHALL 为零。

#### Scenario: 报告暴露单元契约违规
- **WHEN** benchmark 完成 Evidence Unit 与治理复验
- **THEN** 报告 SHALL 输出全部单元完整性指标
- **AND** 任一必须为零的违规指标非零时，复验 SHALL 标记失败
