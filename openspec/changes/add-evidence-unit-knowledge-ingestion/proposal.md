## Why

Caspian 当前把调用方传入的整条 `content` 直接作为一条知识记录：整段正文只获得一个向量、一个 claim domain 和一组权威等级，且同正文的不同来源会因内容哈希键相同而互相覆盖。与此同时，Store 的 `fields: ["$"]` 会把等级、来源和评级 provenance 一并送入 embedding，使相关性召回与权威治理没有真正解耦。

本变更把知识库的最小治理对象从任意长度文本升级为可追踪原文位置、可独立评级、可独立召回和冲突治理的 Evidence Unit（证据单元），并保持“软模型识别语义边界、机械代码验证与裁决”的 Caspian 核心边界。

## What Changes

- 新增文档级批量入库：记录 `document_id`、`document_revision_id`，并把一个文档产出为多个 Evidence Unit。
- 新增确定性的文档结构预切分，按 Markdown 标题、段落、列表、表格和代码块形成候选块，同时保存 `title`、`section_path` 与原文 span。
- 新增 Evidence Unit 原子性闸门：短且命题集中的候选块直接通过；多主题、多结构或超长候选块进入语义边界切分。
- 语义边界切分只允许模型返回原文 `{start, end}` span，代码必须验证 `content[start:end]`；模型不得改写、摘要或生成证据正文，非法或不完整切分不得入库。
- Evidence Unit 不使用正文 overlap；理想长度为 150–400 tokens、hard max 为 600 tokens，不设置硬性最小长度，证据完整性与命题集中度优先于理想长度。
- 将证据正文与检索文本分离：`content` 保持原文精确子串，`retrieval_text` 由标题、section path、版本/时间元数据与正文确定性组装；只有 `retrieval_text` 参与 embedding。
- 每个 Evidence Unit 独立执行现有四维软评级与确定性 L0–L3 映射；来源级字段从文档修订继承，claim-relative 字段按单元独立产生。
- 改用来源感知的分层身份：`document_id = hash(canonical_source)`，`document_revision_id = hash(document_id + revision identity/content hash)`，`chunk_id = hash(document_revision_id + source span + content hash)`；相同正文但不同来源保留为不同证据。
- 扩展证据 schema 与查询投影，使 judge 可见 `title`、`version`、`published_at`、`effective_at` 等语义/时态字段，但继续看不到 `level`、authority score 或 `level_basis`。
- 保留 partial conflict 与原文 span 裁切作为无法合理进一步拆分时的兜底能力，不改变跨等级压制、同级不裁决、potential 不压制及零持久化等治理语义。
- 保留 `put_knowledge()` 作为“调用方已明确提供一个原子知识”的兼容入口，但落库到统一 Evidence Unit 记录模型；存量知识条目继续可读和可查询。
- 扩展 RAG benchmark，覆盖同正文不同来源、文档修订、Evidence Unit 身份稳定性、检索字段隔离及 chunk-level 冲突治理。

## Capabilities

### New Capabilities

- `evidence-unit-ingestion`: 定义文档结构预切分、原子性闸门、span-only 语义切分、Evidence Unit schema、分层身份、零正文 overlap、逐单元评级与批量入库契约。

### Modified Capabilities

- `knowledge`: 将知识检索对象改为 Evidence Unit，规定 `content` / `retrieval_text` / governance metadata 三轴隔离，并扩展 judge 的时间、版本与结构上下文输入，同时保持权威等级对 judge 不可见。
- `benchmark-rag`: 增加 Evidence Unit 身份、同正文多来源、检索字段隔离与 chunk-level 治理的机械复验场景。

## Impact

- 主要影响 `caspian/knowledge/` 下的 schema、入库、评级、检索、judge 与治理投影，并新增独立的文档模型、结构切分、语义 span 切分、身份和批量编排模块。
- `POST /api/knowledge` 与 `add_knowledge` 保持原子知识兼容语义；新增文档批量入库服务/API，知识列表与查询响应增加 Evidence Unit 元数据。
- LangGraph Store 的向量索引字段从整条 JSON value 改为仅 `retrieval_text`；等级、评级依据、来源归属与 provenance 不参与 embedding。
- 知识条目 ID 从纯正文哈希升级为文档修订和原文 span 感知的 ID；现有 ID 仍可读取和用于 PATCH，不执行破坏性原地迁移。
- 测试覆盖结构边界、span 锚定、原子性、长度保险丝、无 overlap、身份稳定性、来源不覆盖、逐单元评级、索引隔离、judge 输入隔离、旧条目兼容和 benchmark 机械指标。
- 不新增必需的外部服务；语义切分复用现有可配置聊天模型。开始实现前必须先通过 Context7 核对 LangGraph Store 的字段索引与批量写入契约；Context7 不可用时停止实施并请求用户处理。
