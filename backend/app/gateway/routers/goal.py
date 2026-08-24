"""
本文件对外提供 `router`（APIRouter 实例），定义目标查询接口 `GET /api/threads/{thread_id}/goal`。

对外提供:
    router: APIRouter — 已注册目标查询路由的 FastAPI Router，供 app 挂载

输入:
    get_current_goal:
        thread_id: str — 路径参数，线程（会话）标识
        request: Request — FastAPI Request 对象（用于获取 store 与当前用户）

输出:
    JSON — {"goal": <当前活性目标快照 | null>}

工作流:
    (1) 从 request.app.state 获取 LangGraph store
    (2) 从 request.state.current_user.id 取 user_id
    (3) 用 GoalService 按 (user_id, thread_id) 读当前目标
    (4) active/paused/blocked 返回其快照（含 objective/rounds/armed/blocked_reason），complete/无目标返回 null

示例:
    from backend.app.gateway.routers.goal import router
    app.include_router(router, prefix="/api/threads")
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter()


def _goal_snapshot(record) -> dict | None:
    """把当前目标记录转为前端快照；complete/无目标返回 None（无活性目标）。"""
    if record is None or record.phase == "complete" or record.is_none():
        return None
    value: dict = {
        "id": record.id,
        "revision": record.revision,
        "objective": record.objective,
        "phase": record.phase,
        "rounds_started": record.rounds_started,
        "max_goal_rounds": record.max_goal_rounds,
        "armed": record.armed,
    }
    if record.blocked_reason is not None:
        value["blocked_reason"] = record.blocked_reason.to_dict()
    return value


@router.get("/{thread_id}/goal")
async def get_current_goal(thread_id: str, request: Request):
    """GET /api/threads/{thread_id}/goal — 读取线程当前目标，供前端打开线程时种子 Goal 徽章。"""
    store = getattr(request.app.state, "store", None)
    if store is None:
        return {"goal": None}
    user = getattr(request.state, "current_user", None)
    if user is None:
        return JSONResponse(status_code=401, content={"detail": "未登录"})

    from caspian.goal.service import GoalService

    service = GoalService(store=store, user_id=str(user.id), thread_id=str(thread_id))
    record = await service.get()
    return {"goal": _goal_snapshot(record)}
