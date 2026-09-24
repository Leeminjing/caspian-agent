## Purpose

定义 Caspian 从来源文档构造、验证、评级并持久化 Evidence Unit 的行为契约，使每个进入知识治理系统的对象都是语义完整、命题集中、来源与时间一致且可机械追溯到原文位置的最小证据单元。

## ADDED Requirements

### Requirement: Evidence Unit 是知识入库与治理的最小单位
系统 SHALL 以 Evidence Unit 作为可独立召回、评级、冲突判断和压制的最小对象。每个新单元 SHALL 包含 `chunk_id`、`document_id`、`document_revision_id`、原文 `content`、`retrieval_text`、`title`、`section_path`、`chunk_index`、绝对 `source_span`、`source`、`source_url`、`published_at`、`effective_at`、`version`、`temporal_bindings`、`atomicity`、`level` 与 `level_basis`；缺失的可选来源或时间字段 SHALL 显式为空而不是无锚点地从正文猜测。

#### Scenario: 单元可独立治理
- **WHEN** 一个文档被拆成多个 Evidence Unit
- **THEN** 每个单元 SHALL 拥有独立 ID、原文位置、检索文本与评级结果
- **AND** 任一单元 SHALL 可在不读取同文档其它单元的情况下参与召回和治理

### Requirement: 文档结构预切分保持天然边界
系统 SHALL 在调用语义模型前先按文档天然结构产生候选块。Markdown 输入 SHALL 识别标题层级、段落、列表、表格与代码块，并为候选块保存有序 `section_path` 和原文绝对 span；普通文本 SHALL 至少按段落边界处理。结构预切分 SHALL NOT 改写、摘要或规范化证据正文。

#### Scenario: Markdown 标题路径被保留
- **WHEN** 文档包含多级标题及其下的段落、列表或表格
- **THEN** 每个候选块 SHALL 携带其标题层级组成的 `section_path`
- **AND** 候选块正文 SHALL 等于原文对应 span 的精确子串

#### Scenario: 代码块不被结构解析破坏
- **WHEN** Markdown 文档包含 fenced code block
- **THEN** 结构预切分 SHALL 把完整代码块识别为一个结构候选
- **AND** SHALL NOT 因代码内部空行或标点把它误当成普通段落拆开

### Requirement: 原子性闸门只让明确单一的候选块走快速路径
系统 SHALL 使用确定性、保守触发的原子性闸门判断候选块是否需要语义切分。处于 hard max 内且没有高置信多主题信号的候选块 SHALL 直接通过；句子数量、句末标点数量、分号数量或代码声明数量本身 SHALL NOT 构成多主题证据。只有 hard max 超限、多个可独立治理的结构项或明确主题转换等高置信信号才 SHALL 触发语义 span 切分。

#### Scenario: 短单命题段落不调用语义切分
- **WHEN** 一个候选块短且只表达一个完整事实簇
- **THEN** 系统 SHALL 直接把它作为 Evidence Unit
- **AND** SHALL NOT 为该候选块支付额外语义切分调用

#### Scenario: 同一事实簇的多句话走快速路径
- **WHEN** 一个 hard max 内的候选块用两句或更多句共同说明同一 API、行为、版本或因果事实簇，且没有高置信主题转换信号
- **THEN** 系统 SHALL 直接保留完整候选块
- **AND** SHALL NOT 仅因句子、句号、分号或声明数量大于一而调用语义切分

#### Scenario: 多主题候选块进入语义切分
- **WHEN** 一个候选块包含两个可独立治理的主题
- **THEN** 系统 SHALL NOT 把整个候选块直接作为一个 Evidence Unit
- **AND** SHALL 把它交给语义 span 切分

### Requirement: 语义切分只能返回可验证原文 span
语义切分器 SHALL 只返回候选块原文中的有序 `{start, end}` 边界、`atomicity`、可选时态绑定及非权威诊断信息，SHALL NOT 返回改写、摘要或新生成的证据正文。prompt SHALL 明确定义事实簇，并包含“多句同事实簇不拆”“不同主题要拆”“依赖上下文不拆”“多版本分别绑定”的正反 few-shot。代码 SHALL 验证每个 span 的范围、顺序、不重叠、非空、原文精确相等，以及所有非空白正文均被覆盖；任一条件失败时 SHALL 拒绝整个切分结果，且无效结果中的单元 SHALL NOT 入库。

#### Scenario: 合法 span 被接受
- **WHEN** 语义切分器返回若干有序且不重叠的 span，并完整覆盖候选块非空白正文
- **THEN** 每个 Evidence Unit 的 `content` SHALL 精确等于对应 `candidate_content[start:end]`
- **AND** 单元的绝对 `source_span` SHALL 由候选块起点与局部 span 机械计算

#### Scenario: 改写文本或错误坐标被拒绝
- **WHEN** 模型输出的 span 越界、重叠、遗漏非空白正文，或声称的文本不等于原文切片
- **THEN** 系统 SHALL 拒绝该候选块的整个切分结果
- **AND** SHALL 返回可识别的切分验证错误而不是静默修补或写入部分单元

#### Scenario: 事实簇 few-shot 不改变 span-only 协议
- **WHEN** 系统向语义切分模型提供事实簇定义和 few-shot
- **THEN** 示例 SHALL 同时覆盖应合并与应拆分的边界
- **AND** 模型输出协议仍 SHALL 只包含坐标、atomicity、时态绑定和诊断字段，不得包含生成的 chunk 正文

### Requirement: 单元级时态元数据必须绑定到原文范围
系统 SHALL 为每个 Evidence Unit 独立解析 `version`、`published_at` 与 `effective_at`。解析优先级 SHALL 为单元正文锚点、最近 section/heading 锚点、文档级显式 metadata。来自正文或 heading 的每个值 SHALL 带有字段名、解析值、原文 anchor 文本及绝对 source span；代码 SHALL 验证 anchor 等于文档对应切片，且 anchor 位于该单元正文 span 内或该单元继承的 heading span 内。无合法锚点的模型值 SHALL 被拒绝，不得覆盖文档级 metadata。

#### Scenario: 同一文档的不同版本获得独立 metadata
- **WHEN** 一个文档在不同 heading 或正文片段下分别讨论 React 18 和 React 19
- **THEN** 对应 Evidence Unit SHALL 分别解析并保存自己的 version
- **AND** 每个 version binding SHALL 精确指向其正文或最近 heading 的原文 span

#### Scenario: 无锚点时态值不能入库
- **WHEN** 语义模型返回一个无法在单元正文或继承 heading 中精确定位的版本或日期
- **THEN** 系统 SHALL 拒绝该时态绑定
- **AND** SHALL NOT 把模型生成值静默写入 Evidence Unit

### Requirement: Evidence Unit 持久化原子性状态
新入库 Evidence Unit 的 `atomicity` SHALL 为 `atomic` 或 `indivisible`。快速路径、正常事实簇 span 和调用方确认的单条知识 SHALL 标记为 `atomic`；只有语义切分阶段明确判定“继续拆分会破坏独立可解释性”的复合事实簇才 MAY 标记为 `indivisible`。legacy 记录缺失该字段时 SHALL 保持缺失或映射为 `legacy_unknown`，不得伪造为 `indivisible`。

#### Scenario: 不可分复合事实被显式标记
- **WHEN** 一个候选包含互相依赖且继续拆分会失去含义的多个陈述
- **THEN** 语义切分结果 MAY 保留一个完整 span 并标记 `atomicity="indivisible"`
- **AND** 该状态 SHALL 随 Evidence Unit 持久化并进入 judge 白名单投影

#### Scenario: 普通快速路径不获得 partial 资格
- **WHEN** 一个候选通过确定性快速路径或单条兼容入口入库
- **THEN** 其 `atomicity` SHALL 为 `atomic`
- **AND** SHALL NOT 因内容包含多句话而自动标记为 `indivisible`

### Requirement: Evidence Unit 采用零正文 overlap 与语义优先的长度策略
相邻 Evidence Unit 的正文 span SHALL 不重叠。系统 SHALL 把 150–400 tokens 仅作为语义切分 prompt 的弱偏好和 benchmark 观测区间，把 600 tokens 作为唯一机械 hard max，且 SHALL NOT 设置硬性最小 token 数。运行时闸门 SHALL NOT 使用理想区间作强拆或强并条件。系统 SHALL 优先保持证据完整性和命题集中度；处于 hard max 内的完整事实簇 SHALL NOT 仅为满足理想区间而被强制拆分，超过 hard max 的候选块 SHALL 在入库前被合法切分，否则该候选块入库失败。

#### Scenario: 短公告保持独立
- **WHEN** 一条完整官方公告只有 60 tokens
- **THEN** 系统 SHALL 允许它独立成为 Evidence Unit
- **AND** SHALL NOT 为达到理想下限而与相邻主题合并

#### Scenario: 完整事实簇允许超过理想上限
- **WHEN** 一个语义完整、命题集中的 API 行为说明为 450 tokens
- **THEN** 系统 SHALL 保持其为一个 Evidence Unit
- **AND** SHALL NOT 仅因超过 400 tokens 而拆分

#### Scenario: hard max 是机械保险丝
- **WHEN** 候选 Evidence Unit 超过 600 tokens
- **THEN** 系统 SHALL 在入库前取得经验证的更细 span
- **AND** 若无法得到合法切分，SHALL 拒绝该候选块而不是写入超限单元

#### Scenario: 理想区间只产生观测数据
- **WHEN** 一个合法 Evidence Unit 少于 150 tokens 或处于 401–600 tokens
- **THEN** 系统 MAY 记录其长度分布或向语义模型表达弱偏好
- **AND** SHALL NOT 仅因偏离理想区间改变已确定的事实簇边界

### Requirement: 分层身份同时绑定来源、修订与原文位置
系统 SHALL 以规范化来源生成稳定 `document_id`，以 `document_id` 加文档内容哈希和声明的修订标识生成 `document_revision_id`，并以 `document_revision_id`、绝对 source span 与单元内容哈希生成 `chunk_id`。相同输入重复入库 SHALL 产生相同身份；相同正文来自不同来源、不同修订或不同原文位置时 SHALL 产生不同的 `chunk_id`。

#### Scenario: 同来源同修订幂等
- **WHEN** 同一规范化来源、相同文档正文和相同修订元数据被重复入库
- **THEN** 系统 SHALL 产生相同的 document、revision 与 chunk IDs
- **AND** SHALL 以幂等更新处理而不是复制一组新单元

#### Scenario: 同正文不同来源不覆盖
- **WHEN** 两个不同来源包含完全相同的证据正文
- **THEN** 它们 SHALL 拥有不同的 `document_id` 与 `chunk_id`
- **AND** 两条证据 SHALL 同时保存在同一用户知识命名空间中

#### Scenario: 新文档修订保留独立身份
- **WHEN** 同一来源的文档正文或声明版本发生变化
- **THEN** 系统 SHALL 创建新的 `document_revision_id`
- **AND** 新旧修订中的 Evidence Unit SHALL 可被区分和审计

### Requirement: 每个 Evidence Unit 独立评级
来源名称与来源链接 SHALL 从文档修订继承；文档级时间/版本字段 SHALL 作为无局部锚点时的默认值，合法的单元正文或 heading 时态绑定 SHALL 覆盖对应默认值。一手程度、领域契合、证据强度、命题针对性、claim domain、最终 level 与 level basis SHALL 针对每个 Evidence Unit 独立计算。一个文档中的不同单元 SHALL 可以得到不同的时间/版本、等级和 claim domain。

#### Scenario: 同文档不同命题获得不同评级
- **WHEN** 一个文档同时包含其权威领域内的事实和领域外观点
- **THEN** 两个 Evidence Unit SHALL 分别评级
- **AND** 它们 SHALL 可以得到不同的 `domain_fit`、claim domain 与最终等级

### Requirement: 文档批量入库先验证后持久化
文档批量入库 SHALL 在写入任何 Evidence Unit 前完成结构解析、span 验证、长度验证、身份计算和全部单元评级。解析或验证失败时 SHALL 返回结构化错误并保证该次文档修订没有新单元写入；成功时 SHALL 返回 document ID、revision ID、单元数量和有序 chunk IDs。

#### Scenario: 验证失败不产生半份修订
- **WHEN** 某个复杂候选块无法得到合法 span 切分
- **THEN** 该文档修订的批量入库 SHALL 失败
- **AND** 本次修订的任何 Evidence Unit SHALL NOT 被写入 Store

#### Scenario: 成功结果可审计
- **WHEN** 文档批量入库成功
- **THEN** 响应 SHALL 包含稳定的 document ID、revision ID 和按 `chunk_index` 排序的 chunk IDs

### Requirement: 原子知识兼容入口使用统一记录模型
现有单条知识入口 SHALL 保留，并把调用方提供的内容视为一个已确认的原子 Evidence Unit。该入口 SHALL 进行 hard max、原文与元数据校验并使用统一 Evidence Unit 存储形状和来源感知身份；它 SHALL NOT 再按纯正文哈希覆盖同正文的不同来源。缺少可识别来源时，系统 SHALL 使用稳定的本地原子知识来源身份保证重复提交幂等。

#### Scenario: 现有调用方式继续工作
- **WHEN** 调用方通过现有 API 或工具提交一条原子知识及来源
- **THEN** 系统 SHALL 返回一个可用于查询和 PATCH 的条目 ID
- **AND** 该条目 SHALL 以统一 Evidence Unit schema 存储并独立评级

#### Scenario: 无来源原子知识保持幂等
- **WHEN** 同一用户重复提交完全相同且没有来源信息的原子知识
- **THEN** 系统 SHALL 生成相同的稳定身份
- **AND** SHALL NOT 无限复制相同条目
