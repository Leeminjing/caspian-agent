"""
本文件对外提供 build_retrieval_text 纯函数，构造唯一允许进入 embedding 的文本。

输入:
    content — 原文证据；title、section_path、version、published_at、effective_at —
    可选语义和时态上下文。

输出:
    str — 由固定标签和非空白名单字段稳定拼接的 retrieval_text。

具体工作流:
    按 Title、Section、Version、Published、Effective、Content 的固定顺序渲染；来源、
    等级、评级依据、provenance 和其它治理字段不属于函数输入，因而不能泄漏到向量。

示例:
    text = build_retrieval_text("ref 可作为 prop。", title="React", version="19")
"""

from collections.abc import Sequence


def build_retrieval_text(
    content: str,
    *,
    title: str = "",
    section_path: Sequence[str] = (),
    version: str | None = None,
    published_at: str | None = None,
    effective_at: str | None = None,
) -> str:
    fields: tuple[tuple[str, str], ...] = (
        ("Title", title),
        ("Section", " > ".join(section_path)),
        ("Version", version or ""),
        ("Published", published_at or ""),
        ("Effective", effective_at or ""),
    )
    lines = [f"{label}: {value}" for label, value in fields if value]
    lines.append(f"Content:\n{content}")
    return "\n".join(lines)
