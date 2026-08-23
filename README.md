# Caspian

## 本地数据库

- PostgreSQL 容器：`desktop-postgres-1`（PostgreSQL 17 + pgvector）
- 主机与端口：`127.0.0.1:7221`
- 应用数据库：`caspian`
- 应用角色：`caspian`，拥有 `caspian` 数据库
- 容器管理角色：`focus`，仅用于本地数据库初始化和管理
- 必需扩展：`vector`
- 应用连接配置：`config.yaml` 的 `database.url`
- Alembic 配置：`backend/packages/harness/caspian/persistence/migrations/alembic.ini`

执行迁移：

```powershell
cd backend/packages/harness/caspian/persistence/migrations
../../../.venv/Scripts/python.exe -m alembic -c alembic.ini upgrade head
```

## 本地启动

Windows 上 psycopg 异步驱动要求 SelectorEventLoop，不能直接用 `uvicorn` 命令（会挂起），用入口脚本启动：

```powershell
python run_dev.py
```

聊天记录持久化：`config.yaml` 的 `checkpointer.type: postgres`（PostgresSaver，消息随 run 写入 `checkpoints` 表，跨重启存活）。前端刷新/切会话后通过 `GET /api/threads/{thread_id}/messages` 恢复历史消息。

## 联网功能环境前置

- web_search（DuckDuckGo）/ web_fetch（Jina Reader）无需 API key，随 harness 依赖安装即可用。
- Playwright MCP（浏览器自动化）走 `extensions_config.json` 的 stdio MCP 通道，宿主机需：
  1. Node.js / npx 可用
  2. 首次运行前执行 `npx playwright install chromium` 下载浏览器
- Playwright MCP 连接失败时按 MCP fail-soft 策略跳过，不影响 lead agent 启动。

## 本地网页登录账户

- 邮箱：`2656226581@qq.com`
- 密码只以项目规定的 SHA-256 + bcrypt 哈希保存在 `users` 表中，不在仓库记录明文。

## 计划模式（plan-mode）

提供"先规划、后执行"的协作姿态：激活时模型每次请求都会带上部署方配置的策略段（`config.yaml` 的 `plan_mode.section`），并可通过 `exit_plan_mode` 工具把完整计划呈给用户评审，批准才退出计划模式。

- **进入 / 退出**：用户输入 `/plan`（进入）、`/plan <消息>`（进入并携带任务描述）、`/plan off`（退出）。
- **评审退出**：计划模式激活时，模型可调用 `exit_plan_mode`（参数 `plan` 为以 `#` 开头的 markdown 计划）。Web 端会弹出"计划审阅卡"，用户选择 **Approve**（批准并退出）/ **Keep planning**（继续，可带反馈）/ **Chat about it**（留在计划模式等待用户发言）。
- **语义边界**：计划模式是**软引导**，只改变模型提示词与评审退出流程，不强制、不隔离；需要强制限制的部署应单独配置沙箱（`config.yaml` 的 `sandbox`）与承诺层（`commitment`）。
- **配置**：`config.yaml` 的 `plan_mode` 段，`section` 必填非空；`enabled: false` 时中间件与 `exit_plan_mode` 工具均不装配，行为与未引入计划模式前一致。
