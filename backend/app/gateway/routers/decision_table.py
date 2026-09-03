"""
本文件对外提供 `router`（APIRouter 实例），定义决策等级表查询接口 `GET /api/threads/{thread_id}/decision-table`。

对外提供:
    router: APIRouter — 已注册等级表查询路由的 FastAPI Router，挂载到 /api/threads 前缀

输入:
    get_decision_table:
        thread_id: str — 请求路径参数，目标 thread ID
        request: Request — FastAPI Request（用于取 current_user 与 session）

输出:
    JSONResponse — { exists, version, updated, rows: [{id, requirement, decision, priority, guards}] }
    exists 为 false 时其余字段为 None / 空列表；thread 不属于当前用户时返回 403

具体工作流:
    (1) 从 request.state.current_user.id 取 user_id
    (2) 所有权校验：thread 已注册且不属于当前用户时返回 403
    (3) 调用 read_decision_table(thread_id, user_id=user_id) 按用户隔离读取
    (4) 不存在 → 返回 exists=false 的空结构；存在 → 返回 version、updated 与逐条条目

示例:
    GET /api/threads/550e8400-e29b-41d4-a716-446655440000/decision-table
    # → {"exists": true, "version": "6d1cee0cec81", "rows": [...]}
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from backend.app.gateway.context.models import WebThread
from caspian.agents.commitment.decision_table import read_decision_table
from caspian.persistence.engine import get_session

router = APIRouter()


@router.get("/{thread_id}/decision-table")
async def get_decision_table(thread_id: str, request: Request) -> JSONResponse:
    """GET /api/threads/{thread_id}/decision-table — 返回当前 thread 的决策等级表。

    输入:
        thread_id: str — 目标 thread ID
        request: Request — FastAPI Request（用于取 current_user 与 session）

    输出:
        JSONResponse — 等级表结构（见文件头）；thread 不属于当前用户返回 403
    """
    user_id = str(request.state.current_user.id)

    async with get_session() as session:
        thread = await session.scalar(
            select(WebThread).where(WebThread.thread_id == thread_id)
        )
    if thread is not None and thread.user_id != user_id:
        raise HTTPException(status_code=403, detail="无权访问该会话的决策等级表")

    table = read_decision_table(thread_id, user_id=user_id)
    if table is None:
        return JSONResponse(
            content={
                "exists": False,
                "version": None,
                "updated": None,
                "rows": [],
            }
        )
    return JSONResponse(
        content={
            "exists": True,
            "version": table.version,
            "updated": table.updated,
            "rows": [
                {
                    "id": row.id,
                    "requirement": row.requirement,
                    "decision": row.decision,
                    "priority": row.priority,
                    "guards": [guard.to_dict() for guard in row.guards],
                }
                for row in table.rows
            ],
        }
    )
