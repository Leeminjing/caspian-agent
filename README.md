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

## 联网功能环境前置

- web_search（DuckDuckGo）/ web_fetch（Jina Reader）无需 API key，随 harness 依赖安装即可用。
- Playwright MCP（浏览器自动化）走 `extensions_config.json` 的 stdio MCP 通道，宿主机需：
  1. Node.js / npx 可用
  2. 首次运行前执行 `npx playwright install chromium` 下载浏览器
- Playwright MCP 连接失败时按 MCP fail-soft 策略跳过，不影响 lead agent 启动。

## 本地网页登录账户

- 邮箱：`2656226581@qq.com`
- 密码只以项目规定的 SHA-256 + bcrypt 哈希保存在 `users` 表中，不在仓库记录明文。
