## ADDED Requirements

### Requirement: 相关性、证据正文与权威治理字段严格隔离
每条新格式知识记录 SHALL 分别保存 `content`、`retrieval_text` 与治理 metadata。`content` SHALL 是可供评级、judge 和 govern 使用的原文精确子串；`retrieval_text` SHALL 只由标题、section path、版本/时间上下文和 `content` 确定性组装；向量索引 SHALL 只消费 `retrieval_text`。`level`、`level_basis`、provenance、source type、authority score 与其它治理结果 SHALL NOT 参与 embedding。

#### Scenario: embedding 输入只含检索文本
- **WHEN** 一个 Evidence Unit 被写入向量 Store
- **THEN** embedding 输入 SHALL 等于该单元的 `retrieval_text`
- **AND** 改变 level、level basis 或 provenance SHALL NOT 改变该单元的 embedding 文本

#### Scenario: 标题路径补充上下文但不污染正文
- **WHEN** 一个短证据依赖标题、章节路径或版本才能正确召回
- **THEN** 这些字段 SHALL 出现在 `retrieval_text` 中
- **AND** `content` SHALL 继续保持原文证据本体，不得把补充上下文写回正文

### Requirement: 查询返回 Evidence Unit 元数据并兼容旧条目
向量查询 SHALL 召回 Evidence Unit，并在候选投影中提供其文档、修订、结构、原文 span 与时间/版本元数据。缺少新字段的存量知识条目 SHALL 作为 legacy Evidence Unit 继续可读、可查询、可人工覆盖等级；读取方 SHALL 为缺失字段提供明确空值或 legacy 标记，而不是拒绝整条记录。

#### Scenario: 新格式候选包含追踪信息
- **WHEN** 查询命中新格式 Evidence Unit
- **THEN** 候选结果 SHALL 包含 chunk、document、revision 身份及 source span
- **AND** SHALL 包含可用的 title、section path、version、published/effective 时间字段

#### Scenario: 存量条目继续可用
- **WHEN** 查询或 PATCH 命中一个只有旧字段的知识条目
- **THEN** 系统 SHALL 继续返回并处理该条目
- **AND** SHALL NOT 要求先执行破坏性数据迁移

### Requirement: 冲突 judge 只消费语义与时态上下文
冲突 judge SHALL 接收候选的 `id`、`content`、`title`、`section_path`、`version`、`published_at` 与 `effective_at`，以便区分同主题不同时间或版本。judge SHALL NOT 接收候选的 `level`、相似度、authority score、`level_basis`、provenance 或来源数量；冲突识别与权威裁决 SHALL 保持正交。

#### Scenario: 时态字段帮助区分版本
- **WHEN** 两个候选正文没有直接写出版本，但结构化 metadata 表明它们属于不同版本
- **THEN** judge SHALL 能使用该 metadata 判定二者时间或版本不相交
- **AND** SHALL NOT 仅因正文表面矛盾输出同版本 explicit 冲突

#### Scenario: 权威等级对 judge 不可见
- **WHEN** 一条 L3 和一条 L1 Evidence Unit 被送入冲突判定
- **THEN** judge 输入 SHALL 不包含任何可推断其等级或评级依据的字段
- **AND** 冲突关系 SHALL 在后续由治理引擎结合等级裁决

### Requirement: 单元级 full conflict 是常规路径且 partial suppression 保留为兜底
系统 SHALL 在 Evidence Unit 之间执行既有冲突治理。语义边界已分离的单元若整体针对同一命题对立，judge SHALL 使用 `scope="full"`；只有一个无法合理继续拆分的 Evidence Unit 内部含多个不可分离命题且其中部分冲突时，才 SHALL 使用经原文锚定的 `scope="partial"`。治理结果仍 SHALL 保持跨等级压制、同级不裁决、potential 不压制和查询级零持久化。

#### Scenario: 独立命题按 full conflict 治理
- **WHEN** 两个原子 Evidence Unit 对同一时间版本的同一命题给出相反结论
- **THEN** judge SHALL 输出 full explicit 或 potential 关系
- **AND** govern SHALL 按既有等级规则处理

#### Scenario: partial 继续要求原文锚定
- **WHEN** 无法进一步合理拆分的单元只有部分命题与另一单元冲突
- **THEN** partial 关系 SHALL 继续提供可验证的 claim 与 span
- **AND** 未锚定的 partial 关系 SHALL 降级为 potential，不得触发压制

## MODIFIED Requirements

### Requirement: 时间/版本不匹配不视为冲突
judge SHALL 在判定两条 Evidence Unit 冲突时使用正文以及 `title`、`section_path`、`version`、`published_at`、`effective_at` 等非权威 metadata 识别其时间/版本目标；若两条证据针对同一主题但分属不同时间点或版本，SHALL NOT 输出 explicit 或 potential（可省略该对，或输出 relation="temporal_disjoint" 以说明版本差异）。judge SHALL NOT 读取 level、level basis、provenance、相似度或来源数量。治理引擎 SHALL 将 temporal_disjoint（若出现）视为「非同一命题冲突」：双方 SHALL 均保留、SHALL NOT 压制、SHALL NOT 落为 conflict_same_level，并 SHALL 在治理提示中说明二者针对不同时间/版本。

#### Scenario: 不同版本判定为非冲突
- **WHEN** 两条 Evidence Unit 分别针对同一主题的不同版本，且版本可由正文或结构化 metadata 确定
- **THEN** judge SHALL NOT 输出 explicit 或 potential
- **AND** 可省略该对或输出 relation="temporal_disjoint"

#### Scenario: 时间不匹配双方均保留
- **WHEN** 治理引擎收到一条 temporal_disjoint 关系
- **THEN** 双方证据 SHALL 均保留在最终证据集
- **AND** SHALL NOT 触发等级压制或 conflict_same_level
- **AND** 治理提示 SHALL 说明二者针对不同时间/版本

#### Scenario: 同时间同命题冲突仍按既有规则治理
- **WHEN** 两条 Evidence Unit 针对相同时间/版本的同一命题且给出明确相反结论
- **THEN** judge SHALL 输出 explicit（或 potential）
- **AND** 治理引擎 SHALL 按既有等级规则裁决，行为不变
