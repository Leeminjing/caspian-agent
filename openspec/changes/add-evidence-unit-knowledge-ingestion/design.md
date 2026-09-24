## Context

见 `proposal.md - Why`。当前知识路径有四个直接约束：

- `put_knowledge` 对整条 `content` 执行一次评级，以 `sha256(content)[:16]` 为 key，并把 `{content, level, source, source_url, provenance, level_basis}` 作为一个 Store value 写入。
- `langgraph_store.fields = ["$"]` 使整条 value 成为默认 embedding 输入；召回相关性因此可能受到等级、来源和评级理由影响。
- `EvidenceEntry` 没有文档、修订、结构、原文 span 或时间字段；`judge_conflicts` 只给模型 `id + content`。
- `judge` 与 `govern` 已具备 partial claim/span 的原文锚定和裁切机制，该机制必须保留，但新入库链应让 unit-to-unit full conflict 成为常规路径。

既有硬约束保持不变：四维软评级加确定性等级映射、L0 黑名单、未评级安全港、跨等级压制、同级不裁决、potential 不压制、temporal disjoint 双方保留、查询级零持久化和人工覆盖 CAS。

实施还有一个前置闸门：任何源代码编辑之前，必须先使用 Context7 核对当前安装/目标版本的 LangGraph Store 字段索引、逐条 `index` 参数、异步批量操作、向量更新与相同 key upsert 语义。Context7 可能以 MCP 或工具存在；若找不到或无法调用，实施必须立即停止并向用户询问，不得凭记忆继续。

## Goals / Non-Goals

**Goals:**

- 以 Evidence Unit 领域模型统一文档入库、原子知识入库、检索、评级与治理。
- 让所有正文都可机械追溯到输入文档的精确字符 span，LLM 只决定边界而不拥有正文。
- 让相关性向量、证据正文和权威 metadata 成为三个独立数据面。
- 用稳定、来源感知、修订感知和位置感知的身份消除同正文多来源覆盖。
- 通过小模块与纯函数隔离结构解析、边界验证、身份、检索文本、评级编排和 Store I/O。
- 保持旧 API、旧条目、治理算法与人工覆盖行为可用。

**Non-Goals:**

- 不负责 PDF、Word、网页或 OCR 的二进制提取；文档入口接收已经提取出的 Markdown 或纯文本及来源 metadata。
- 不对存量知识自动重切分或重新评级；旧记录通过兼容投影继续工作。
- 不做 query-time 重切分、query-time 重评级或基于查询动态合并 Evidence Unit。
- 不引入固定窗口正文 overlap，不用摘要或改写文本代替原文证据。
- 不改变等级映射、govern 的裁决规则、Decision Table 或其它治理子系统。

## Decisions

### D0: Context7 是 apply 的硬前置条件

apply 的第一个动作只做 Context7 发现和文档查询，至少确认：

1. LangGraph Store index 配置的 field path 语义；
2. `aput` 的 per-item index 覆盖语义；
3. 异步 batch API 的输入、顺序、失败与 upsert 行为；
4. 已存在 key 更新 value 时向量如何重算；
5. 当前版本对嵌套/顶层字段路径的支持。

结论要记录到 change 的 implementation evidence。找不到 Context7 时不创建或修改任何 Python、YAML、API 或测试文件。

Rationale：`fields: ["$"]` 正是本变更要修复的边界，若对 Store 索引契约理解错误，会让设计表面分层、运行时仍把整条 JSON 送入 embedding。

Alternative rejected：直接依据记忆修改 `fields` 或 `index`；这无法证明与仓库实际依赖版本一致。

### D1: 领域值、切分、模型适配、编排和存储分成独立模块

新增或调整下列职责边界：

- `knowledge/evidence.py`：只定义文档输入、来源 span、候选块、Evidence Unit draft/final、批量结果等不可变领域值及字段校验。
- `knowledge/identity.py`：只提供来源规范化和 document/revision/chunk ID 纯函数。
- `knowledge/chunking.py`：只负责 Markdown/纯文本结构预切分、保守触发的原子性快速闸门、hard-max 策略和 span 集合机械验证；公开核心函数短小，解析 helpers 使用模块级受保护函数。
- `knowledge/segmentation.py`：只封装事实簇语义模型调用、prompt/few-shot 与结构化 span/atomicity/时态绑定输出解析；一个小型模型适配类实现单一 `split` 职责，原文校验仍留在纯函数中。
- `knowledge/temporal.py`：只负责 heading/正文时态 anchor 的机械校验、优先级解析与单元级 metadata 绑定；不调用 Store 或评级器。
- `knowledge/retrieval_text.py`：只负责确定性构造 `retrieval_text`，显式白名单允许进入 embedding 的字段。
- `knowledge/ingestion.py`：提供文档级 `put_document` 编排，顺序调用上述模块、现有评级器和 Store client；不承载具体解析、哈希或向量细节。
- `knowledge/store_client.py`：继续作为 Store I/O 与 legacy 投影边界；`put_knowledge` 变为原子 Evidence Unit 兼容入口并委托统一编排/写入函数。
- `knowledge/schemas.py`：保留查询、judge 和 governance 输出协议；`EvidenceEntry` 扩展非权威 metadata，但不吸收文档切分实现类型。

不创建一个同时解析文档、调用模型、评级、生成 ID 和写 Store 的 `ChunkingService` 或 `KnowledgeService` 大类。需要依赖注入的模型适配器使用小类；其余无状态逻辑优先使用纯函数。核心方法只编排一个职责，helpers 使用 `_` 前缀；确需供多个模块复用的纯逻辑才作为公开函数，类级无实例依赖的 helper 才使用 static/class method。

Rationale：这些模块的变化原因不同。结构解析随输入格式变化，模型适配随 provider 变化，身份与 retrieval text 必须纯且稳定，Store 行为随 LangGraph 版本变化，评级和治理已有独立生命周期。

Alternative rejected：把全部逻辑继续堆进 `store_client.py`；它会同时拥有文本理解、领域建模、评级和持久化，无法独立验证边界。

### D2: 所有输入先形成绝对坐标的结构候选块

文档入口使用一个不可变 `DocumentInput`，包含正文、格式、来源身份、标题、声明版本和时间字段。结构预切分只读取原文并输出：

```text
CandidateBlock {
  kind,
  content,
  source_span: {start, end},
  section_path,
  section_refs: [{title, source_span}],
  structural_index
}
```

Markdown 扫描器是确定性的单遍状态机：维护带绝对 span 的 heading stack，并把 paragraph、list、table、fenced code block 作为结构块。候选 `content` 始终通过原文绝对 span 切取得到；解析器不对空格、标点、列表符号或代码做格式化。纯文本走段落扫描器。标题用于 `section_path` 与时态 binding 上下文，不复制进 evidence `content`。

Rationale：先缩小模型面对的范围，同时保留可追溯位置；结构天然边界比 token window 更稳定。

Alternative rejected：把整篇文档交给 LLM 直接输出 chunks；这增加成本，并使原文完整性只能依赖模型自律。

### D3: 原子性快速闸门只对高置信多主题信号触发

快速闸门返回带 reason 的 `AtomicityGateDecision`，而不是把句子数等同于事实簇数。以下条件触发语义切分：超过 hard max；一个结构候选内存在多个可独立治理的列表/表格单元；或出现可由测试锁定的明确主题转换。句子数、句末标点数、分号数和代码声明数仅可作为诊断，不得单独触发模型调用。hard max 内且未命中高置信信号的候选直接通过并标记 `atomic`。

token 计数以独立 `TokenCounter` callable 注入。600 hard max 用于闸门与最终机械验证；150–400 ideal 只进入语义 prompt 的弱偏好和 benchmark 分布统计，不进入 pass/fail 分支，不设置 hard min。

Rationale：事实簇可能天然由多句话或多个相互依赖的声明组成。把标点数量当主题数量会无谓增加 LLM 调用，并提高错误切分概率。

Alternative rejected：无法确定就一律切分；它仍把低置信启发式变成昂贵且不稳定的模型路径。

### D4: 模型只返回边界，代码拥有证据正文

语义切分结构化输出只包含 ordered spans、每个 span 的 `atomicity`、可选时态 bindings 与 reason，不包含生成后的 chunk 文本。prompt 先定义“可独立评级和治理的最小完整事实簇”，再提供四类固定 few-shot：多句同事实簇合并、不同主题拆分、依赖上下文不拆、不同版本拆分并绑定 metadata。机械验证器执行：

1. 坐标为整数且满足 `0 <= start < end <= len(candidate)`；
2. span 严格升序且不重叠；
3. 每个 `candidate[start:end]` 非空；
4. span 之间及两端未覆盖区域只能是空白或结构分隔；
5. 每个切片通过 token hard max；
6. 转换到文档绝对坐标后再次验证 `document[start:end] == unit.content`；
7. atomicity 只能为 `atomic` 或 `indivisible`；
8. 每个正文或 heading 时态 binding 的 anchor 都能由文档绝对 span 精确还原，且属于当前单元或其继承 heading。

任一验证失败，整个候选切分失败。编排器在任何 Store 写入前验证所有候选，因此不会因模型返回部分正确结果写入半份文档修订。无固定 overlap；相邻正文 span 不重叠。

Rationale：与现有 judge partial span 的“软识别、硬锚定”一致，但把该边界提前到知识入库。

Alternative rejected：让模型返回摘要或重写后的 chunks；它会失去证据原文和可审计 span。

### D4a: 单元级时态 metadata 使用可审计 binding

结构扫描器除 `section_path` 外还保留每级 heading 的标题与绝对 span。新增 `TemporalBinding` 值对象：

```text
field: version | published_at | effective_at
value: 供检索和 judge 使用的解析值
anchor_text: 原文精确片段
source_span: 文档绝对半开区间
source_kind: content | heading | document
```

正文或 heading binding 必须满足 `document[source_span] == anchor_text`。正文 anchor 必须位于 unit span 内；heading anchor 必须属于该 unit 的 section refs。文档请求显式 metadata 使用 `source_kind=document` 且不伪造 span。解析优先级是 content、最近 heading、document，字段逐项解析，因此同一文档的 React 18 与 React 19 单元可以拥有不同 version。

语义模型只负责指出候选 span 内的字段、值与 anchor；代码负责坐标转换、归属校验和优先级合并。无合法 anchor 的模型 metadata 整体拒绝，不从模型自由文本猜值。

Rationale：单元级时间/版本若继续从 Document 整体继承，judge 虽然看见 metadata，也无法区分同文档内部的多个版本。

Alternative rejected：只把 heading 文本拼进 retrieval text 而不存 binding；它不可审计，也无法稳定投影给 judge。

### D5: Evidence Unit 身份由规范化来源、修订内容和绝对位置派生

身份使用完整 SHA-256 的规范化输入并输出稳定、带类型前缀的 ID：

```text
document_id = hash("document\0" + canonical_source)
revision_id = hash("revision\0" + document_id + content_hash + normalized revision metadata)
chunk_id    = hash("chunk\0" + revision_id + start + end + content_hash)
```

`canonical_source` 的优先级为：调用方稳定 external source ID、规范化 URL、规范化来源名。文档批量入口要求至少存在一种来源身份。URL 规范化只做不改变资源语义的操作：scheme/host 小写、移除 fragment、规范默认端口和空 path；不擅自删除或重排有语义的 query。

兼容 `put_knowledge` 在没有任何来源时使用 `manual-content:<content_hash>` 作为稳定本地来源，因此同一无来源正文仍幂等；只要来源不同，相同正文就获得不同 document/chunk ID。

Rationale：Store key 表示证据身份，而非正文去重键。正文相同不代表 provenance、权威或时间相同。

Alternative rejected：继续只哈希 content；它会把“多来源印证”折叠成一次覆盖。

### D6: `content`、`retrieval_text`、governance metadata 采用显式白名单隔离

最终 Store value 使用 `schema_version` 与 `record_type="evidence_unit"` 标识新格式。`content` 保持原文切片；`atomicity` 与 `temporal_bindings` 作为治理/审计 metadata 持久化；`retrieval_text` 以稳定模板拼接解析后的单元级非空字段：

```text
Title
Section path
Version
Published at
Effective at
Content
```

模板不包含 source/source URL、level、level basis、provenance、相似度、来源数量或任何治理输出。Store 全局 index fields 改为顶层 `retrieval_text`，新写入同时显式声明只索引该字段；两处行为以 Context7 核验结果为准并由捕获 embedding 输入的测试锁定。

更新 provenance 或人工 level override 时，value 可更新但检索文本保持不变，并显式避免不必要的向量重算；具体 Store 调用形式由 D0 确认。

Rationale：相关性与权威性是正交轴，字段白名单比“排除若干已知字段”更不易随 schema 扩展泄漏治理信息。

Alternative rejected：仍索引 `$` 再要求 embedding 忽略 metadata；序列化文本已经污染输入，模型无法保证忽略。

### D7: 文档级编排先完成全部 draft 与评级，再统一写入

`put_document` 的固定阶段为：

```text
validate input
→ canonicalize source and compute document/revision IDs
→ structural candidates
→ atomicity gate / semantic spans
→ validate exact spans, atomicity and temporal bindings
→ resolve unit-level temporal metadata and token limits
→ build retrieval text and chunk IDs
→ independently rate every draft
→ construct final Store values
→ batch write
→ return ordered result
```

解析、span 或长度失败发生在任何写入之前。评级沿用现有失败落未评级的安全港，因此单个评级器故障不会使整份文档失去可存储性。Store batch 自身是否具备事务性由 Context7 确认；若库不提供事务保证，持久化错误必须明确报告已完成/未完成 IDs，不能返回伪成功，但不在应用层伪造一个跨 Store 事务抽象。

Rationale：把“构造有效证据集”和“持久化”分开，避免模型验证失败产生半份修订，同时不假装底层拥有未提供的事务能力。

Alternative rejected：每生成一个 chunk 立即写入；后续 chunk 验证失败会留下不可识别的半份修订。

### D8: `put_knowledge` 是已原子化输入的薄兼容层

现有函数、REST 请求和 agent 工具保留参数与返回形状。兼容层把一条 content 包装为单文档、单修订、单 Evidence Unit，执行 hard max、ID、retrieval text、评级和统一写入，但跳过结构/语义切分。超过 hard max 时返回明确验证错误，调用方需使用文档入口。

现有 `PATCH /api/knowledge/{id}` 继续以 opaque ID 工作。新格式更新仅改变 provenance/level 字段；旧格式按当前 CAS 行为处理。查询投影集中在一个 adapter：新格式读取全部 metadata，旧格式映射为 `legacy=True` 且新增字段为空。

Rationale：兼容入口表达的是“调用方确认这就是一个原子知识”，不应在背后重新猜测文档边界。

Alternative rejected：删除或改变 `put_knowledge` 为批量返回；这会同时打破工具、REST 和既有测试。

### D9: judge 输入使用独立的非权威投影

新增纯函数把 `EvidenceEntry` 投影成 judge payload，只允许：`id`、`content`、`title`、`section_path`、`version`、`published_at`、`effective_at`、`atomicity`。不得使用 `model_dump()` 直接发送整个对象，因为未来新增字段可能把 level 或 provenance 泄漏给 judge。

partial claim/span 继续以 Evidence Unit 自身 content 为局部坐标验证，但资格由代码判定：逐侧检查 claim span 是否为去除首尾空白后的正文真子区间；真子区间所在侧必须为 `atomicity="indivisible"`。atomic、legacy_unknown 或缺失状态不具备 partial 资格；违规关系降为 potential、清空 spans。双方都是完整覆盖时改写为 full。govern 只消费经过资格归一化的关系与内部候选 level；score 与 metadata 均不进入等级比较。

Rationale：显式投影把语义冲突检测和权威裁决的隔离变成代码结构，而不是 prompt 约定。

Alternative rejected：把完整候选对象交给 judge 并在 prompt 中要求忽略 level；模型仍然看见权威信息。

### D9a: 原子性状态是 partial suppression 的代码门禁

自动入库快速路径与普通事实簇 span 标记为 `atomic`；segmenter 只有在继续拆分会破坏独立解释时才能返回 `indivisible`，并由事实簇金标语料覆盖。`put_knowledge` 表示调用方确认原子性，因此固定为 `atomic`。legacy 读取映射为 `legacy_unknown`，不伪造 partial 资格。

Judge prompt 仍提示优先 full，但可信边界不依赖 prompt。归一化阶段在构造 `ConflictRelation` 前执行 atomicity/span 资格校验；不合格 partial 无法到达 governance 的裁切分支。

Rationale：原文锚点只能证明 claim 来自正文，不能证明该单元确实不可继续拆分。显式状态和机械资格检查把“partial 仅兜底”从提示词约定提升为代码不变量。

Alternative rejected：继续接受所有合法 partial spans；这会让粗切分单元长期依赖运行时字符串手术。

### D10: API 只增加文档入口，不复制业务逻辑

新增 `POST /api/knowledge/documents`，请求包含正文、格式、来源身份、标题、版本与时间 metadata，响应包含 document/revision IDs、chunk 数量和有序 chunk IDs。路由只做 Pydantic 边界校验、身份/Store/model 依赖取得与错误映射，核心流程只调用 `knowledge.ingestion.put_document`。

现有 `POST /api/knowledge`、列表、PATCH 和 query 保持可用；列表/query 响应在不删除旧字段的前提下增加 Evidence Unit metadata。

Rationale：REST 和 agent 工具不能各自维护一套 chunking 或评级逻辑。

Alternative rejected：直接把结构切分写在 router；它无法被工具、测试或未来导入器复用。

### D11: 源码注释只使用文件头声明式契约

每个新增或职责发生变化的 Python 文件，文件头 SHALL 有且仅有一个声明式模块 docstring，明确列出：

- 本文件对外提供的类或函数；
- 每个公开入口的输入及含义；
- 输出及含义；
- 具体工作流；
- 最小示范例子。

实现体内部不写叙述性步骤注释；只有局部不变量无法通过命名、类型或拆函数表达时才保留必要注释。修改既有文件职责时同步更新文件头契约。核心函数和方法保持短小；helpers 使用受保护命名，公开复用 helper 才提升为公开纯函数，类内无实例依赖的 helper 才使用 static/class method。

Rationale：声明式文件契约与模块边界同步，避免实现步骤注释随重构失真。

Alternative rejected：在函数体逐行解释流程；这会重复代码并形成维护负担。

## Risks / Trade-offs

- [语义切分模型可能漏掉边界或返回错误 span] → 全覆盖、精确子串、顺序、不重叠和 hard-max 机械验证；失败时整个候选拒绝。
- [文档逐单元评级增加入库成本和延迟] → 确定性快速闸门避免对所有短块调用切分模型；评级仍是入库时一次，不进入查询热路径。
- [零 overlap 可能降低缺少上下文的短正文召回] → 以 title、section path、version 与时间构造 retrieval text，正文不复制。
- [结构解析无法覆盖所有 Markdown 方言] → 首版只保证标题、段落、列表、表格、fenced code block；未知结构保持原文候选并走保守语义切分。
- [600-token hard max 可能拒绝无法安全拆分的长表格或代码] → 返回明确验证错误，不通过重写或截断伪造证据。
- [新旧记录并存使读取路径更复杂] → 所有 legacy 兼容集中在 Store adapter，不把版本分支扩散到 judge、govern、router。
- [底层 Store batch 可能不具备事务性] → 写前完成全部模型与机械验证；按 Context7 确认的真实失败语义报告结果，不构造虚假事务保证。
- [ID 算法变化导致现有调用者无法预测新 ID] → API 始终把 ID 作为 opaque 返回；旧 ID 不重写且继续可 PATCH/查询。
- [时态提取模型可能生成无依据值] → 只接受能绑定到正文或 heading 精确原文 span 的 metadata；否则使用显式文档默认值或保持为空。
- [indivisible 状态仍含语义判断] → 用金标边界语料、few-shot 和持久化状态提高可审计性；最终 partial eligibility 由代码机械校验。
- [150–400 观测指标可能被误用为质量门槛] → 报告明确标记为 telemetry，pass/fail 只由 600 hard max 和语义/span 不变量决定。

## Migration Plan

1. apply 开始先完成 D0 Context7 核验并记录证据；不可用则停止。
2. 新增领域值、身份、切分、retrieval text 与语义 span 适配模块及其纯函数测试，不接入生产写路径。
3. 扩展 EvidenceEntry、atomicity、temporal bindings 与 Store adapter，先使新旧格式读取测试同时通过。
4. 调整 Store 索引配置并用捕获 embedding 输入的测试证明只有 `retrieval_text` 被索引。
5. 实现文档编排和新 REST 入口，再把 `put_knowledge` 改为统一原子兼容层。
6. 扩展 judge 非权威投影与 partial eligibility 门禁，再扩展事实簇边界 benchmark、API 响应和回归测试。
7. 运行 targeted tests、完整非 live pytest、benchmark 机械臂、格式/编译检查和 `openspec validate --strict`。

部署不执行破坏性数据迁移。新写入采用 Evidence Unit schema；旧记录继续原样存在并经 legacy adapter 读取。回滚时旧代码会忽略新 value 中的额外字段，基础 `content/level/source/source_url/provenance/level_basis` 仍保留；新 ID 仍作为普通 Store key 可访问。
