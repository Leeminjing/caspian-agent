# Caspian v0.1.0-rc1 — 冷启动核查（Cold-Start Audit）

> 本文是**对照仓库实况**的核查记录，不是实现。
> 目标：判断 ChatGPT 建议的那份 RC 前置清单里，哪些是"已成立"，哪些是"臆测/模板项"，
> 并把真正会卡住"陌生用户冷启动"的项补上。
> 核查方式：只读代码 + 文档对照，附文件:行号。未运行真实容器/LLM，**凡未实测处均标注**。

---

## 结论（TL;DR）

**`docker compose up` 能起来 ≠ 一个新用户能跑通第一个任务。**

- ChatGPT 清单里约一半是"本就成立"的项（`.env.example`、默认 Docker 沙箱、隔离测试文件、建账号 snippet）。
- 清单里"LocalSandbox 路径漏洞修复"**未在代码中见到依据**。
- 真正会卡冷启动的项 ChatGPT 一条都没列：**readyz 探测不到 13GB 懒加载沙箱镜像**、**`/mnt/skills` 在沙箱内不含公共 skills**、**建扩展依赖超级用户**。

**决策（最新）**：`docx` / `vision` 定为**可有可无**。
→ 风险② **不阻塞本次发布**，作为 Known limitation + follow-up task 记录，不作为本版本的能力承诺。

---

## 一、核实为"已成立"的项（不必当待办）

| 一说 | 核实结果 | 依据 |
|---|---|---|
| `version="0.1.0"` | ✅ 属实 | `backend/packages/harness/pyproject.toml:3` |
| 建账号 snippet 是否对 | ✅ 与代码完全吻合 | `hash_password`(`app/gateway/auth/security.py:71`)、`User`(`app/gateway/models/user.py:45`)、`init_engine(AppConfig)`、`get_session()`（`persistence/engine.py`） |
| vector 扩展问题 | ✅ 迁移已建 `CREATE EXTENSION vector`（**但依赖 DB 角色有 CREATE 权限**） | `migrations/versions/a2c3e4f5d6b7_create_store_tables.py:58-65`（`compose:22-23` 注释已过时） |
| `.env.example` 完整 | ✅ 含 5 key + `$CASPIAN_SANDBOX`，默认 AioSandbox | `.env.example:4-21` |
| run_dev.py 的 Windows 事件循环 | ✅ 双层处理 | `run_dev.py:41-47`、`app.py:49-50` |
| compose 的 netns / DOCKER_HOST 设计 | ✅ 与 AioSandboxProvider 的 `localhost:port` 一致 | `docker-compose.yml:62,72` ↔ `local_backend.py:170` |
| 默认 = Docker 沙箱 | ✅ `.env.example` 与 Dockerfile/compose 均默认 AioSandbox | `.env.example:21`、`Dockerfile:50` |
| **CI 是否覆盖 compose 冷启动** | ❌ **没有**。CI 只跑 `pytest -m "not live"`，无 compose 冒烟 job | `.github/workflows/ci.yml` |

---

## 二、真实冷启动风险（应拦在 RC 前）

### ⚠️ 风险① readyz 探测不到"真正会用的重依赖"
`app.py:readyz` 只查 postgres + checkpointer + store（`app.py:127-165`）。
但沙箱 provider 是**懒加载**（`sandbox/provider.py:64-77`，首次 `get_sandbox_provider()` 才建），
故 **app 无 Docker 也能启动**，而**第一个触发沙箱的命令才会拉 ~13GB 镜像**（`README.md:246` 也承认）。
→ healthcheck/readyz 全绿 ≠ 用户能跑任务。首条 `/commit` + sandbox 命令才暴露"没 Docker/拉不动 13GB"。

### ⚠️ 风险② 沙箱内 `/mnt/skills` 挂载源 ≠ 公共 skills 真实位置（**已确证**）
- provider 传 `skills_path=os.path.abspath(".caspian/skills")` → `aio_sandbox_provider.py:121`；
  `local_backend.create_container` 把它绑到容器 `/mnt/skills/`（`local_backend.py:142`）。
- 公共 skills 实际在 `skills/`：`path_utils.py:76` `SKILLS_PUBLIC_REAL_ROOT="skills"`；compose 挂 `./skills:/app/skills:ro`（`docker-compose.yml:48`）。
- 所以容器里 `/mnt/skills/public/...` 绑的是 `.caspian/skills`（默认**无任何代码创建、为空**），
  而 app 侧 `resolve_skill_path` 把公共 skills 解析到 `skills/`（`path_utils.py:297-347`）。
- skill 脚本以"相对本 skill 目录"执行（`skills/docx/SKILL.md:17,40,49,54,57`，如 `python scripts/office/soffice.py …`），
  走 `cd /mnt/skills/public/docx && …` 或绝对路径在**沙箱内**执行 → 找不到。
- `LocalSandbox` 更直接：`run_shell` 在宿主机 `workspace` 下跑（`sandbox/local.py:118-144`），宿主机没有 `/mnt/skills`。
→ **两种沙箱的 bash 执行点都拿不到公共 skills**。这是配置不一致，非"疑似"。
（注：`read_file_tool("/mnt/skills/...")` 在 app 进程内解析到宿主机 `skills/` 仍是通的，故**读** SKILL.md 正常，**跑脚本**才断。）

### ⚠️ 风险③ `CREATE EXTENSION vector` 依赖超级用户
迁移会建扩展，但仅当 DB 角色有 CREATE 权限（`a2c3e4f5d6b7:58-65`）。
compose 里 `caspian` 是官方镜像预设超级用户（可行）；**受限角色会在 alembic upgrade 阶段静默失败**。

### ⚠️ 风险④ 默认 Docker 沙箱 vs "本机开发会踩坑"的张力
README 已说本地开发要切 `LocalSandbox`（无 OS 隔离）。
但「app 能启动」掩盖了「第一个沙箱调用才失败」——没设 `CASPIAN_SANDBOX` 的开发者会**在很晚才看到报错**。

---

## 三、对上 ChatGPT 清单的修订结论

**保留**：`v0.1.0-rc1` tag + four-block Release Notes（尤其 **Known limitations**）。

**改为**按"陌生用户冷启动门槛"重排前置项（并已按最新决策定标）：

- ✅ 保留：CI 全绿、默认 Docker 沙箱、`docker compose up` 全新启动、跨 user/thread 隔离测试。
- ❌ 降级/删除：`LocalSandbox 路径漏洞修复`（未找到依据）；`README Quick Start 重写`（已写，改为"对照容器流程复查"）；`.env.example 完整`（已完整）。
- ➕ **发布前补上**：
  1. **用一个含沙箱命令的真实任务做冒烟**，而不是只看 readyz（这是让 RC 有意义的唯一关键动作）。
  2. **验证 `CREATE EXTENSION vector` 在目标 DB 角色下可执行**。
  3. **明确首次沙箱调用需拉取 ~13GB 镜像**（写进 Known limitations）。
- 📌 **改为 follow-up（不阻塞发布）**：沙箱内 `/mnt/skills` 访问公共 skills——因 `docx`/`vision` 定为可有可无，降级为后期任务。

---

## 四、Release Notes · Known limitations（定稿，可直接贴入）

> **Experimental research release.**
>
> - **冷启动门槛**：`docker compose up` 就绪只证明数据库 / checkpointer / store 层 OK；**首个触发沙箱的命令才会懒拉取约 13GB 的 all-in-one 沙箱镜像**。无 Docker 或网络/磁盘受限时该步骤会失败。
> - **沙箱后端**：容器沙箱是默认后端；本地 / 无 Docker 需切换到 `LocalSandbox`，后者**不提供 OS 级隔离**。
> - **数据库扩展**：迁移需 `CREATE EXTENSION vector`，依赖 DB 角色具有 CREATE 权限（compose 预设用户可用；受限角色会在 `alembic upgrade` 阶段失败）。
> - **Skill 能力为 best-effort**：`docx` / `vision` 等公共 skill 依赖沙箱内 `/mnt/skills` 挂载可访问其脚本；当前该挂载源与公共 skills 真实位置不一致，这些 skill 属"尽力而为"，**不作为本版本的能力承诺**。
> - **治理基准**：离散等级治理基准尚不能确立相对其他 agent 架构的普遍优越性。

---

## 五、本核查未覆盖 / 未实测项（诚实标注）

- 未真实运行任何容器 / LLM；风险②是**代码级确证**的配置不一致，其"是否 100% 运行失败"取决于 agent 实际怎么驱动脚本。
- 未验证 `sandbox-docker` (dind) 在当前宿主机上能否真正跑 13GB 镜像（性能/网络/磁盘）。
- 未逐条跑 `tests/`（`-m "not live"` 之外的 `live` 用例）。
- 未验证 compose 中 `caspian_data` 卷与 `./skills` 只读挂载在 `dind` 侧是否满足 bind mount source 存在性。

---

## 六、Release runbook（git 卫生 + 打 tag）

`git status` 实测（`main`，指向 `origin/main`，origin = `https://github.com/Leeminjing/caspian-agent.git`）暴露两类未提交文件：

**A. 运行时产物（不应入库）** — 多个 `requirements/<uuid>/`（决策等级表 / task-contract，按 thread 落盘的**用户数据**）。
`.gitignore` 只忽略了 `stress-*/edit-*/extest/...` 等具体模式，**没忽略任意 UUID 目录**，故漏在工作区。
→ 建议 `requirements/` 整体入 `.gitignore`（它们是运行时账本，不是源码）。

**B. 真实但未提交的内容** — `skills/vision/`、`.mcp.json`、`knowledge/Next.js-latest-stable.md`、`knowledge/React-latest-stable.md`、`knowledge/Supabase-latest-stable.md`、`knowledge/shadcn-ui-latest-stable.md`。
→ 其中 `skills/vision/` 与 `.mcp.json`（vision MCP 注册）成对；`knowledge/*-latest-stable.md` 是知识产物。是否随本次发布入库由仓库所有者定（vision 已定为"可有可无"）。

> ⚠️ **若现在直接 `git tag`，tag 会指向 `e32a64e`，缺 `skills/vision/` 与 `.mcp.json`。** 必须先清理/提交，再打 tag。

**建议顺序（按你的处置决定调整后逐条执行）**：

```bash
# 1) 运行时产物不入库
echo "requirements/" >> .gitignore          # 或只忽略 UUID 目录（按需）

# 2) 提交本次 release 文档
git add RELEASE_NOTES_v0.1.0-rc1.md RELEASE_COLDSTART_AUDIT.md
#    若 vision 随本次发布：一并
#    git add skills/vision/ .mcp.json knowledge/*-latest-stable.md

# 3) 提交 + 打 tag + 推送到 GitHub（origin = Leeminjing/caspian-agent）
git commit -m "docs: v0.1.0-rc1 release notes and cold-start audit"
git tag -a v0.1.0-rc1 -m "Caspian v0.1.0-rc1"
git push origin main
git push origin v0.1.0-rc1
```

> 说明：`git push` 的目标是你的**公开 GitHub 仓库**，且涉及"哪些内容该进本次发布"的内容裁决，故此处只给命令、不代跑。
