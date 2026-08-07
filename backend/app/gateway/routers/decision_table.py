"""
本文件对外提供 `router`（APIRouter 实例），定义决策等级表查询接口 `GET /api/threads/{thread_id}/decision-table`。

对外提供:
    router: APIRouter — 已注册等级表查询路由的 FastAPI Router，挂载到 /api/threads 前缀

输入:
    get_decision_table:
        thread_id: str — 请求路径参数，目标 thread ID

输出:
    JSONResponse — { exists, version, updated, rows: [{requirement, decision, priority}, ...] }
    exists 为 false 时其余字段为 None / 空列表

具体工作流:
    (1) 调用 read_decision_table(thread_id) 读取当前 thread 的决策等级表
    (2) 不存在 → 返回 exists=false 的空结构
    (3) 存在 → 返回 version、updated 与逐条条目（requirement/decision/priority）

示例:
    GET /api/threads/550e8400-e29b-41d4-a716-446655440000/decision-table
    # → {"exists": true, "version": "6d1cee0cec81", "rows": [...]}
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from caspian.agents.commitment.decision_table import read_decision_table

router = APIRouter()


@router.get("/{thread_id}/decision-table")
async def get_decision_table(thread_id: str, request: Request) -> JSONResponse:
    """GET /api/threads/{thread_id}/decision-table — 返回当前 thread 的决策等级表。

    输入:
        thread_id: str — 目标 thread ID
        request: Request — FastAPI Request（仅用于路由上下文）

    输出:
        JSONResponse — 等级表结构（见文件头）
    """
    table = read_decision_table(thread_id)
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
                    "requirement": row.requirement,
                    "decision": row.decision,
                    "priority": row.priority,
                }
                for row in table.rows
            ],
        }
    )
