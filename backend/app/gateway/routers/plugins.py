"""
本文件对外提供 `router`（APIRouter 实例），定义插件调试视图 REST 接口。

对外提供:
    router: APIRouter — 已注册插件路由的 FastAPI Router，前缀 /api/plugins

输入:
    list_plugins(request) — GET /api/plugins
    get_plugin(name, request) — GET /api/plugins/{name}

输出:
    GET /api/plugins         → {"plugins": [状态档案, ...]}（public + 当前用户 custom）
    GET /api/plugins/{name}  → 单插件状态档案 + recent_traces（该插件最近执行参与记录）
    未登录 → 401（由 AuthMiddleware 统一处理）；插件不存在 → 404

具体工作流:
    (1) 从 request.app.state.plugin_runtime 取插件运行时（未初始化返回空列表）
    (2) 状态按当前登录用户过滤（custom 插件仅本人可见）

示例:
    GET /api/plugins
    # → {"plugins": [{"name": "vision", "scope": "public", "state": "active",
    #     "injected": ["tool"], "requires": [], ...}]}
"""

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/plugins")


def _runtime(request: Request):
    runtime = getattr(request.app.state, "plugin_runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="插件运行时未初始化")
    return runtime


@router.get("")
async def list_plugins(request: Request) -> dict:
    """GET /api/plugins — 返回 public + 当前用户 custom 插件状态列表。"""
    runtime = _runtime(request)
    user_id = str(request.state.current_user.id)
    return {"plugins": runtime.status_payload(user_id)}


@router.get("/{name}")
async def get_plugin(name: str, request: Request) -> dict:
    """GET /api/plugins/{name} — 返回单插件状态与最近执行参与记录。"""
    runtime = _runtime(request)
    user_id = str(request.state.current_user.id)
    payload = runtime.status_for(name, user_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="插件不存在")
    payload["recent_traces"] = runtime.trace.recent(plugin=name, limit=20)
    return payload
