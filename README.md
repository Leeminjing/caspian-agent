# Caspian

**软硬兼施 · 离散等级治理 Agent** &nbsp;&nbsp;/&nbsp;&nbsp; **Soft and Hard, Hand in Hand — The Discrete-Level Governance Agent**

> An open-source super-agent built on LangGraph, governed by **discrete levels**: the uncertain LLM *detects and proposes*, the deterministic code *adjudicates and locks*, and a set of trivially-comparable integers is the only language they share.
>
> 一个基于 LangGraph 的开源超级 Agent。它的治理靠**离散等级**:不确定的 LLM 负责*感知与提案*,确定的机械代码负责*裁决与锁序*,而两者之间唯一通用、平凡正确的语言,是一组可比较的小整数。

---

## 为什么叫这个名字 / Why this name

Most agent systems are **homogeneous**: either "an LLM judging an LLM" (soft decides soft, so the verdict has no determinism), or "rules detecting and rules deciding" (hard detects hard, so it can't see semantics). Caspian welds the two **heterogeneously**: the LLM is the only thing that can do semantic work (detect conflicts, extract requirements, synthesize), and the code is the only thing that can be certain (compare levels, validate structure, lock ordering, cascade authority). Neither is complete without the other.

> 多数 agent 系统是**同质**的:要么"LLM 审 LLM"(软决软,裁决无确定性),要么"规则检测 + 规则裁决"(硬决硬,看不见语义)。Caspian 把两者**异性焊接**:LLM 是唯一能做语义工作的地方(检冲突、提要求、综合),代码是唯一能确定的地方(比等级、验结构、锁顺序、级联权威),二者互为缺失、缺一不可。

**The axiom / 设计公理:**

> 软触发,硬裁决。软把模糊判断坍缩成离散等级;硬裁决这些等级如何转移、谁获胜、沉淀成什么。二者是因果链上的两环,拆掉任何一环,系统都不成立。
>
> Soft for sensing, hard for adjudicating. The soft collapses fuzzy judgment into discrete levels; the hard decides how those levels move, who wins, and what they accumulate into. They are two links in one causal chain — remove either, and the system no longer holds.

---

## 设计公理的内核 / The Core Idea

A single rule reproduces itself across the whole system:

> **在每一处 LLM 边界,把自由度压缩成一个可机械校验的窄类型(离散等级是最典型的一种),然后用纯函数裁决、用版本化文件落账、用 CAS 管可变状态、用 fail-hard 守核心。**
>
> **Compress the LLM's freedom at every boundary into a narrow, mechanically-checkable type (discrete levels being the canonical case); then adjudicate with pure functions, persist with versioned files, guard mutable state with compare-and-set, and fail-hard at the core.**

This is why the front end is also zero-dependency and transparent, why the orchestration is non-LLM, and why every "hard" is ultimately the lifecycle of one protocol type.

---

## 架构总览 / Architecture

Caspian is a **parent-graph-with-child-graph** LangGraph system; the `lead_agent` runs as the reasoning & task-orchestration subgraph. A FastAPI gateway drives it over a single `POST /api/threads/{thread_id}/runs/stream` SSE channel.

```
┌────────────────────────────── FastAPI Gateway ──────────────────────────────┐
│  router (access)  →  services (orchestrate)  →  worker (execute)             │
│  AuthMiddleware · CSRFMiddleware · RunManager · StreamBridge · checkpointer   │
└──────────────────────────────────┬───────────────────────────────────────────┘
                                   │ SSE (metadata / events / interrupt /
                                   │  goal_state / commitment_messages / error)
                                   ▼
                     ┌──────────────────────────────┐
                     │   lead_agent  (LangGraph)     │
                     │   create_agent(make_lead_agent)│
                     │   middleware chain + tools +  │
                     │   system prompt + LeadAgentState│
                     └──────────────┬───────────────┘
                                    │  child / sibling graphs
        ┌───────────────┬───────────┴──────────┬────────────────┐
        ▼               ▼                       ▼                ▼
   commitment        knowledge               subagents        plugins / skills
   9-stage L0-L3     RAG judge→govern        delegation       hooks/tools
```

- **Storage**: `PostgreSQL + pgvector` for checkpoints (`PostgresSaver`) and the LangGraph Store (long-term memory, semantic search).
- **Streaming**: in-process `StreamBridge` → SSE; `RunManager` tracks run status; rollback snapshots via checkpointer.
- **Sandbox**: pluggable, selected via the `$CASPIAN_SANDBOX` env var (see `.env.example`). **Default is `AioSandbox`** (container-isolated, requires Docker) for untrusted agent runs; set `CASPIAN_SANDBOX=caspian.sandbox.local:LocalSandbox` for development-only local runs. `LocalSandbox` validates virtual paths (path containment, `..` traversal rejection, symlink escape rejection) and guards shell commands, but **does not provide OS-level isolation**. `AioSandbox` runs one container per `(user, thread)`, enables default seccomp + `no-new-privileges` (no `seccomp=unconfined`; the all-in-one image keeps its default capability set), applies pids/memory/CPU limits, and binds its control port to `127.0.0.1` only. Both carry path whitelists + shell command guards + a regex risk-audit middleware.
- **Frontend**: zero-dependency static UI, served by the same Python process (vanilla JS, hand-written CSS, 2 vendored libs: `marked` + `DOMPurify`). No npm, no build step, no framework.

> - **存储**:`PostgreSQL + pgvector` 承载 checkpoint(`PostgresSaver`)与 LangGraph Store(长期记忆、语义检索)。
> - **流式**:进程内 `StreamBridge` → SSE;`RunManager` 管理 run 状态;checkpointer 提供 rollback 快照。
> - **沙箱**:可插拔,经 `$CASPIAN_SANDBOX` 选择(见 `.env.example`);默认 `AioSandbox`(容器隔离、需 Docker,一个 `(user, thread)` 一容器:默认 seccomp + `no-new-privileges`(不关 `seccomp=unconfined`,all-in-one 镜像保留默认能力集)、pids/内存/CPU 上限、控制端口仅绑 `127.0.0.1`);`LocalSandbox` 仅限本地/开发,不做 OS 级隔离。两者均含路径白名单 + shell 命令守卫 + regex 风险审计中间件。
> - **前端**:零依赖静态 UI,由同一个 Python 进程直接服务(vanilla JS + 手写 CSS + 2 个 vendor 库 `marked`/`DOMPurify`)。无 npm、无构建、无框架。

---

## 三大硬机制 / The Three Hard Mechanisms

The three "hard" are not three separate features — they are the **produce / apply / remember** lifecycle of *one* discrete-level concept.

> 三大"硬"不是三个孤立 feature,而是*同一个*离散等级概念的**生产 / 使用 / 记忆**三段生命。

```
  produce (铸/生产)          apply (用/落地)           remember (存/记忆)
  ┌────────────────┐        ┌────────────────┐        ┌────────────────┐
  │ Hard ①         │        │ Hard ②         │        │ Hard ③         │
  │ 9-stage forced │        │ layered-       │        │ decision table │
  │ progression    │        │ suppression RAG│        │ (决策等级表)     │
  │ stage ∈ {1..9} │        │ L0..L3         │        │ priority 1/2/3  │
  │ forge AIMessage│        │ judge→govern   │        │ versioned,      │
  │                │        │                │        │ monotonic,      │
  │                │        │                │        │ cross-run inject│
  └────────────────┘        └────────────────┘        └────────────────┘
```

### Hard ① — 九阶段强制推进 / Forced 9-stage progression (the *forged* AIMessage)

The commitment layer's supervisor is **not an LLM**. It is a deterministic `StateGraph` that *fabricates* an `AIMessage` (with a synthetic `tool_call` to `delegate_with_review`) and routes it through a `ToolNode`. The LLM only ever runs *inside* the tool body (Worker + Evaluator); the assembly line — which stage, what next, when to stop — is pure code. Stage order is locked: `expected = state.stage + 1; stage != expected → invalid_stage`.

Because the supervisor is code, it **cannot be prompt-injected or persuaded** to skip or reorder a stage. The LLM attack surface is confined to a schema-gated content box.

> 承诺层的 Supervisor **不是一个 LLM**。它是一个确定性的 `StateGraph`,逐字节**伪造**一条 `AIMessage`(含指向 `delegate_with_review` 的合成 `tool_call`)交给 `ToolNode`。LLM 只出现在*工具函数体内*(Worker + Evaluator);而"哪个阶段、下一步做什么、何时停"的装配线是纯代码。阶段顺序被锁死:`expected = state.stage + 1; stage != expected → invalid_stage`。
>
> 因为 Supervisor 是代码,所以**无法被 prompt injection 说服或跳步/重排**。LLM 的攻击面被关进一个 schema 收口的盒子。

### Hard ② — 分层压制 RAG / Layered-suppression RAG

Knowledge retrieval is three-stage: **recall** (vector, level-blind) → **judge** (LLM detects conflicts) → **govern** (deterministic suppression). Once a level gap exists, the higher-authority evidence *vetoes* the lower one on that proposition — **similarity score, source count, and any rerank score cannot override it**. Same level is *not* adjudicated (`conflict_same_level`); a *potential* conflict is *not* suppressed. Suppression is query-level, proposition-level, explainable, and **zero-persistence** (it never mutates the knowledge base).

> 知识检索分三段:**召回**(向量,等级不参与)→ **judge**(LLM 检冲突)→ **govern**(确定性压制)。一旦存在等级差,高权威证据在*该命题*上**否决**低权威证据——**相似度、来源数量、任何 rerank 分数都不能翻盘**。同等级不裁决(`conflict_same_level`),potential 冲突不压制。压制是查询级、命题级、可解释、且**零持久化**(不改动知识库)。

### Hard ③ — 决策等级表 / The decision table

A thread-level, versioned ledger at `requirements/{thread_id}/decision-table.md` — the **single source of truth** for human-approved decisions. Each row carries a `priority` of `1/2/3`; the file is content-addressed by a sha256 version; it is injected into every run via a fixed message id (`decision-table`) and **in-place replaced** on version change (zero token when unchanged). The arbitration is **monotonic**: a new decision that *downgrades* a table entry is deterministically rejected; one that *upgrades* (or states no level) requires human confirmation. This erases requirement drift / scope creep — you cannot "persuade" a table, because it is an `int` comparison.

> 线程级、带版本的账本,存于 `requirements/{thread_id}/decision-table.md`——人已批准决策的**唯一事实源**。每行带 `priority 1/2/3`;文件以 sha256 版本内容寻址;每次 run 通过固定 id(`decision-table`)注入并**原位替换**(版本不变则零 token)。仲裁是**单调**的:新决策若**降级**表内条目即被确定性拒绝;若**升级**(或未定级)则须人工确认。这一下掐死了需求漂移/范围蔓延——你无法"说服"一张表,因为它是一段 `int` 比较。

---

## 离散等级 = 协议类型 / Discrete Levels as the Protocol Type

Every governed thing is collapsed into a **discrete token**; the code owns the **transition / adjudication** of that token; the LLM only owns the **content** inside it.

> 一切被治理的东西都被坍缩成**离散 token**;代码拥有这个 token 的**跃迁/裁决**权;LLM 只拥有它内部的**内容**。

| Component | Discrete token | Code owns | LLM owns |
|---|---|---|---|
| Commitment | `stage 1..9` | advance / reject jump | stage content |
| Decision table | `priority 1/2/3` | compare + arbitrate | semantic full-scan of conflicts |
| Knowledge RAG | `L0..L3` | suppress by level | detect conflict edges |
| Goal mode | `phase` + `revision` | CAS + transition | propose / declare done |
| Subagents | `status` enum | validate + ledger + caps | describe / analyze |
| Sandbox | path/shell verdict | whitelist + block/warn/pass | the command itself |

---

## 为什么"2 > 1"成立 / Why "2 > 1" is sound

The levels are deliberately chosen to be **coarse, ordinal, small integers** — so that "2 > 1" is *trivially true* and therefore safe to hand to mechanical code. Compare with what a continuous score, a fuzzy rank, or a heavy reranker would do: they don't make the decision *mechanically correct*, they only make it *possible to compare* while leaving the semantics opaque and the edge cases gameable.

> 等级被刻意做成**粗粒、有序、小整数**——于是"2 > 1"是**平凡真**,可以放心交给机械代码。相比之下,连续分、模糊档、或重型 rerank 并不能让裁决*机械正确*,只是让"能比",却把语义留成黑盒、把边界留成可擦边。

- **Sound / 平凡正确**: `new_priority >= table_priority` is a plain `int` comparison.
- **Auditable / 可读**: you read `1/2/3`, not a black-box score.
- **Ungameable / 不可辩驳**: a model cannot nudge `2` toward `3`, nor argue `2 > 3`.

The coarse level also forces an honest boundary: where the level gap is clear, the code adjudicates cleanly; where two evidence are **equal** level, the system **refuses to adjudicate** (`conflict_same_level`) and hands it off — because it deliberately has no finer granularity to pretend otherwise.

> 粗等级还逼出一条诚实边界:等级差明确时,代码干净裁决;两条**同级**证据时,系统**拒绝裁决**(`conflict_same_level`)并交还他人——因为它刻意没有更细的粒度去假装能裁。

---

## 能力地图 / Capabilities

- **承诺层 (Commitment)**: `/commit <指令>` 触发 9 阶段;Worker–Evaluator 审核;人工节点在阶段 3/5/6/7 强制暂停;输出 `task-contract` + 决策等级表。Context7 仅为该流程懒加载(普通对话零依赖)。
- **决策等级表**: 版本化、内容寻址、跨 run 注入 + 单调仲裁;`update_decision_table` 内置工具受机械校验约束。
- **分层压制RAG**: `add_knowledge` 带权威等级入库(官方文档=3/官方博客=2/普通博客=1/未定级=最低),`knowledge_query` 返回已治理证据。
- **目标模式 (Goal)**: 持久目标 + 自动跨 run 推进(`<goal_round>`);compare-and-set 修订;`active/paused/blocked/complete` 生命周期;authority 边界(直接人类回合 vs 精确 goal 回合)。
- **计划模式 (Plan)**: `/plan` 软引导 + `exit_plan_mode` 评审卡(Approve / Keep planning / Chat about it);刻意不强制、不隔离。
- **子代理 (Subagents)**: `task` 委托;委托账本从消息流确定性重建;并发/总额硬上限截断;状态契约枚举 + 结果 sha256。
- **插件 (Plugins)**: 注入注册表 + 单实现冲突检测 + 稳定顺序 + 依赖解析;接口分 tool / ordered-mutator / ordered-observer / service,失败策略 `skip`。
- **技能 (Skills)**: `skills/public` + `skills/custom`;`extensions_config.json` 启停;`describe_skill` 发现。示例:`docx`、`vision`。
- **沙箱 (Sandbox)**: 可插拔,经 `$CASPIAN_SANDBOX` 选择(见 `.env.example`);默认 `AioSandbox`(容器隔离,需 Docker,一个 `(user, thread)` 一容器:默认 seccomp + `no-new-privileges`(不关 `seccomp=unconfined`,all-in-one 镜像保留默认能力集)、pids/内存/CPU 上限、控制端口仅绑 `127.0.0.1`);`LocalSandbox` 为 **development-only** 本地受限执行器——校验虚拟路径(目录层级围栏、`..` 穿越拒绝、symlink 逃逸拒绝)与 shell 命令,但 **不提供 OS 级隔离**,仅用于本地/开发运行。两者均含虚拟路径白名单 `validate_subdir`、`resolve_path` 防越界、shell 五道防线 + regex `block/warn/pass` 审计;错误自动清洗真实路径。
- **上下文压缩**: 触发阈值 + 切点 + LLM 摘要 + 后置校验(fail-soft);被压消息入 `archive.jsonl` 存档。
- **工具错误收口**: `ToolErrorMiddleware` 统一捕获工具异常并回传 LLM。

---

## 技术栈 / Tech Stack

- **语言/框架**: Python · LangChain · LangGraph (parent/child graph, `create_agent`).
- **服务**: FastAPI (gateway) · in-process `StreamBridge` · SSE.
- **存储**: PostgreSQL 17 + pgvector · Alembic · LangGraph Store (async_postgres).
- **模型**: OpenAI-compatible (default `deepseek-v4-flash` via `caspian.models.deepseek`), pluggable.
- **外部能力**: MCP (Context7 docs, Playwright browser), vendored vanilla-JS frontend.
- **配置**: `config.yaml` · `extensions_config.json` · `.mcp.json`.

---

## 快速开始 / Quick Start

### 本地数据库 / Local database
- PostgreSQL container: `desktop-postgres-1` (PostgreSQL 17 + pgvector).
- Host/port: `127.0.0.1:7221`. App database: `caspian`; app role: `caspian`. Required extension: `vector`.
- App connection: `config.yaml` → `database.url`. Alembic: `backend/packages/harness/caspian/persistence/migrations/alembic.ini`.

Run migrations:
```powershell
cd backend/packages/harness/caspian/persistence/migrations
../../../.venv/Scripts/python.exe -m alembic -c alembic.ini upgrade head
```

### 本地启动 / Local start
- 沙箱后端由 `$CASPIAN_SANDBOX` 决定（`.env` / `.env.example`。默认 `AioSandbox` **需要 Docker**；无 Docker 的本地开发设为 `caspian.sandbox.local:LocalSandbox`）。
- **注意**：`CASPIAN_SANDBOX` 是**必填**环境变量（`config.yaml` 的 `sandbox.use: $CASPIAN_SANDBOX` 引用它）。未设置时配置加载会抛 `KeyError`、应用无法启动——务必先 `cp .env.example .env` 并确保 `.env` 里有该值。
- Windows psycopg requires a `SelectorEventLoop`; do not run `uvicorn` directly. Use the entry script:
```powershell
python run_dev.py
```
Chat history is persisted via `checkpointer.type: postgres` (PostgresSaver) and restored from `GET /api/threads/{thread_id}/messages` on refresh / session switch.

### 本地网页登录账户 / Local login
- Email: `2656226581@qq.com` (password stored only as SHA-256 + bcrypt hash; not recorded in any repo file).

---

## 目录结构 / Repo Layout

```
backend/
  app/gateway/            FastAPI shell (auth/CSRF, routers, services, static frontend)
  packages/harness/
    caspian/
      agents/             lead agent, commitment (9-stage), middlewares, plan, goal
      knowledge/          governed RAG (judge + govern)
      subagents/          delegation executor / registry / status-contract
      plugins/            injection registry, hooks, runtime
      sandbox/            local / aio (Docker), path & shell guards
      runtime/            runs (worker), checkpointer, stream-bridge, store
      persistence/        SQLAlchemy + Alembic migrations
skills/                   docx, vision (and more) as SKILL.md
knowledge/                governed knowledge artifacts
requirements/{thread}/   task-contract.md + decision-table.md per thread
config.yaml               models, tools, sandbox, commitment, goal/plan, compression, subagents
extensions_config.json    skills / MCP servers / plugins
run_dev.py                Windows-safe start entry
```

---

## 一句话 / In one line

> **软硬兼施,而且连顺序与裁决都由代码说了算:离散等级治理 Agent。**
> **Soft and hard, hand in hand — and even the ordering and the verdict belong to code: the discrete-level governance Agent.**

---

## Project Lineage and Development History

Caspian is an independently developed Agent project, and its early general-purpose Agent Harness has a clear engineering lineage with DeerFlow. During the initial infrastructure-building stage of the project, Caspian referred relatively deeply to DeerFlow's architecture and implementation design. Some subsystems directly followed DeerFlow's core design ideas and interface organization, including the Skills system, as well as some conventions related to state, tools, prompts, sandbox, runtime, and Agent assembly.

Caspian was not developed by forking the DeerFlow repository and then continuously modifying it. The project follows an OpenSpec-driven development approach, in which capabilities are incrementally broken down into specification, design, and tasks, and are then implemented and iterated in a separate codebase. Therefore, DeerFlow's influence on Caspian is mainly concentrated in the early construction of the Harness and in certain specific subsystems, and this cannot simply be equated with Caspian as a whole being derived from the DeerFlow source code.

As the project continued to develop, Caspian's development focus gradually moved away from DeerFlow's original direction. A large number of subsequent capabilities began to evolve independently around Caspian's own requirements, including new sandbox security boundaries, runtime and persistence extensions, and the discrete-level governance system, deterministic commitment-stage progression, and layered-suppression RAG mechanisms that were ultimately formed.

Therefore, a more accurate positioning of DeerFlow is that it is one of the important engineering foundations and design sources for Caspian's early Agent Harness, rather than the complete architectural source of the entire Caspian project. Caspian's subsequent main development direction, especially its discrete-level governance system, has formed technical goals and architectural priorities that are different from those of DeerFlow.

---

## 项目血缘与开发历史

Caspian 是一个独立开发的 Agent 项目，其早期通用 Agent Harness 与 DeerFlow 存在明显的工程血缘关系。在项目最初的基础设施建设阶段，Caspian 曾较深入地参考 DeerFlow 的架构和实现设计，其中部分子系统直接沿用了 DeerFlow 的核心设计思路与接口组织，例如 Skills 体系，以及部分 state、tool、prompt、sandbox、runtime 与 Agent 装配相关约定。

Caspian 并不是通过 fork DeerFlow 仓库后持续修改形成的。项目采用 OpenSpec 驱动的开发方式，将能力逐项拆分为 specification、design、tasks，再在独立代码库中实现和迭代。因此，DeerFlow 对 Caspian 的影响主要集中在早期 Harness 建设及部分具体子系统，而不能简单等同于 Caspian 整体由 DeerFlow 源码派生。

随着项目继续发展，Caspian 的开发重点逐渐脱离 DeerFlow 的原有方向。后续大量能力开始围绕自身需求独立演化，包括新的沙箱安全边界、运行时与持久化扩展，以及最终形成的离散等级治理体系、确定性承诺阶段推进、分层压制 RAG 等治理机制。

因此，DeerFlow 更准确的定位是：Caspian 早期 Agent Harness 的重要工程基础与设计来源之一，而不是 Caspian 整个项目的完整架构来源。Caspian 后续的主要发展方向，尤其是其离散等级治理体系，已经形成了与 DeerFlow 不同的技术目标和架构重点。
