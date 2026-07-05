"""
本文件对外提供 FastAPI 应用实例 app，作为 backend 网关的统一入口。

对外提供:
    app: FastAPI — 已绑定 lifespan 的 FastAPI 应用实例

具体工作流:
    (1) 定义 lifespan 异步上下文管理器
    (2) lifespan 内部加载 AppConfig（组合根），传入 langgraph_runtime 进行依赖注入
    (3) async with langgraph_runtime(app, app_config) 管理核心资源生命周期
    (4) 创建 FastAPI 实例并传入 lifespan
    (5) 模块级导出 app 实例，供 uvicorn 等 ASGI server 直接引用

示例:
    uvicorn backend.app.gateway.app:app --host 0.0.0.0 --port 8000
"""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.app.gateway.deps import langgraph_runtime

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    from lead_agent.config import get_app_config

    app_config = get_app_config("config.yaml")
    logger.info("AppConfig 已加载，开始初始化 agent 核心资源...")

    async with langgraph_runtime(app, app_config):
        logger.info("agent 核心资源初始化完成，开始接收请求")
        yield

    logger.info("FastAPI lifespan 关闭，agent 核心资源已释放")


app = FastAPI(lifespan=lifespan)

from backend.app.gateway.routers.thread_runs import router as thread_runs_router

app.include_router(thread_runs_router, prefix="/api/threads")
