"""
本文件对外提供 FastAPI 应用实例 app，作为 backend 网关的统一入口。

对外提供:
    app: FastAPI — 已绑定 lifespan 的 FastAPI 应用实例

具体工作流:
    (0) 定义 lifespan 异步上下文管理器
    (1) lifespan 内部加载 AppConfig（组合根），传入 langgraph_runtime 进行依赖注入
    (2) async with langgraph_runtime(app, app_config) 管理核心资源生命周期
    (3) 创建 FastAPI 实例并传入 lifespan
    (4) 注册中间件和路由
    (5) 模块级导出 app 实例，供 uvicorn 等 ASGI server 直接引用

示例:
    uvicorn backend.app.gateway.app:app --host 0.0.0.0 --port 8000
"""

import asyncio
import sys
from pathlib import Path

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from caspian.runtime.home import caspian_env_file

from backend.app.gateway.deps import langgraph_runtime

# 导入 gateway models 以注册到 Base.metadata（供 Alembic autogenerate 发现）
import backend.app.gateway.models  # noqa: F401
import backend.app.gateway.context.models  # noqa: F401

# 在所有配置加载之前注入环境变量。优先从 ~/.caspian/config/.env 读取（不覆盖已 setx 的真环境变量）；
# 文件不存在时回退到进程工作目录的 .env。
home_env_file = caspian_env_file()
if home_env_file.exists():
    load_dotenv(home_env_file, override=False)
else:
    load_dotenv()

# psycopg 异步驱动要求 SelectorEventLoop（Windows 默认 ProactorEventLoop 不兼容）
# 必须在 uvicorn 创建事件循环之前设置
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    from caspian.config import get_app_config

    app_config = get_app_config("config.yaml")
    logger.info("AppConfig 已加载，开始初始化 agent 核心资源...")

    async with langgraph_runtime(app, app_config):
        logger.info("agent 核心资源初始化完成，开始接收请求")
        yield

    logger.info("FastAPI lifespan 关闭，agent 核心资源已释放")


app = FastAPI(lifespan=lifespan)
static_dir = Path(__file__).with_name("static")
app.mount("/assets", StaticFiles(directory=static_dir), name="assets")


@app.get("/", include_in_schema=False)
async def frontend() -> FileResponse:
    return FileResponse(static_dir / "index.html")


@app.get("/healthz", include_in_schema=False)
async def healthz() -> JSONResponse:
    """存活探针：进程可响应即返回 ok。"""
    return JSONResponse({"status": "ok"})


@app.get("/readyz", include_in_schema=False)
async def readyz(request: Request) -> JSONResponse:
    """就绪探针：PostgreSQL / checkpointer / LangGraph Store 均可达才返回就绪（200），
    任一不可达返回非就绪（503）。供 docker compose healthcheck 使用。"""
    checks: dict[str, str] = {}

    try:
        from caspian.persistence.engine import get_session
        from sqlalchemy import text

        async with get_session() as session:
            await session.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception:
        checks["postgres"] = "unavailable"

    try:
        checkpointer = getattr(request.app.state, "checkpointer", None)
        if checkpointer is None:
            raise RuntimeError("checkpointer 未初始化")
        await checkpointer.aget_tuple({"configurable": {"thread_id": "__readyz_probe__"}})
        checks["checkpointer"] = "ok"
    except Exception:
        checks["checkpointer"] = "unavailable"

    try:
        store = getattr(request.app.state, "store", None)
        if store is None:
            raise RuntimeError("store 未初始化")
        await store.aget(("__readyz_probe__",), "__readyz_probe__")
        checks["langgraph_store"] = "ok"
    except Exception:
        checks["langgraph_store"] = "unavailable"

    ready = all(value == "ok" for value in checks.values())
    return JSONResponse(
        {"status": "ready" if ready else "not_ready", "checks": checks},
        status_code=200 if ready else 503,
    )

# 注册中间件（AuthMiddleware 注入本地单用户身份）
from backend.app.gateway.middleware.auth import AuthMiddleware

app.add_middleware(AuthMiddleware)
logger.info("AuthMiddleware 已注册（本地单用户身份注入）")

# 注册路由
from backend.app.gateway.routers.thread_runs import router as thread_runs_router
from backend.app.gateway.routers.auth import router as auth_router
from backend.app.gateway.routers.uploads import router as uploads_router
from backend.app.gateway.routers.skills import router as skills_router
from backend.app.gateway.routers.decision_table import router as decision_table_router
from backend.app.gateway.routers.chat_records import router as chat_records_router
from backend.app.gateway.routers.config import router as config_router
from backend.app.gateway.routers.knowledge import router as knowledge_router
from backend.app.gateway.routers.contexts import router as contexts_router
from backend.app.gateway.routers.plugins import router as plugins_router
from backend.app.gateway.routers.models import router as models_router
from backend.app.gateway.routers.goal import router as goal_router
from backend.app.gateway.routers.thread_lifecycle import router as thread_lifecycle_router

app.include_router(thread_runs_router, prefix="/api/threads")
app.include_router(uploads_router, prefix="/api/threads")
app.include_router(decision_table_router, prefix="/api/threads")
app.include_router(chat_records_router, prefix="/api/threads")
app.include_router(thread_lifecycle_router, prefix="/api/threads")
app.include_router(skills_router)
app.include_router(knowledge_router)
app.include_router(contexts_router)
app.include_router(plugins_router)
app.include_router(models_router)
app.include_router(auth_router)
app.include_router(goal_router, prefix="/api/threads")
app.include_router(config_router)
logger.info("路由已注册: thread_runs, uploads, decision_table, chat_records, config, thread_lifecycle, skills, knowledge, contexts, plugins, models, auth, goal")
