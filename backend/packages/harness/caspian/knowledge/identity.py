"""
本文件对外提供 Evidence Unit 的来源规范化与分层身份纯函数。

对外提供:
    content_hash — 计算正文 SHA-256
    canonical_source — 按 external source ID、URL、来源名、manual-content 的优先级取来源身份
    make_document_id — 生成来源稳定的 document ID
    make_revision_id — 绑定 document、正文和修订元数据
    make_chunk_id — 绑定 revision、绝对 span 和单元正文

输入:
    来源字段、文档/单元正文、版本与时间元数据、原文半开字符区间。

输出:
    规范化来源字符串或带 doc_/rev_/chunk_ 前缀的完整 SHA-256 ID。

具体工作流:
    先选择并规范化 canonical source，再逐层哈希 document、revision、chunk；URL 仅
    规范 scheme/host、默认端口、空 path 和 fragment，query 原样保留。

示例:
    source = canonical_source(source_url="HTTPS://EXAMPLE.COM:443/a#part")
    document_id = make_document_id(source)
"""

from __future__ import annotations

import hashlib
import json
from urllib.parse import SplitResult, urlsplit, urlunsplit

from caspian.knowledge.evidence import EvidenceValidationError, SourceSpan


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _normalize_text(value: str) -> str:
    return " ".join(str(value).strip().split())


def _normalize_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if not parsed.scheme or not parsed.hostname:
        raise EvidenceValidationError("invalid_source_url", "source_url 必须是绝对 URL")
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname.lower()
    port = parsed.port
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        hostname = f"{hostname}:{port}"
    if parsed.username:
        auth = parsed.username
        if parsed.password:
            auth = f"{auth}:{parsed.password}"
        hostname = f"{auth}@{hostname}"
    normalized = SplitResult(scheme, hostname, parsed.path or "/", parsed.query, "")
    return urlunsplit(normalized)


def canonical_source(
    *,
    external_source_id: str | None = None,
    source_url: str | None = None,
    source: str = "",
    manual_content_hash: str | None = None,
) -> str:
    if external_source_id and _normalize_text(external_source_id):
        return f"external:{_normalize_text(external_source_id)}"
    if source_url and source_url.strip():
        return f"url:{_normalize_url(source_url)}"
    if source and _normalize_text(source):
        return f"name:{_normalize_text(source).casefold()}"
    if manual_content_hash:
        return f"manual-content:{manual_content_hash}"
    raise EvidenceValidationError(
        "missing_source_identity",
        "文档入库至少需要 external_source_id、source_url 或 source 之一",
    )


def _stable_id(kind: str, *parts: str) -> str:
    payload = "\0".join((kind, *parts)).encode("utf-8")
    return f"{kind}_{hashlib.sha256(payload).hexdigest()}"


def make_document_id(source_identity: str) -> str:
    return _stable_id("doc", source_identity)


def make_revision_id(
    document_id: str,
    document_content_hash: str,
    *,
    version: str | None = None,
    published_at: str | None = None,
    effective_at: str | None = None,
) -> str:
    metadata = json.dumps(
        {
            "effective_at": effective_at or "",
            "published_at": published_at or "",
            "version": version or "",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _stable_id("rev", document_id, document_content_hash, metadata)


def make_chunk_id(revision_id: str, source_span: SourceSpan, unit_content_hash: str) -> str:
    return _stable_id(
        "chunk",
        revision_id,
        str(source_span.start),
        str(source_span.end),
        unit_content_hash,
    )
