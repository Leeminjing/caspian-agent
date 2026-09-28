"""本文件对外提供 langgraph_runtime，管理网关的进程级运行资源。

输入为 FastAPI app 与 AppConfig；输出为已挂载的数据库、checkpointer、共享 checkpoint 写入者、outbox 工作者、Store 和 RunManager。
工作流先初始化数据库与 checkpointer，标记上次进程遗留的未完成 Run，再补投递操作事件并恢复共享投影，退出时按资源栈逆序清理。
示例：`async with langgraph_runtime(app, config): await serve()`。"""

import contextlib
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from caspian.config.app_config import AppConfig
from caspian.runtime import RunManager
from caspian.runtime.stream_bridge.async_provider import create_stream_bridge

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

        # (3) 数据库引擎初始化（全局单例，不挂载 app.state）
        if app_config.database is not None:
            from caspian.persistence.engine import dispose_engine, init_engine
            from caspian.persistence.engine import get_session

            init_engine(app_config)
            stack.callback(dispose_engine)
            logger.info("数据库引擎已初始化 (backend=%s)", app_config.database.backend)

        # (3.1) 本地单用户身份引导：采纳已有唯一用户，否则自动创建；user_id 稳定不变。
        from backend.app.gateway.auth.local import ensure_local_user

        local_user = await ensure_local_user()
        app.state.local_user = local_user
        logger.info("本地单用户身份已就绪: user_id=%s", getattr(local_user, "id", None))

        # (3.5) Checkpointer 资源初始化
        from caspian.runtime.checkpointer import create_checkpointer, dispose_checkpointer

        checkpointer = await create_checkpointer(app_config)
        app.state.checkpointer = checkpointer
        stack.push_async_callback(dispose_checkpointer, checkpointer)
        logger.info("Checkpointer 已挂载到 app.state.checkpointer (type=%s)", app_config.checkpointer.type)

        app.state.shared_checkpoint_writer = None
        if app_config.database is not None:
            from caspian.decision_governance.outbox_worker import run_outbox
            from caspian.decision_governance.shared_checkpoint import SharedCheckpointWriter

            shared_writer = SharedCheckpointWriter(checkpointer)
            app.state.shared_checkpoint_writer = shared_writer
            await stack.enter_async_context(run_outbox(get_session, checkpoint_writer=shared_writer))

        # (3.6) Store 资源初始化
        from caspian.runtime.store import create_store, dispose_store

        store = await create_store(app_config)
        app.state.store = store
        stack.push_async_callback(dispose_store, store)
        logger.info("Store 已挂载到 app.state.store (backend=%s)", app_config.langgraph_store.backend)

        # (3.7) ContextService 初始化（Recursive Context Forking，依赖 checkpointer）
        from backend.app.gateway.context.service import ContextService

        app.state.context_service = ContextService(checkpointer)
        logger.info("ContextService 已挂载到 app.state.context_service")

        # (3.7.1) ThreadLifecycleService 初始化（会话级联删除/归档/恢复，依赖 checkpointer + store）
        from backend.app.gateway.context.lifecycle import ThreadLifecycleService

        app.state.thread_lifecycle = ThreadLifecycleService(checkpointer, store)
        logger.info("ThreadLifecycleService 已挂载到 app.state.thread_lifecycle")

        # (3.8) PluginRuntime 初始化（插件系统：public 插件在启动期加载，
        #       custom 插件在用户首次 run 时惰性加载；加载失败不影响系统启动）
        from caspian.plugins.runtime import PluginRuntime, set_plugin_runtime

        try:
            from caspian.config.extensions_config import get_extensions_config

            plugin_runtime = PluginRuntime(
                get_extensions_config("extensions_config.json")
            )
        except Exception:
            from caspian.config.extensions_config import ExtensionsConfig

            plugin_runtime = PluginRuntime(ExtensionsConfig(mcpServers={}, plugins={}))
        await plugin_runtime.load_public()
        set_plugin_runtime(plugin_runtime)
        app.state.plugin_runtime = plugin_runtime
        stack.push_async_callback(plugin_runtime.close)
        logger.info("PluginRuntime 已挂载到 app.state.plugin_runtime")

        # (4) 创建 RunManager 实例，每进程唯一
        run_manager = RunManager()
        app.state.run_manager = run_manager
        logger.info("RunManager 已挂载到 app.state.run_manager")
        if app_config.database is not None:
            from caspian.decision_governance.run_audit import reconcile_incomplete_run_audits

            recovered = await reconcile_incomplete_run_audits(get_session)
            if recovered:
                logger.warning("已标记 %s 个上次进程遗留的未完成 Run", recovered)

        # (5) ... 待扩展更多资源

        yield
