"""
本文件对外提供 `router`（APIRouter 实例），定义会话生命周期接口：
级联删除、级联归档、级联恢复与归档列表。

对外提供:
    router: APIRouter — 已注册会话生命周期路由的 FastAPI Router，挂载到 /api/threads 前缀

输入:
    各端点通过 Request 获取 app.state.thread_lifecycle 与 request.state.current_user.id

输出:
    delete → {"deleted": [...]}
    archive → {"archived": [...]}
    restore → {"restored": [...]}
    list_archived → [{"thread_id", "title", "archived_at"}, ...]

具体工作流:
    (1) 每个端点先从 request.state.current_user.id 取 user_id
    (2) 调用 app.state.thread_lifecycle 对应方法
    (3) 目标会话不存在或属于其他用户时由 HTTPException 返回 404

示例:
    app.include_router(thread_lifecycle_router, prefix="/api/threads")
"""

from fastapi import APIRouter, Request

from backend.app.gateway.context.lifecycle import ThreadLifecycleService

router = APIRouter()


def _service(request: Request) -> ThreadLifecycleService:
    return request.app.state.thread_lifecycle


def _user_id(request: Request) -> str:
    return str(request.state.current_user.id)


@router.get("/archived")
async def list_archived(request: Request) -> list[dict]:
    """GET /api/threads/archived — 返回当前用户全部已归档会话。"""
    return await _service(request).list_archived(_user_id(request))


@router.delete("/{thread_id}")
async def delete_thread(thread_id: str, request: Request) -> dict:
    """DELETE /api/threads/{thread_id} — 级联硬删除会话及其全部派生后裔（不可恢复）。"""
    return await _service(request).delete(_user_id(request), thread_id)


@router.post("/{thread_id}/archive")
async def archive_thread(thread_id: str, request: Request) -> dict:
    """POST /api/threads/{thread_id}/archive — 级联归档会话及其派生后裔（软删除，可恢复）。"""
    return await _service(request).archive(_user_id(request), thread_id)


@router.post("/{thread_id}/restore")
async def restore_thread(thread_id: str, request: Request) -> dict:
    """POST /api/threads/{thread_id}/restore — 级联恢复已归档会话及其派生后裔。"""
    return await _service(request).restore(_user_id(request), thread_id)
