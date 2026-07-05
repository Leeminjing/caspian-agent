"""
本文件对外提供 langgraph_runtime 异步上下文管理器，作为 FastAPI 应用运行时依赖模块。

对外提供:
    langgraph_runtime(app, app_config) — 进程级 agent 核心资源的生命周期管理器

输入:
    app: FastAPI — FastAPI 应用实例，资源创建后挂载到 app.state
    app_config: AppConfig — 应用配置对象，驱动各资源的实现选择（组合根依赖注入）

输出:
    AsyncGenerator[None, None] — yield 前完成资源初始化并挂载，yield 后清理所有资源

具体工作流:
    (1) 创建 AsyncExitStack 统一管理多个异步上下文资源
    (2) 通过 create_stream_bridge(app_config) 创建 StreamBridge → 挂载到 app.state.stream_bridge
        → 注册到 ExitStack（create_stream_bridge 自带 cleanup on exit）
    (3) 创建 RunManager 实例 → 挂载到 app.state.run_manager
    (4) yield — 此时 FastAPI 开始接收请求
    (5) yield 之后 ExitStack 按 LIFO 顺序清理所有已注册资源

示例:
    from backend.app.gateway.deps import langgraph_runtime
    from lead_agent.config import get_app_config

    app_config = get_app_config("config.yaml")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with langgraph_runtime(app, app_config):
            yield
"""

import contextlib
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from lead_agent.config.app_config import AppConfig
from lead_agent.runtime import RunManager
from lead_agent.runtime.stream_bridge.async_provider import create_stream_bridge

logger = logging.getLogger(__name__)


@asynccontextmanager
async def langgraph_runtime(app: FastAPI, app_config: AppConfig) -> AsyncGenerator[None, None]:
    async with contextlib.AsyncExitStack() as stack:
        # (2) 通过 create_stream_bridge 工厂创建 StreamBridge（组合根依赖注入）
        #     create_stream_bridge 根据 app_config.stream_bridge.type 选择实现
        #     退出时自动清理所有 run 资源
        stream_bridge = await stack.enter_async_context(create_stream_bridge(app_config))
        app.state.stream_bridge = stream_bridge
        logger.info("StreamBridge 已挂载到 app.state.stream_bridge")

        # (3) 创建 RunManager 实例，每进程唯一
        run_manager = RunManager()
        app.state.run_manager = run_manager
        logger.info("RunManager 已挂载到 app.state.run_manager")

        # (4) ... 待扩展更多资源

        yield
