"""
本文件对外提供 web_search_tool 与 web_fetch_tool 两个模块级联网 @tool 函数，
作为 config.yaml tools 段（group: web）的声明式加载目标，供 resolve_class 直接解析。

对外提供:
    web_search_tool — DuckDuckGo 关键词搜索（无需 API key），返回编号格式化结果
    web_fetch_tool — Jina Reader 单页抓取（无需 API key），返回可读性提取后的 Markdown 文本

输入:
    web_search_tool:
        query: str — 搜索关键词
        max_results: int — 返回结果条数（自动钳制到 [1, 10]）
    web_fetch_tool:
        url: str — 目标页面 URL（仅接受 http:// 与 https:// 协议）
        max_tokens: int | None — 可选，限制返回内容的 token 数（Jina X-Max-Tokens）

输出:
    str — 成功时返回搜索/抓取内容文本；失败时返回可读错误字符串（fail-soft，不抛异常）

具体工作流:
    web_search_tool:
    (1) 钳制 max_results 到 [1, 10]
    (2) 经 asyncio.to_thread 在线程池执行同步 DDGS().text()（避免阻塞 worker 事件循环）
    (3) 捕获 RatelimitException 与 DuckDuckGoSearchException/其他异常 → 返回可读错误串
    (4) 成功 → 编号格式化（标题 / URL / 摘要），无结果 → 说明性文本

    web_fetch_tool:
    (1) 校验 url 必须以 http:// 或 https:// 开头，拒绝 file:// / data: 等协议
    (2) httpx GET https://r.jina.ai/{url}，携带 Accept: application/json 与 X-Respond-With: markdown
    (3) max_tokens 提供时追加 X-Max-Tokens 请求头；60 秒超时
    (4) 非 2xx / 网络异常 / 业务 code 非 200 → 可读错误串；成功 → 返回 data 字段文本

示例:
    result = await web_search_tool.ainvoke({"query": "LangGraph 文档", "max_results": 5})
    content = await web_fetch_tool.ainvoke({"url": "https://example.com/doc", "max_tokens": 2000})
"""

import asyncio
import logging
from typing import Any

import httpx
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

_MIN_RESULTS = 1
_MAX_RESULTS = 10
_FETCH_TIMEOUT_SECONDS = 60.0
_JINA_READER_BASE = "https://r.jina.ai/"


def _clamp_max_results(max_results: int) -> int:
    """将 max_results 钳制到 [1, 10]（受保护 helper）。

    输入:
        max_results: int — 调用方传入的结果条数

    输出:
        int — 钳制后的值
    """
    return max(_MIN_RESULTS, min(_MAX_RESULTS, int(max_results)))


def _is_http_url(url: str) -> bool:
    """判断 URL 是否 http(s) 协议（受保护 helper）。

    输入:
        url: str — 待校验的 URL

    输出:
        bool — True 表示以 http:// 或 https:// 开头
    """
    return url.startswith(("http://", "https://"))


def _ddgs_text_search(query: str, max_results: int) -> list[dict[str, str]]:
    """同步执行 DuckDuckGo 文本搜索（受保护 helper，供 asyncio.to_thread 包装）。

    输入:
        query: str — 搜索关键词
        max_results: int — 结果条数

    输出:
        list[dict[str, str]] — [{title, href, body}, ...]

    工作流:
        (1) 依次尝试 auto / lite / html 三个 backend（auto 反爬随机性高，单次常返回空）
        (2) 单 backend 失败或空结果 → 继续尝试下一个 backend
        (3) 按 href 去重合并结果，达到 max_results 提前停止
        (4) RatelimitException 直接上抛，由调用方显示频率受限提示

    异常:
        duckduckgo_search 的 RatelimitException — 由调用方捕获显示限流提示
    """
    from duckduckgo_search import DDGS
    from duckduckgo_search.exceptions import RatelimitException

    seen: set[str] = set()
    results: list[dict[str, str]] = []
    for backend in ("auto", "lite", "html"):
        try:
            batch = DDGS(timeout=30).text(
                keywords=query, max_results=max_results, backend=backend
            )
        except RatelimitException:
            raise
        except Exception:
            continue
        for item in batch:
            href = str(item.get("href", ""))
            if href and href not in seen:
                seen.add(href)
                results.append(item)
        if len(results) >= max_results:
            break
    return results[:max_results]


def _format_search_results(results: list[dict[str, str]]) -> str:
    """将搜索结果格式化为编号文本（受保护 helper）。

    输入:
        results: list[dict[str, str]] — DDGS.text 返回的结果列表

    输出:
        str — 编号格式化文本；空列表时返回说明性文本
    """
    if not results:
        return "没有找到相关搜索结果。"
    lines: list[str] = []
    for index, item in enumerate(results, start=1):
        title = str(item.get("title", "")).strip()
        href = str(item.get("href", "")).strip()
        body = str(item.get("body", "")).strip()
        lines.append(f"{index}. {title}\n   URL: {href}\n   {body}")
    return "\n\n".join(lines)


@tool
async def web_search_tool(query: str, max_results: int = 5) -> str:
    """通过 DuckDuckGo 搜索网页并返回结构化结果（标题/URL/摘要），无需 API key。

    Args:
        query: 搜索关键词
        max_results: 返回结果条数（自动钳制到 1-10）
    """
    if not isinstance(query, str) or not query.strip():
        return "搜索失败: query 不能为空。"
    try:
        results: list[dict[str, str]] = await asyncio.to_thread(
            _ddgs_text_search, query.strip(), _clamp_max_results(max_results)
        )
    except Exception as exc:
        name = type(exc).__name__
        if "ratelimit" in name.lower() or "rate" in str(exc).lower():
            return f"搜索失败: DuckDuckGo 频率受限（{name}），请稍后重试。"
        logger.warning("web_search 失败: %s: %s", name, exc)
        return f"搜索失败（{name}）: {exc}"
    return _format_search_results(results)


@tool
async def web_fetch_tool(url: str, max_tokens: int | None = None) -> str:
    """通过 Jina Reader 抓取单页 URL 内容，返回可读性提取后的 Markdown 文本，无需 API key。

    Args:
        url: 目标页面 URL（仅支持 http:// 与 https://）
        max_tokens: 可选，限制返回内容的最大 token 数
    """
    if not isinstance(url, str) or not url.strip():
        return "抓取失败: url 不能为空。"
    url = url.strip()
    if not _is_http_url(url):
        return f"抓取失败: URL 协议不支持 '{url.split(':', 1)[0]}://'，仅允许 http:// 与 https://"

    headers: dict[str, str] = {
        "Accept": "application/json",
        "X-Respond-With": "markdown",
    }
    if max_tokens is not None and int(max_tokens) > 0:
        headers["X-Max-Tokens"] = str(int(max_tokens))

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(_FETCH_TIMEOUT_SECONDS)) as client:
            response = await client.get(_JINA_READER_BASE + url, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("web_fetch 网络错误: %s: %s", type(exc).__name__, exc)
        return f"抓取失败（网络错误 {type(exc).__name__}）: {exc}"

    if response.status_code != 200:
        return f"抓取失败（HTTP {response.status_code}）: {response.text[:200]}"

    try:
        payload: dict[str, Any] = response.json()
    except ValueError:
        return response.text

    if payload.get("code") != 200:
        return f"抓取失败: {payload}"
    data = payload.get("data")
    if data is None:
        return "抓取失败: 响应缺少 data 字段。"
    return str(data)
