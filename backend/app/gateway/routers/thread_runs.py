"""
本文件对外提供 `router`（APIRouter 实例），定义 core-aob 链路唯一对外接口 `POST /api/threads/{thread_id}/runs/stream`。

对外提供:
    router: APIRouter — 已注册 thread runs 相关路由的 FastAPI Router，供 app 挂载
    RunCreateRequest: BaseModel — 请求体 Pydantic 模型
    format_sse: 纯函数 — 将事件信息编码为 SSE 帧字符串
    sse_consumer: 异步生成器 — 订阅 StreamBridge，产出 SSE 帧

输入:
    stream_run:
        thread_id: str — 请求查询参数
        body: RunCreateRequest — 请求体
        request: Request — FastAPI Request 对象

    sse_consumer:
        bridge: StreamBridge — 进程内事件总线
        record: RunRecord — 当前 run 的运行时档案
        request: Request — FastAPI Request 对象（用于检测断线）
        run_mgr: RunManager — run 状态管理注册表

输出:
    stream_run → StreamingResponse(sse_consumer(...))

具体工作流:
    stream_run:
    (1) 调用 start_run(body, thread_id, request) 获取 RunRecord
    (2) 从 request.app.state 获取 StreamBridge 和 RunManager
    (3) 返回 StreamingResponse(sse_consumer(bridge, record, request, run_mgr))

    sse_consumer:
    (1) 读取请求头 Last-Event-ID 作为断线重连起点
    (2) async for event in bridge.subscribe(run_id, last_event_id, heartbeat_interval):
    (3) 若为 HEARTBEAT_SENTINEL → yield SSE 注释行保活
    (4) 若为 END_SENTINEL → yield 结束帧并退出
    (5) 普通事件 → format_sse(event_type, data, event_id) → yield
    (6) 客户端断线 → 停止迭代

示例:
    from backend.app.gateway.routers.thread_runs import router
    app.include_router(router, prefix="/api/threads")
"""

import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from lead_agent.runtime.runs.manager import RunManager, RunRecord
from lead_agent.runtime.stream_bridge.base import StreamBridge
from lead_agent.runtime.stream_bridge.schemas import (
    END_SENTINEL,
    HEARTBEAT_SENTINEL,
    StreamEvent,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class RunCreateRequest(BaseModel):
    """POST /api/threads/{thread_id}/runs/stream 的请求体。"""

    input: dict[str, Any] | None = Field(
        default=None,
        description="Graph input (e.g. {messages: [...]})",
    )
    context: dict[str, Any] | None = Field(default=None)
    stream_mode: list[str] | str | None = Field(
        default=None,
        description="Stream mode(s)",
    )


def format_sse(event_type: str, data: Any, event_id: str) -> str:
    """将事件信息编码为 SSE 帧字符串。

    输入:
        event_type: str — SSE 事件类型
        data: Any — 事件载荷（将序列化为 JSON）
        event_id: str — 流事件 ID

    输出:
        str — SSE 帧字符串，格式为:
            event: <事件类型>
            data: <JSON 数据>
            id: <流事件 ID>

    示例:
        frame = format_sse("metadata", {"run_id": "abc"}, "1")
        # → "event: metadata\\ndata: {"run_id": "abc"}\\nid: 1\\n\\n"
    """
    data_str = json.dumps(data, ensure_ascii=False) if not isinstance(data, str) else data
    return f"event: {event_type}\ndata: {data_str}\nid: {event_id}\n\n"


async def sse_consumer(
    bridge: StreamBridge,
    record: RunRecord,
    request: Request,
    run_mgr: RunManager,
):
    """异步生成器，按 run_id 订阅 StreamBridge，将事件逐条编码为 SSE 帧并 yield。

    输入:
        bridge: StreamBridge — 进程内事件总线
        record: RunRecord — 当前 run 的运行时档案
        request: Request — FastAPI Request 对象（用于读取 Last-Event-ID、检测断线）
        run_mgr: RunManager — run 状态管理注册表

    输出:
        AsyncIterator[str] — 逐条 yield SSE 帧字符串

    工作流:
        (1) 读取 Last-Event-ID 请求头作为断线重连起点
        (2) async for event in bridge.subscribe(run_id, last_event_id)
        (3) 心跳 → yield SSE 注释行保活
        (4) 结束 → yield 结束帧并退出
        (5) 普通事件 → format_sse → yield
        (6) 客户端断线 → 停止迭代

    示例:
        response = StreamingResponse(
            sse_consumer(bridge, record, request, run_mgr),
            media_type="text/event-stream",
        )
    """
    run_id = record.run_id
    last_event_id = request.headers.get("Last-Event-ID")

    try:
        async for event in bridge.subscribe(
            run_id,
            last_event_id=last_event_id,
            heartbeat_interval=15,
        ):
            # 检查客户端是否已断开
            if await request.is_disconnected():
                logger.info("客户端已断开: run_id='%s'", run_id)
                break

            if event is HEARTBEAT_SENTINEL:
                yield ": heartbeat\n\n"
                continue

            if event is END_SENTINEL:
                yield format_sse("end", {"run_id": run_id}, event.id or "end")
                return

            yield format_sse(event.event, event.data, event.id)
    except Exception:
        logger.error("sse_consumer 异常: run_id='%s'", run_id, exc_info=True)


@router.post("/{thread_id}/runs/stream")
async def stream_run(
    thread_id: str,
    body: RunCreateRequest,
    request: Request,
) -> StreamingResponse:
    """POST /api/threads/{thread_id}/runs/stream — 创建 run 并以 SSE 流形式返回执行事件。

    输入:
        thread_id: str — 会话线程 ID
        body: RunCreateRequest — 请求体
        request: Request — FastAPI Request 对象

    输出:
        StreamingResponse — SSE 流式响应
    """
    from backend.app.gateway.services import start_run

    record = await start_run(body, thread_id, request)

    bridge: StreamBridge = request.app.state.stream_bridge
    run_mgr: RunManager = request.app.state.run_manager

    return StreamingResponse(
        sse_consumer(bridge, record, request, run_mgr),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Content-Location": f"/api/threads/{thread_id}/runs/{record.run_id}",
        },
    )
