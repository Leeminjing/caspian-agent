## 1. Apply 前置证据与基线

- [x] 1.1 在修改任何源代码、配置或测试前，搜索并调用可用的 Context7 MCP 或工具；若找不到或调用失败，立即停止 apply 并询问用户，验证方式是 implementation evidence 记录所用 Context7 入口及成功响应摘要。
- [x] 1.2 通过 Context7 核对当前目标版本 LangGraph Store 的 field-path index、`aput` per-item index、异步 batch、相同 key upsert、value 更新与向量重算语义，验证方式是把准确 API 契约、版本和文档结论写入 implementation evidence。
- [x] 1.3 通过 Context7 核对当前 LangChain/模型栈可用的 token 计数接口，选定单一 `TokenCounter` 实现并记录 150–400 ideal、600 hard max 所采用的计数语义，验证方式是针对中英文和代码文本的确定性计数测试通过。
- [x] 1.4 运行现有 knowledge、judge、governance、store client、API 与 benchmark 非 live 测试并记录基线，验证方式是保存通过数与任何既有失败清单，确保后续回归可区分。

## 2. Evidence Unit 领域模型与稳定身份

- [x] 2.1 新增 `knowledge/evidence.py`，定义不可变的文档输入、source span、候选块、Evidence Unit draft/final 和批量结果模型，并按声明式要求写文件头 docstring；验证方式是 schema 序列化、空值、span 与必填身份测试通过。
- [x] 2.2 新增 `knowledge/identity.py`，实现 canonical source、document ID、revision ID 与 chunk ID 纯函数，helpers 使用受保护命名且不持有 Store/模型依赖；验证方式是相同输入幂等、不同来源/修订/span 不同 ID 的表驱动测试通过。
- [x] 2.3 明确 URL、external source ID、来源名和无来源原子知识的规范化优先级，验证方式是 fragment、默认端口、query、大小写、空来源及 `manual-content` 测试锁定结果。
- [x] 2.4 新增 `knowledge/retrieval_text.py`，以字段白名单稳定渲染 title、section path、version、published/effective 时间和 content；验证方式是 level、level_basis、provenance、source/source_url 改变时输出完全不变。

## 3. 确定性结构切分与长度闸门

- [x] 3.1 新增 `knowledge/chunking.py` 的 Markdown 单遍结构扫描，识别 heading、paragraph、list、table 与 fenced code block 并保存绝对 span/section path；验证方式是混合 Markdown fixture 的块类型、顺序、路径和 `document[start:end] == content` 全部成立。
- [x] 3.2 实现纯文本段落预切分并保证不规范化正文，验证方式是 CRLF/LF、连续空行、Unicode 与首尾空白 fixture 的绝对 span 精确测试通过。
- [x] 3.3 实现保守原子性快速闸门，将短单结构候选直接通过、多结构/明显并列/超长/不确定候选送入语义切分；验证方式是单事实、事实簇、多主题列表、多段落和长块用例覆盖两条分支。
- [x] 3.4 接入经 Context7 选定的 `TokenCounter`，统一用于快速闸门与最终 hard-max 验证；验证方式是 60-token 短公告保留、450-token 完整事实簇保留、601-token 候选必须切分或失败。
- [x] 3.5 实现零正文 overlap 与有序 chunk index 检查，验证方式是所有生成单元的绝对 span 单调、不重叠且正文不重复。

## 4. 语义 span 切分与机械验证

- [x] 4.1 新增 `knowledge/segmentation.py` 的结构化 span 输出 schema 与单一职责模型适配器，模型只返回 `{start,end}` 和诊断信息；验证方式是捕获模型请求/响应证明没有生成 chunk content 字段。
- [x] 4.2 在 `knowledge/chunking.py` 实现纯 span 验证：整数范围、升序、不重叠、非空、非空白全覆盖、精确原文和 hard max；验证方式是每类非法输出都有拒绝测试。
- [x] 4.3 将局部候选 span 机械转换为文档绝对 span并二次验证原文相等，验证方式是多级 heading 下的复杂候选可还原到原始文档精确字符位置。
- [x] 4.4 实现语义切分失败的 fail-hard 行为，不采用部分合法 spans、不摘要、不截断也不回退为超限整块；验证方式是越界、重叠、遗漏和超限模型输出均返回结构化验证错误且 Store 零写入。
- [x] 4.5 更新所有新增/修改切分文件的声明式文件头 docstring，确保仅文件头描述公开 API、输入、输出、工作流和例子；验证方式是人工检查和针对旧职责描述的文本搜索均通过。

## 5. 文档级入库编排与逐单元评级

- [x] 5.1 新增 `knowledge/ingestion.py` 的 `put_document` 核心编排，严格按输入验证、身份、结构候选、原子/语义切分、全量机械验证、retrieval text、逐单元评级、批量写入和有序结果执行；验证方式是端到端 fake store/model 测试锁定调用顺序。
- [x] 5.2 在任何 Store 写入前完成整份修订的所有解析和 span/长度验证，验证方式是后置候选失败时 fake store 的写调用次数为零。
- [x] 5.3 对每个 Evidence Unit 独立调用现有四维评级与硬映射，继承文档来源/时间字段但不共享 claim-relative 结果；验证方式是同文档两个单元可产生不同 claim domain、dimensions 和 level。
- [x] 5.4 保留评级失败落未评级安全港及 L0 黑名单短路，验证方式是单个单元评级异常仍生成 level=None/basis，黑名单单元不调用评级模型。
- [x] 5.5 使用 Context7 已确认的 Store batch/upsert API 写入统一 `schema_version` / `record_type=evidence_unit` value，并正确报告底层部分失败语义；验证方式是成功、同 revision 重放和注入写失败测试通过且不返回伪成功。
- [x] 5.6 让现有 `put_knowledge` 成为单 Evidence Unit 薄兼容层，保持参数和 `(id, level)` 返回形状但使用来源感知身份；验证方式是既有工具/API 测试继续通过、同正文不同来源不再覆盖、无来源相同正文仍幂等。
- [x] 5.7 对原子兼容入口实施 600-token hard max，验证方式是超限输入返回明确错误并指向文档入口，hard max 内输入不触发语义切分。

## 6. Store 索引隔离与 legacy 读取

- [x] 6.1 将 LangGraph Store 全局索引字段从整条 value 改为顶层 `retrieval_text`，并按 Context7 契约让新写入显式只索引该字段；验证方式是捕获 embedding adapter 的原始输入与 `retrieval_text` 完全一致。
- [x] 6.2 增加索引隔离回归测试：只改变 level、level_basis、provenance 或来源数量时 embedding 文本不变，改变 title/section/version/content 时按模板变化。
- [x] 6.3 调整人工 level/provenance 更新路径，保持 `retrieval_text` 不变并依据 Context7 契约避免无意义向量重算；验证方式是 PATCH CAS 测试与 embedding 调用计数测试通过。
- [x] 6.4 在 Store client 集中实现新旧 value 到 `EvidenceEntry` 的投影，legacy 条目新增字段为空并带 legacy 标记；验证方式是新旧混合命名空间的 list/search/PATCH 测试全部通过。
- [x] 6.5 扩展知识列表与查询序列化而不删除现有字段，验证方式是旧前端/测试依赖的 `id/content/level/source/source_url` 仍存在，新记录同时返回追踪 metadata。

## 7. Judge 输入隔离与治理回归

- [x] 7.1 扩展 `EvidenceEntry` 的 document/revision/chunk、title、section path、span、version 和时间字段，并更新文件头声明式契约；验证方式是 Pydantic 新旧输入兼容测试通过。
- [x] 7.2 新增 judge 专用白名单投影，只发送 `id/content/title/section_path/version/published_at/effective_at`，禁止直接 `model_dump()` 整个候选；验证方式是捕获 payload 确认不含 level、score、level_basis、provenance、source count。
- [x] 7.3 更新 judge prompt 与 temporal-disjoint 测试，使正文未写版本但 metadata 不同的候选可判为时间/版本不相交；验证方式是结构化与纯文本 fallback 两条路径均覆盖。
- [x] 7.4 保持 full/potential/partial/temporal 及 span 锚定语义，验证方式是原子单元冲突走 full、不可分复合证据 partial 可裁切、未锚定 partial 降为 potential。
- [x] 7.5 运行现有 govern 全套回归，验证跨等级压制、同级不裁决、未评级独立、potential 不压制、temporal 双方保留和零持久化结果不变。

## 8. 文档 API 与错误协议

- [x] 8.1 在知识 router 新增文档批量入库请求/响应模型与 `POST /api/knowledge/documents`，路由只做边界校验和调用 `put_document`；验证方式是成功响应包含 document/revision IDs、count 和有序 chunk IDs。
- [x] 8.2 将 span、hard-max、缺失来源身份和模型切分错误映射为稳定 4xx 结构化响应，Store/内部错误保持明确 5xx；验证方式是每种错误分支的 API 测试通过且没有部分写入。
- [x] 8.3 保持现有 `POST /api/knowledge`、GET、PATCH 和 query 路由兼容，验证方式是原有 API 测试不改调用方式仍通过。
- [x] 8.4 更新知识 router、store client、schemas、judge 等职责变化文件的文件头 docstring，删除非必要实现体叙述注释；验证方式是逐文件检查公开入口、输入、输出、流程、例子与实际代码一致。

## 9. Benchmark 与完整性度量

- [x] 9.1 扩展 RAG benchmark schema/fixtures，加入同正文不同来源、同来源不同 revision、多命题文档、不可分 partial 证据和治理 metadata 变化场景；验证方式是语料加载器机械校验全部字段。
- [x] 9.2 增加身份碰撞、来源覆盖、正文 overlap、hard-max、非法 span 接受及 full/partial conflict 计数器，验证方式是正确 fixture 全为零违规、故障注入 fixture 精确触发对应指标。
- [x] 9.3 增加无真实 embedding/LLM 依赖的检索字段隔离臂，验证方式是治理 metadata 变化不改变捕获的 embedding 文本，但 govern 使用更新等级。
- [x] 9.4 更新 Markdown/结构化 benchmark 报告，保留既有错误信息采纳率和正确信息保留率并追加 Evidence Unit 完整性指标；验证方式是报告快照测试通过。

## 10. 文档、配置与最终验证

- [x] 10.1 更新 `config.yaml`、README 与知识 API 文档，说明 Evidence Unit、文档入口、零 overlap、长度策略、三轴隔离、legacy 行为及 opaque ID；验证方式是文档示例与实际请求/配置字段一致。
- [x] 10.2 审核所有新增和 materially changed Python 文件，确保文件头使用声明式 docstring，正文只有不可由命名/类型表达的不变量注释，且没有职责过大的类或过长核心函数；验证方式是逐文件审查记录映射到 design D1/D11。
- [x] 10.3 运行 Evidence Unit、identity、chunking、segmentation、ingestion、Store、API、judge、governance 和 benchmark targeted tests，验证全部通过且无真实网络调用。
- [x] 10.4 运行 `python -m pytest backend/packages/harness/tests -m "not live" -q`，验证完整非 live 套件无新增失败，并把既有环境失败与本变更失败分开记录。
- [x] 10.5 运行机械 benchmark、`python -m compileall`、仓库已有的格式/类型/静态检查以及 `git diff --check`，验证无完整性违规、语法错误或空白错误。
- [x] 10.6 运行 `openspec validate add-evidence-unit-knowledge-ingestion --strict` 并逐项核对 proposal/specs/design/tasks 与最终 diff，验证所有 requirement 均有实现和测试证据且无超范围改动。
