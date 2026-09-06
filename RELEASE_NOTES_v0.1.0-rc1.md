# Caspian v0.1.0-rc1

> 软硬兼施 · 离散等级治理 Agent — Soft and Hard, Hand in Hand: The Discrete-Level Governance Agent

---

## What is Caspian

Caspian 是一个基于 [LangGraph](https://langchain-ai.github.io/langgraph/) 的开源超级 Agent。它的治理靠**离散等级**：不确定的 LLM 负责*感知与提案*，确定的机械代码负责*裁决与锁序*，两者之间唯一通用、平凡正确的语言是一组可比较的小整数。

设计公理：**软触发，硬裁决**——在每一处 LLM 边界，把自由度压缩成一个可机械校验的窄类型（离散等级是最典型的一种），然后用纯函数裁决、用版本化文件落账、用 CAS 管可变状态、用 fail-hard 守核心。

---

## Three Hard Mechanisms

三大"硬"不是三个孤立 feature，而是*同一个*离散等级概念的 **生产 / 使用 / 记忆** 三段生命。

```
  produce (生产)            apply (落地)             remember (记忆)
  ┌────────────────┐        ┌────────────────┐        ┌────────────────┐
  │ Hard ①         │        │ Hard ②         │        │ Hard ③         │
  │ 9-stage forced │        │ layered-       │        │ decision table │
  │ progression    │        │ suppression RAG│        │ priority 1/2/3 │
  │ stage ∈ {1..9} │        │ L0..L3         │        │ versioned,      │
  │ forge AIMessage│        │ judge→govern   │        │ monotonic       │
  └────────────────┘        └────────────────┘        └────────────────┘
```

- **Hard ① — 九阶段强制推进**：承诺层的 Supervisor **不是 LLM**，而是一个确定性的 `StateGraph`，逐字节伪造 `AIMessage`（含合成 `tool_call`）交给 `ToolNode`。LLM 只出现在 Worker / Evaluator 的工具函数体内；"哪个阶段、下一步、何时停"的装配线是纯代码，阶段顺序被锁死（`stage != state.stage + 1 → invalid_stage`）。无法被 prompt injection 说服跳步或重排。
- **Hard ② — 分层压制 RAG**：检索分三段——**召回**（向量，等级不参与）→ **judge**（LLM 检冲突）→ **govern**（确定性压制）。一旦存在等级差，高权威证据在*该命题*上否决低权威证据——相似度、来源数量、任何 rerank 分数都不能翻盘。同等级不裁决、potential 冲突不压制；压制是查询级、命题级、可解释、**零持久化**。
- **Hard ③ — 决策等级表**：线程级、版本化账本，`requirements/{thread_id}/decision-table.md` 是唯一事实源。每行带 `priority 1/2/3`；文件以 sha256 内容寻址，跨 run 原位注入（版本不变零 token）。仲裁**单调**：降级被确定性拒绝，升级/未定级需人工确认——你无法"说服"一张表，因为它是一段 `int` 比较。

---

## How to Run

### Docker 一键部署（推荐）

```bash
git clone <你的仓库> && cd <目录>
cp .env.example .env          # 填下面 3 个必填 key
docker compose up -d          # 首次约 3–5 分钟（构建应用镜像），之后秒起
# 打开 http://localhost:8000 → 登录（首次启动前先建账号，见下）
```

`.env` 必填（其余有默认）：

| 变量 | 说明 |
|---|---|
| `OPENAI_API_KEY` | 模型调用（DeepSeek） |
| `JWT_SECRET` | 登录 token 签名密钥 |
| `DASHSCOPE_API_KEY` | 知识库向量嵌入 |

> 首次使用前先建一个登录账号（尚无注册界面）：

```bash
docker compose exec -T caspian python - <<'PY'
import asyncio, uuid
from backend.app.gateway.auth.security import hash_password
from backend.app.gateway.models.user import User
from caspian.persistence.engine import init_engine, get_session
from caspian.config import get_app_config
EMAIL = "you@example.com"      # 改成你的邮箱
PASSWORD = "your-password"     # 改成你的密码
async def main():
    init_engine(get_app_config("config.yaml"))
    async with get_session() as s:
        u = User(id=uuid.uuid4(), email=EMAIL, password_hash=hash_password(PASSWORD), token_version=0)
        s.add(u); await s.commit()
        print("user created:", u.id)
asyncio.run(main())
PY
```

### 本地开发（Windows）

- PostgreSQL 容器 `desktop-postgres-1`（`127.0.0.1:7221`，库/角色 `caspian`，需 `vector` 扩展）。
- 跑迁移：
  ```powershell
  cd backend/packages/harness/caspian/persistence/migrations
  ../../../.venv/Scripts/python.exe -m alembic -c alembic.ini upgrade head
  ```
- **`CASPIAN_SANDBOX` 必填**；无 Docker 的本地开发设为 `caspian.sandbox.local:LocalSandbox`。
- 启动（Windows psycopg 需 `SelectorEventLoop`，勿直接 `uvicorn`）：
  ```powershell
  python run_dev.py
  ```

---

## Known Limitations

> **Experimental research release.**

- **冷启动门槛**：`docker compose up` 就绪只证明数据库 / checkpointer / store 层 OK；**首个触发沙箱的命令才会懒拉取约 13GB 的 all-in-one 沙箱镜像**。无 Docker 或网络/磁盘受限时该步骤会失败。
- **沙箱后端**：容器沙箱是默认后端；本地 / 无 Docker 需切换到 `LocalSandbox`，后者**不提供 OS 级隔离**。
- **数据库扩展**：迁移需 `CREATE EXTENSION vector`，依赖 DB 角色具有 CREATE 权限（compose 预设用户可用；受限角色会在 `alembic upgrade` 阶段失败）。
- **Skill 能力为 best-effort**：`docx` / `vision` 等公共 skill 依赖沙箱内 `/mnt/skills` 挂载可访问其脚本；当前该挂载源与公共 skills 真实位置不一致，这些 skill 属"尽力而为"，**不作为本版本的能力承诺**。
- **治理基准**：离散等级治理基准尚不能确立相对其他 agent 架构的普遍优越性。
