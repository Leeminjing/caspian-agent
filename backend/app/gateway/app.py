"""
本文件对外提供 FastAPI 应用实例 app，作为 backend 网关的统一入口。

对外提供:
    app: FastAPI — 已绑定 lifespan 的 FastAPI 应用实例
    auth_config: AuthConfig — 模块级认证配置单例，供中间件和路由使用

具体工作流:
    (0) 模块加载时从 config.yaml 独立加载 AuthConfig（web 层配置，不经过 AppConfig）
    (1) 定义 lifespan 异步上下文管理器
    (2) lifespan 内部加载 AppConfig（组合根），传入 langgraph_runtime 进行依赖注入
    (3) async with langgraph_runtime(app, app_config) 管理核心资源生命周期
    (4) 创建 FastAPI 实例并传入 lifespan
    (5) 注册中间件和路由
    (6) 模块级导出 app 实例，供 uvicorn 等 ASGI server 直接引用

示例:
    uvicorn backend.app.gateway.app:app --host 0.0.0.0 --port 8000
"""

import os
from pathlib import Path

import yaml

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.app.gateway.auth.config import AuthConfig
from backend.app.gateway.deps import langgraph_runtime

# 导入 gateway models 以注册到 Base.metadata（供 Alembic autogenerate 发现）
import backend.app.gateway.models  # noqa: F401

# 在所有配置加载之前注入 .env 环境变量
load_dotenv()

logger = logging.getLogger(__name__)


def _load_auth_config(yaml_path: str) -> AuthConfig:
    """从 config.yaml 独立加载 AuthConfig（web 层配置，不经过 AppConfig 组合根）。

    输入:
        yaml_path: str — config.yaml 文件路径

    输出:
        AuthConfig — 认证配置实例

    工作流:
        (1) 读取 YAML 文件
        (2) 解析 $ENV_VAR 环境变量引用
        (3) 从 auth 段构造 AuthConfig 实例
    """
    yaml_file = Path(yaml_path)
    if not yaml_file.exists():
        raise FileNotFoundError(f"配置文件不存在: {yaml_path}")

    with open(yaml_file, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    # 解析 auth 段中的 $ENV_VAR 环境变量引用
    auth_raw = raw.get("auth", {})
    resolved = {}
    for key, value in auth_raw.items():
        if isinstance(value, str) and len(value) > 1 and value.startswith("$"):
            env_var = value[1:]
            env_value = os.environ.get(env_var)
            if env_value is None:
                raise KeyError(f"环境变量未设置: {env_var}")
            resolved[key] = env_value
        else:
            resolved[key] = value

    return AuthConfig.model_validate(resolved)


# 模块级加载认证配置（中间件注册时需要，必须在 lifespan 之前）
auth_config = _load_auth_config("config.yaml")
logger.info("AuthConfig 已加载（web 层独立配置）")


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

# 注册中间件（AuthMiddleware 先于 CSRFMiddleware）
from backend.app.gateway.middleware.auth import AuthMiddleware
from backend.app.gateway.middleware.csrf import CSRFMiddleware

app.add_middleware(AuthMiddleware, auth_config=auth_config)
app.add_middleware(CSRFMiddleware, auth_config=auth_config)
logger.info("AuthMiddleware 和 CSRFMiddleware 已注册")

# 注册路由
from backend.app.gateway.routers.thread_runs import router as thread_runs_router
from backend.app.gateway.routers.auth import router as auth_router
from backend.app.gateway.routers.uploads import router as uploads_router
from backend.app.gateway.routers.skills import router as skills_router

app.include_router(thread_runs_router, prefix="/api/threads")
app.include_router(uploads_router, prefix="/api/threads")
app.include_router(skills_router)
app.include_router(auth_router)
logger.info("路由已注册: thread_runs, uploads, auth")
