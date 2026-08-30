"""
本文件对外提供 `start_run` 异步函数，作为 core-aob 链路的编排层核心。

对外提供:
    start_run — 编排实现层需要的参数与对象，最后创建 asyncio task 调用实现层

输入:
    body: Any — RunCreateRequest 解析后的请求体
    thread_id: str — 请求查询参数中的 thread_id
    request: Request — FastAPI Request 对象，用于获取 app.state 中的资源

输出:
    RunRecord — 新创建的 run 运行时档案

具体工作流:
    (1) 从 request.app.state 获取 StreamBridge、RunManager、Checkpointer、Store
    (2) 通过 get_app_config("config.yaml") 获取 AppConfig
    (3) 从 request.state.current_user.id 提取 user_id
    (4) 创建 RunRecord（初始状态 pending）
    (5) 组装参数：input → HumanMessage（保留 additional_kwargs.files）、RunnableConfig、context（含 user_id）、stream_modes
    (6) asyncio.create_task(run_agent(...)) 启动 worker
    (7) record.task = task，返回 RunRecord

示例:
    @router.post("/{thread_id}/runs/stream")
    async def stream_run(thread_id: str, body: RunCreateRequest, request: Request):
        record = await start_run(body, thread_id, request)
        return StreamingResponse(sse_consumer(...))
"""

import asyncio
import logging
from typing import Any

from fastapi import HTTPException, Request
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from caspian.config.app_config import get_app_config
from caspian.runtime.runs.manager import RunManager, RunRecord
from caspian.runtime.runs.schemas import DisconnectMode
from caspian.runtime.runs.worker import run_agent
from caspian.runtime.stream_bridge.base import StreamBridge

logger = logging.getLogger(__name__)


def _validated_selected_skills(body: Any, user_id: str) -> list[str]:
    from caspian.agents.lead.agent import (
        build_enabled_skill_catalog,
        canonicalize_selected_skills,
    )

    catalog = build_enabled_skill_catalog(user_id=user_id)
    selected_skills, invalid_skills = canonicalize_selected_skills(
        getattr(body, "selected_skills", []),
        catalog,
    )
    if invalid_skills:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown or disabled selected Skills: {', '.join(invalid_skills)}",
        )
    return selected_skills


def _build_graph_input(body: Any) -> dict | Command:
    if hasattr(body, "resume") and body.resume is not None:
        return Command(resume=body.resume)

    graph_input: dict = {}
    if not hasattr(body, "input") or not body.input:
        return graph_input

    raw_input = body.input
    if not isinstance(raw_input, dict):
        return raw_input

    messages = []
    for message in raw_input.get("messages", []):
        if isinstance(message, HumanMessage):
            messages.append(message)
            continue
        if not isinstance(message, dict) or message.get("role", "user") != "user":
            continue
        additional_kwargs = message.get("additional_kwargs")
        files = (
            additional_kwargs.get("files")
            if isinstance(additional_kwargs, dict)
            else None
        )
        kwargs: dict = {}
        if files is not None:
            kwargs["additional_kwargs"] = {"files": files}
        elif (
            isinstance(additional_kwargs, dict)
            and additional_kwargs.get("decision_table_edit") is not None
        ):
            kwargs["additional_kwargs"] = {
                "decision_table_edit": additional_kwargs["decision_table_edit"]
            }
        messages.append(HumanMessage(content=message.get("content", ""), **kwargs))
    graph_input["messages"] = messages
    return graph_input


async def _persist_run_usage(
    record: RunRecord, context_service: Any, thread_id: str
) -> None:
    """run 终态前把累计 usage 聚合写入 web_threads（best-effort，失败仅日志）。

    作为 run_agent 的 before_end 钩子在关流前执行：订阅者收到 end 帧时 usage 已落库，
    rail 刷新不会与落库竞态。

    输入:
        record: RunRecord — 终态 run 档案（worker 已累计 input/cache-hit token）
        context_service: ContextService — 网关层 Context 服务
        thread_id: str — 所属线程

    输出:
        None
    """
    try:
        await context_service.accumulate_usage(
            thread_id,
            record.prompt_input_tokens,
            record.prompt_cache_hit_tokens,
        )
    except Exception:
        logger.error("usage 聚合落库失败 (thread_id=%s)", thread_id, exc_info=True)


async def start_run(
    body: Any,
    thread_id: str,
    request: Request,
) -> RunRecord:
    # (1) 从 request.app.state 获取资源
    bridge: StreamBridge = request.app.state.stream_bridge
    run_manager: RunManager = request.app.state.run_manager
    checkpointer = request.app.state.checkpointer
    store = request.app.state.store

    # (2) 获取 AppConfig
    app_config = get_app_config("config.yaml")

    user_id = str(request.state.current_user.id)
    selected_skills = _validated_selected_skills(body, user_id)

    # (2.5) Context 投影闸门：受阻派生 Context 禁止启动主运行（Recursive Context Forking）
    context_service = request.app.state.context_service
    await context_service.ensure_runnable(user_id, thread_id)
    await context_service.register_main_run(user_id, thread_id)

    # (3) 创建 RunRecord
    model_name = None
    if hasattr(body, "context") and body.context:
        model_name = body.context.get("model_name") if isinstance(body.context, dict) else getattr(body.context, "model_name", None)

    record = run_manager.create(
        thread_id=thread_id,
        on_disconnect=DisconnectMode.cancel,
        model_name=model_name,
    )
    logger.info("RunRecord 已创建: run_id='%s', thread_id='%s'", record.run_id, thread_id)

    # (4) 组装参数

    graph_input = _build_graph_input(body)

    # RunnableConfig（recursion_limit 来自 config.yaml agent 段）
    runnable_config: RunnableConfig = {
        "max_concurrency": None,
        "recursion_limit": app_config.agent.recursion_limit,
        "configurable": {
            "thread_id": thread_id,
            "run_id": record.run_id,
        },
    }

    # LangGraph context
    langgraph_context: dict = {
        "model_name": model_name,
        "app_config": app_config,
        "user_id": user_id,
        "selected_skills": selected_skills,
    }

    # stream_modes
    stream_modes: list[str] | str | None = None
    if hasattr(body, "stream_mode") and body.stream_mode:
        stream_modes = body.stream_mode
    if stream_modes is None:
        stream_modes = ["values"]

    # (5) asyncio.create_task 启动 worker
    task = asyncio.create_task(
        run_agent(
            record=record,
            bridge=bridge,
            run_manager=run_manager,
            app_config=app_config,
            graph_input=graph_input,
            runnable_config=runnable_config,
            stream_modes=stream_modes,
            langgraph_context=langgraph_context,
            checkpointer=checkpointer,
            store=store,
            before_end=lambda _r: _persist_run_usage(_r, context_service, thread_id),
        )
    )

    # (6) 挂载 task 并返回
    record.task = task
    logger.info("worker 已启动: run_id='%s'", record.run_id)
    return record
