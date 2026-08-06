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

## 本地网页登录账户

- 邮箱：`2656226581@qq.com`
- 密码只以项目规定的 SHA-256 + bcrypt 哈希保存在 `users` 表中，不在仓库记录明文。
