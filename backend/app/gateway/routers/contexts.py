"""
本文件对外提供 `router`（APIRouter 实例），定义 Recursive Context Forking 的 7 个 HTTP 接口。

对外提供:
    router: APIRouter — prefix=/api/contexts

输入:
    各端点通过 Request 获取 app.state.context_service 与 request.state.current_user.id

输出:
    snapshot / lineage / tree / derive / definition update / projection decision / rename 的 JSON 结果

具体工作流:
    (1) 每个端点先从 request.state.current_user.id 取 user_id
    (2) 调用 app.state.context_service 对应方法（服务内部做用户归属校验）
    (3) 非法归属/状态冲突时由 HTTPException 返回 404 / 409 / 422

示例:
    app.include_router(contexts_router)
"""

from fastapi import APIRouter, Request

from backend.app.gateway.context.models import (
    ContextDefinitionUpdate,
    ContextDeriveCreate,
    ContextProjectionDecision,
    ContextRenameRequest,
)

router = APIRouter(prefix="/api/contexts")


def _service(request: Request):
    return request.app.state.context_service


def _user_id(request: Request) -> str:
    return str(request.state.current_user.id)


@router.get("/tree")
async def get_context_tree(request: Request) -> list[dict]:
    return await _service(request).tree(_user_id(request))


@router.get("/{context_id}")
async def get_context(context_id: str, request: Request) -> dict:
    return await _service(request).get(_user_id(request), context_id)


@router.get("/{context_id}/snapshot")
async def get_context_snapshot(
    context_id: str, request: Request, checkpoint_id: str | None = None
) -> dict:
    return await _service(request).snapshot(_user_id(request), context_id, checkpoint_id)


@router.get("/{context_id}/lineage")
async def get_context_lineage(context_id: str, request: Request) -> dict:
    return await _service(request).lineage(_user_id(request), context_id)


@router.post("/derive")
async def derive_context(body: ContextDeriveCreate, request: Request) -> dict:
    return await _service(request).derive(_user_id(request), body)


@router.put("/{context_id}/definition")
async def update_context_definition(
    context_id: str, body: ContextDefinitionUpdate, request: Request
) -> dict:
    return await _service(request).update_definition(_user_id(request), context_id, body)


@router.post("/{context_id}/projection/decision")
async def decide_context_projection(
    context_id: str, body: ContextProjectionDecision, request: Request
) -> dict:
    return await _service(request).decide(_user_id(request), context_id, body)


@router.patch("/{context_id}")
async def rename_context(
    context_id: str, body: ContextRenameRequest, request: Request
) -> dict:
    return await _service(request).rename(_user_id(request), context_id, body.title)
