# Knowledge API

Caspian 以 Evidence Unit 作为独立召回、评级、冲突判断和压制的最小单位。ID 是 opaque 值，调用方只应保存和回传，不应解析其哈希格式。

## 原子知识入口

`POST /api/knowledge` 兼容既有调用方式。调用方明确保证 `content` 已经是一个原子证据单元；服务不会再次切分。正文不得超过 600 tokens，超限时使用文档入口。

```json
{
  "content": "Foo API 的默认值是 30。",
  "source": "Foo 官方文档",
  "source_url": "https://docs.example.com/foo"
}
```

响应为 `201`：

```json
{"id":"chunk_...","level":3,"level_display":"L3"}
```

相同正文、相同来源重复提交保持幂等；相同正文来自不同来源时保留为不同证据。无来源原子知识使用正文派生的稳定本地来源身份。

## 文档入口

`POST /api/knowledge/documents` 接收已经提取出的 Markdown 或纯文本，不负责 PDF、Word、网页或 OCR 提取。文档必须提供 `external_source_id`、`source_url` 或 `source` 中至少一项。

```json
{
  "content": "# React 19\n\nref 可以直接作为 prop 传递。\n\nHydration error 信息得到改进。",
  "format": "markdown",
  "external_source_id": "react-docs:19:ref",
  "source": "React 官方文档",
  "source_url": "https://react.dev/blog/2024/12/05/react-19",
  "title": "React 19",
  "version": "19",
  "published_at": "2024-12-05",
  "effective_at": "2024-12-05"
}
```

成功响应为 `201`：

```json
{
  "document_id": "doc_...",
  "document_revision_id": "rev_...",
  "count": 2,
  "chunk_ids": ["chunk_...", "chunk_..."]
}
```

处理顺序固定为：结构预切分 → 保守原子性闸门 → 必要时模型只返回原文 span、`atomicity` 与带锚点的时态 metadata → 代码验证完整覆盖、精确子串、顺序、零 overlap、时态 anchor 和 600-token hard max → 每单元独立评级 → 批量写入。

原子性闸门只在超过 600 tokens、明确主题转换或具有独立主题标签的结构项等高置信信号下调用切分模型。句子、句号、分号或代码声明数量本身不会触发切分；共同说明同一 API、行为、条件或因果的多句话保持一个事实簇。150–400 tokens 仅是模型 prompt 的弱偏好与 benchmark 观测区间，不参与运行时强拆、强并或合格判定；不设置硬性最小长度。

每个新单元保存 `atomicity=atomic|indivisible`。快速路径、普通事实簇和原子知识入口均为 `atomic`；只有继续拆分会破坏独立解释的复合事实簇可由语义阶段标为 `indivisible`。legacy 条目读取为 `legacy_unknown`。

`version`、`published_at`、`effective_at` 按“单元正文锚点 → 最近 heading 锚点 → 文档显式 metadata”逐字段解析。正文与 heading 值均保存 `anchor_text` 和绝对 `source_span`，无法精确还原原文的模型值会被拒绝。同一文档因此可以同时保存 React 18 与 React 19 单元，而不共享错误的文档级版本。

## 三个数据面

- `content`：原文精确子串，供评级、judge 和 govern 使用。
- `retrieval_text`：仅由 title、section path、version、published/effective 时间和 content 确定性组成，唯一参与 embedding。
- governance metadata：atomicity、temporal bindings、level、level_basis、provenance 等，只参与审计或权威治理，不进入 embedding。

正文与相邻单元不做 overlap。短正文所需上下文通过 `retrieval_text` 补充，不复制进 `content`。

## 查询、列表与更新

- `GET /api/knowledge` 返回既有字段以及 document/revision/chunk、section、span、version、时间、`temporal_bindings`、`atomicity` 和 `legacy` 标记。存量旧记录的 `atomicity` 为 `legacy_unknown`。
- `PATCH /api/knowledge/{id}` 保持既有 CAS 协议；更新 level/provenance 不改变 `retrieval_text`，也不触发无意义向量重算。新格式 Evidence Unit 的来源参与身份计算，因此不能通过 PATCH 原地修改 `source_url`，应按新来源重新入库；legacy 条目仍保留原有来源补录行为。
- `POST /api/knowledge/query` 仍执行向量召回 → 非权威冲突 judge → 确定性 govern。judge 可见版本、时间和 atomicity，但看不到 level、score、level_basis 或 provenance。只有 claim 为 `indivisible` 单元正文真子区间时，partial 才能进入治理；atomic、legacy_unknown、缺失状态或未锚定 partial 均机械降级为 potential，不触发压制。

文档身份/span/hard-max/切分错误返回结构化 `422`：

```json
{"detail":{"code":"span_gap","message":"spans 遗漏了非空白原文","details":{}}}
```

Store 写入失败返回 `500`，并明确给出底层可确认的 `completed_ids` 与 `pending_ids`；服务不会把失败响应伪装成成功，也不声明底层未提供的跨 Store 事务保证。
