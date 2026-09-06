#!/bin/sh
# Caspian 容器入口：先执行数据库迁移（读取 $DATABASE_URL，见 migrations/env.py），
# 再启动网关。迁移仅在首次/表结构变化时才真正执行，幂等，不需要用户手工命令。
#
# 依赖：工作目录为 /app（Dockerfile 固定 WORKDIR=/app），
#       config.yaml、run_dev.py、backend/... 均在 /app 下。
set -e

echo "[entrypoint] 开始执行数据库迁移 (alembic upgrade head) ..."
python -m alembic \
  -c backend/packages/harness/caspian/persistence/migrations/alembic.ini \
  upgrade head
echo "[entrypoint] 数据库迁移完成，启动网关 ..."

exec python run_dev.py "${PORT:-8000}"
