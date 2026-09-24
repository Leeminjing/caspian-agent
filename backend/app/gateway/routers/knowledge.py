"""
本文件对外提供 /api/knowledge 的原子入库、文档入库、列表、CAS 更新和治理查询路由。

对外提供:
    router — FastAPI Router
    ingest_knowledge — POST /api/knowledge，接收已原子化知识并返回 opaque chunk ID
    ingest_document — POST /api/knowledge/documents，返回 document/revision IDs 与有序 chunk IDs
    get_knowledge_list / patch_knowledge / query_knowledge — 兼容既有列表、更新和查询协议

输入:
    Pydantic 请求体、request.state.current_user.id 和 request.app.state.store。

输出:
    JSON 响应；列表/query 同时投影 atomicity 与时态 bindings；EvidenceValidationError 映射
    稳定 422，持久化错误映射 500，CAS 冲突映射 409。

具体工作流:
    路由只做边界校验、依赖取得、调用 knowledge 入库/查询编排和错误映射；结构切分、评级、
    身份、Store value 构造均留在 knowledge 模块。

示例:
    POST /api/knowledge {"content":"功能 A 已废弃。","source":"官方"}
    POST /api/knowledge/documents {"content":"# API\n\n事实。","format":"markdown","source":"官方"}
"""

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from caspian.knowledge.evidence import (
    DocumentInput,
    EvidencePersistenceError,
    EvidenceValidationError,
)
from caspian.knowledge.ingestion import put_document
from caspian.knowledge.pipeline import run_governed_query
from caspian.knowledge.schemas import level_display
from caspian.knowledge.store_client import (
    ProvenanceUpdateStatus,
    evidence_from_item,
    list_knowledge,
    put_knowledge,
    update_provenance,
)

router = APIRouter(prefix="/api/knowledge")

_CONTENT_MAX = 8000
_LIST_LIMIT_MAX = 500


class IngestRequest(BaseModel):
    content: str = Field(min_length=1, max_length=_CONTENT_MAX)
    source: str = ""
    source_url: str | None = None


class DocumentIngestRequest(BaseModel):
    content: str = Field(min_length=1)
    format: Literal["markdown", "text"] = "markdown"
    external_source_id: str | None = None
    source: str = ""
    source_url: str | None = None
    title: str = ""
    published_at: str | None = None
    effective_at: str | None = None
    version: str | None = None


class UpdateKnowledgeRequest(BaseModel):
    source_url: str | None = None
    level_override: int | None = Field(default=None, ge=0, le=3)
    expected_level: int | None = Field(default=None, ge=0, le=3)


class QueryRequest(BaseModel):
    query: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=20)


def _entry_dict(item) -> dict:
    value = item.value or {}
    entry = evidence_from_item(item)
    projected = entry.model_dump()
    projected.update({
        "level_display": level_display(entry.level),
        "provenance": value.get("provenance"),
        "level_basis": value.get("level_basis"),
        "retrieval_text": value.get("retrieval_text"),
        "created_at": getattr(item, "created_at", None),
        "updated_at": getattr(item, "updated_at", None),
    })
    return projected


def _validation_detail(exc: EvidenceValidationError) -> dict:
    return {"code": exc.code, "message": str(exc), "details": exc.details}


@router.post("")
async def ingest_knowledge(body: IngestRequest, request: Request) -> JSONResponse:
    try:
        key, level = await put_knowledge(
            request.app.state.store,
            str(request.state.current_user.id),
            content=body.content,
            source=body.source,
            source_url=body.source_url,
        )
    except EvidenceValidationError as exc:
        raise HTTPException(status_code=422, detail=_validation_detail(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse(status_code=201, content={"id": key, "level": level, "level_display": level_display(level)})


@router.post("/documents")
async def ingest_document(body: DocumentIngestRequest, request: Request) -> JSONResponse:
    document = DocumentInput(**body.model_dump())
    try:
        result = await put_document(
            request.app.state.store,
            str(request.state.current_user.id),
            document,
        )
    except EvidenceValidationError as exc:
        raise HTTPException(status_code=422, detail=_validation_detail(exc)) from exc
    except EvidencePersistenceError as exc:
        detail = {
            "code": "persistence_failed",
            "message": str(exc),
            "completed_ids": list(exc.completed_ids),
            "pending_ids": list(exc.pending_ids),
        }
        raise HTTPException(status_code=500, detail=detail) from exc
    return JSONResponse(status_code=201, content=result.model_dump(mode="json"))


@router.get("")
async def get_knowledge_list(
    request: Request,
    limit: int = Query(_LIST_LIMIT_MAX, ge=1, le=_LIST_LIMIT_MAX),
    offset: int = Query(0, ge=0),
) -> dict:
    items = await list_knowledge(
        request.app.state.store,
        str(request.state.current_user.id),
        limit=limit,
        offset=offset,
    )
    return {"entries": [_entry_dict(item) for item in items]}


@router.patch("/{entry_id}")
async def patch_knowledge(entry_id: str, body: UpdateKnowledgeRequest, request: Request) -> dict:
    if body.source_url is None and body.level_override is None:
        raise HTTPException(status_code=422, detail="需提供 source_url 或 level_override 之一")
    try:
        status = await update_provenance(
            request.app.state.store,
            str(request.state.current_user.id),
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
    item = await request.app.state.store.aget(("knowledge", str(request.state.current_user.id)), entry_id)
    level = item.value.get("level") if item is not None else None
    return {"id": entry_id, "level": level, "level_display": level_display(level)}


@router.post("/query")
async def query_knowledge(body: QueryRequest, request: Request) -> dict:
    result = await run_governed_query(
        request.app.state.store,
        str(request.state.current_user.id),
        body.query,
        body.top_k,
    )
    return {
        "query": body.query,
        "status": result.status,
        "candidates": [candidate.model_dump() for candidate in result.candidates],
        "result": {
            "final_evidence_set": [
                entry.model_dump() for entry in result.final_evidence_set
            ],
            "notes": result.notes,
        },
        "ledger": [entry.model_dump() for entry in result.ledger],
    }
