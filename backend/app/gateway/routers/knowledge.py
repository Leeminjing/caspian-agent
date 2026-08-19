"""
本文件对外提供 `router`（APIRouter 实例），定义离散等级治理 RAG 的知识库接口。

对外提供:
    router: APIRouter — 已注册知识库路由的 FastAPI Router，前缀 /api/knowledge

输入:
    ingest_knowledge: {content, level?, source?, source_url?}
    list_knowledge: 无
    update_level: 路径参数 entry_id + {level}
    query_knowledge: {query, top_k?}

输出:
    POST /             → 201 {id, level, level_display}
    GET /              → {entries: [{id, content, level, level_display, source,
                          source_url, created_at, updated_at}]}
    PATCH /{entry_id}  → {id, level, level_display}；404 条目不存在
    POST /query        → {query, candidates, result: {final_evidence_set, notes},
                          ledger}；judge 失败 → 502

具体工作流:
    (1) 全部端点从 request.state.current_user.id 取 user_id，
        request.app.state.store 取 LangGraph Store
    (2) 入库/改等级经 store_client（level ∈ {0,1,2,3,None}，content 1..8000）
    (3) 查询经共享管线 run_governed_query（向量召回 → judge 冲突判定 → govern
        等级治理）执行；judge 调用异常返回 502，绝不静默跳过治理

示例:
    POST /api/knowledge {"content": "功能 A 已废弃。", "level": 3}
    POST /api/knowledge/query {"query": "功能 A 是否可用"}
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from caspian.knowledge.pipeline import run_governed_query
from caspian.knowledge.schemas import level_display
from caspian.knowledge.store_client import (
    list_knowledge,
    put_knowledge,
    update_level,
)

router = APIRouter(prefix="/api/knowledge")

_CONTENT_MAX = 8000


class IngestRequest(BaseModel):
    """入库请求体。level 允许 0-3 或 null（null=未评级）。"""

    content: str = Field(min_length=1, max_length=_CONTENT_MAX)
    level: int | None = Field(default=None, ge=0, le=3)
    source: str = ""
    source_url: str | None = None


class UpdateLevelRequest(BaseModel):
    """改等级请求体。"""

    level: int | None = Field(default=None, ge=0, le=3)


class QueryRequest(BaseModel):
    """检索治理请求体。"""

    query: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=20)


def _entry_dict(item) -> dict:
    value = item.value or {}
    return {
        "id": str(item.key),
        "content": str(value.get("content", "")),
        "level": value.get("level"),
        "level_display": level_display(value.get("level")),
        "source": str(value.get("source", "")),
        "source_url": value.get("source_url"),
        "created_at": getattr(item, "created_at", None),
        "updated_at": getattr(item, "updated_at", None),
    }


@router.post("")
async def ingest_knowledge(body: IngestRequest, request: Request) -> JSONResponse:
    user_id = str(request.state.current_user.id)
    store = request.app.state.store
    try:
        key = await put_knowledge(
            store,
            user_id,
            content=body.content,
            level=body.level,
            source=body.source,
            source_url=body.source_url,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse(
        status_code=201,
        content={"id": key, "level": body.level, "level_display": level_display(body.level)},
    )


@router.get("")
async def get_knowledge_list(request: Request) -> dict:
    user_id = str(request.state.current_user.id)
    store = request.app.state.store
    items = await list_knowledge(store, user_id)
    return {"entries": [_entry_dict(item) for item in items]}


@router.patch("/{entry_id}")
async def patch_knowledge_level(
    entry_id: str, body: UpdateLevelRequest, request: Request
) -> dict:
    user_id = str(request.state.current_user.id)
    store = request.app.state.store
    try:
        ok = await update_level(store, user_id, entry_id, body.level)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail="条目不存在")
    return {
        "id": entry_id,
        "level": body.level,
        "level_display": level_display(body.level),
    }


@router.post("/query")
async def query_knowledge(body: QueryRequest, request: Request) -> dict:
    user_id = str(request.state.current_user.id)
    store = request.app.state.store

    result, candidates, error = await run_governed_query(
        store,
        user_id,
        body.query,
        body.top_k,
    )
    if error is not None:
        raise HTTPException(status_code=502, detail="冲突判定失败，治理未执行")
    if not candidates:
        return {
            "query": body.query,
            "candidates": [],
            "result": {"final_evidence_set": [], "notes": ["知识库中没有检索到相关内容。"]},
            "ledger": [],
        }

    return {
        "query": body.query,
        "candidates": [c.model_dump() for c in candidates],
        "result": {
            "final_evidence_set": [e.model_dump() for e in result.final_evidence_set],
            "notes": result.notes,
        },
        "ledger": [item.model_dump() for item in result.ledger],
    }
