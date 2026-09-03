"""
本文件对外提供 `router`（APIRouter 实例），定义离散等级治理 RAG 的知识库接口。

对外提供:
    router: APIRouter — 已注册知识库路由的 FastAPI Router，前缀 /api/knowledge

输入:
    ingest_knowledge: {content, source?, source_url?}
    list_knowledge: {limit?, offset?}
    patch_knowledge: 路径参数 entry_id + {source_url?, level_override?, expected_level?}
    query_knowledge: {query, top_k?}

输出:
    POST /        → 201 {id, level, level_display}
    GET /         → {entries: [...]}
    PATCH /{id}   → {id, level, level_display}；404 不存在 / 409 CAS 冲突
    POST /query   → {query, status, candidates, result, ledger}

具体工作流:
    (1) 全部端点从 request.state.current_user.id 取 user_id，request.app.state.store 取 store
    (2) 入库经 store_client（等级由 source_url 派生，content 1..8000）
    (3) PATCH 改来源归属/覆盖等级，走 expected_level CAS
    (4) 查询经共享管线 run_governed_query；judge 失败返回 unadjudicated 而非 502

示例:
    POST /api/knowledge {"content": "功能 A 已废弃。", "source_url": "https://docs.example.com/x"}
    POST /api/knowledge/query {"query": "功能 A 是否可用"}
"""

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from caspian.knowledge.pipeline import run_governed_query
from caspian.knowledge.schemas import level_display
from caspian.knowledge.store_client import (
    ProvenanceUpdateStatus,
    list_knowledge,
    put_knowledge,
    update_provenance,
)

router = APIRouter(prefix="/api/knowledge")

_CONTENT_MAX = 8000
_LIST_LIMIT_MAX = 500


class IngestRequest(BaseModel):
    """入库请求体。等级由 source_url 的域名策略派生，不接收 level 字段。"""

    content: str = Field(min_length=1, max_length=_CONTENT_MAX)
    source: str = ""
    source_url: str | None = None


class UpdateKnowledgeRequest(BaseModel):
    """改来源归属/覆盖等级的请求体（CAS）。"""

    source_url: str | None = None
    level_override: int | None = Field(default=None, ge=0, le=3)
    expected_level: int | None = Field(default=None, ge=0, le=3)


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
        "provenance": value.get("provenance"),
        "created_at": getattr(item, "created_at", None),
        "updated_at": getattr(item, "updated_at", None),
    }


@router.post("")
async def ingest_knowledge(body: IngestRequest, request: Request) -> JSONResponse:
    user_id = str(request.state.current_user.id)
    store = request.app.state.store
    try:
        key, level = await put_knowledge(
            store,
            user_id,
            content=body.content,
            source=body.source,
            source_url=body.source_url,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse(
        status_code=201,
        content={"id": key, "level": level, "level_display": level_display(level)},
    )


@router.get("")
async def get_knowledge_list(
    request: Request,
    limit: int = Query(_LIST_LIMIT_MAX, ge=1, le=_LIST_LIMIT_MAX),
    offset: int = Query(0, ge=0),
) -> dict:
    user_id = str(request.state.current_user.id)
    store = request.app.state.store
    items = await list_knowledge(store, user_id, limit=limit, offset=offset)
    return {"entries": [_entry_dict(item) for item in items]}


@router.patch("/{entry_id}")
async def patch_knowledge(
    entry_id: str, body: UpdateKnowledgeRequest, request: Request
) -> dict:
    user_id = str(request.state.current_user.id)
    store = request.app.state.store
    if body.source_url is None and body.level_override is None:
        raise HTTPException(status_code=422, detail="需提供 source_url 或 level_override 之一")
    try:
        status = await update_provenance(
            store,
            user_id,
            entry_id,
            source_url=body.source_url,
            level_override=body.level_override,
            expected_level=body.expected_level,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if status is ProvenanceUpdateStatus.NOT_FOUND:
        raise HTTPException(status_code=404, detail="条目不存在")
    if status is ProvenanceUpdateStatus.CONFLICT:
        raise HTTPException(status_code=409, detail="等级已变更，请刷新后重试")

    item = await store.aget(("knowledge", user_id), entry_id)
    level = item.value.get("level") if item is not None else None
    return {"id": entry_id, "level": level, "level_display": level_display(level)}


@router.post("/query")
async def query_knowledge(body: QueryRequest, request: Request) -> dict:
    user_id = str(request.state.current_user.id)
    store = request.app.state.store

    result = await run_governed_query(
        store,
        user_id,
        body.query,
        body.top_k,
    )
    return {
        "query": body.query,
        "status": result.status,
        "candidates": [c.model_dump() for c in result.candidates],
        "result": {
            "final_evidence_set": [e.model_dump() for e in result.final_evidence_set],
            "notes": result.notes,
        },
        "ledger": [item.model_dump() for item in result.ledger],
    }
