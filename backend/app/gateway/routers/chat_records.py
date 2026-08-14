"""
本文件对外提供 `router`（APIRouter 实例），定义聊天记录历史读取接口 `GET /api/threads/{thread_id}/messages`。

对外提供:
    router: APIRouter — 已注册聊天记录路由的 FastAPI Router，供 app 挂载
    get_thread_messages — 异步路由函数，读取指定 thread 的最新聊天消息

输入:
    get_thread_messages:
        thread_id: str — 会话线程 ID（路径参数）
        request: Request — FastAPI Request 对象，用于获取 app.state.checkpointer

输出:
    dict — {"messages": [...]}，消息为与 run 流式 values chunk 同构的序列化 dict

具体工作流:
    (1) 从 request.app.state.checkpointer 获取 LangGraph checkpointer
    (2) 构造 thread 查询 config，调用 aget_tuple 读取最新 checkpoint
    (3) 无 checkpoint（thread 从未运行）→ 返回空消息列表
    (4) 有 checkpoint → 取 channel_values.messages，逐个 BaseMessage.model_dump()
        序列化后返回（与 worker 流式序列化一致，前端渲染路径可复用）

示例:
    from backend.app.gateway.routers.chat_records import router
    app.include_router(router, prefix="/api/threads")
"""

import logging

from fastapi import APIRouter, Request

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/{thread_id}/messages")
async def get_thread_messages(thread_id: str, request: Request) -> dict:
    """读取指定 thread 的最新聊天消息列表。

    输入:
        thread_id: str — 会话线程 ID
        request: Request — FastAPI Request 对象

    输出:
        dict — {"messages": [...]}，空线程返回 {"messages": []}
    """
    checkpointer = request.app.state.checkpointer
    latest = await checkpointer.aget_tuple(
        {"configurable": {"thread_id": thread_id}}
    )
    if latest is None:
        return {"messages": []}
    messages = latest.checkpoint.get("channel_values", {}).get("messages", [])
    return {"messages": [message.model_dump() for message in messages]}
