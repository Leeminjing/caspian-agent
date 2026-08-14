"""test_web_tools — web_search / web_fetch 工具的 fail-soft 与校验行为测试（mock 网络）。"""

import asyncio

import pytest

from caspian.web.tools import (
    _clamp_max_results,
    _ddgs_text_search,
    _format_search_results,
    web_fetch_tool,
    web_search_tool,
)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# web_search_tool
# ---------------------------------------------------------------------------


class TestWebSearchClamp:

    def test_clamp_lower_bound(self):
        assert _clamp_max_results(0) == 1

    def test_clamp_upper_bound(self):
        assert _clamp_max_results(99) == 10

    def test_clamp_keeps_mid(self):
        assert _clamp_max_results(5) == 5


class TestWebSearchFormat:

    def test_format_results(self):
        results = [
            {"title": "A", "href": "https://a.com", "body": "desc a"},
            {"title": "B", "href": "https://b.com", "body": "desc b"},
        ]
        text = _format_search_results(results)
        assert "1. A" in text
        assert "URL: https://a.com" in text
        assert "desc b" in text

    def test_format_empty(self):
        assert "没有找到" in _format_search_results([])


class TestWebSearchBackendFallback:

    def test_falls_back_to_next_backend(self, monkeypatch):
        """auto 返回空 → lite 返回结果 → 合并返回。"""
        calls = []
        fake_ddgs = type("FakeDDGS", (), {})
        fake_ddgs.__init__ = lambda self, timeout: None

        def fake_text(self, keywords, max_results, backend):
            calls.append(backend)
            if backend == "auto":
                return []
            return [{"title": "T", "href": f"https://{backend}.com", "body": "B"}]

        fake_ddgs.text = fake_text
        monkeypatch.setattr("duckduckgo_search.DDGS", fake_ddgs)

        results = _ddgs_text_search("q", 5)
        assert calls == ["auto", "lite", "html"]
        assert results[0]["href"] == "https://lite.com"
        assert results[1]["href"] == "https://html.com"

    def test_deduplicates_by_href(self, monkeypatch):
        """同一 href 跨 backend 出现只保留一份。"""
        fake_ddgs = type("FakeDDGS", (), {})
        fake_ddgs.__init__ = lambda self, timeout: None

        def fake_text(self, keywords, max_results, backend):
            return [
                {"title": "T", "href": "https://same.com", "body": "B"},
                {"title": "T2", "href": "https://same.com", "body": "B2"},
            ]

        fake_ddgs.text = fake_text
        monkeypatch.setattr("duckduckgo_search.DDGS", fake_ddgs)

        results = _ddgs_text_search("q", 5)
        assert len(results) == 1

    def test_ratelimit_propagates(self, monkeypatch):
        """RatelimitException 上抛给调用方显示限流提示。"""
        from duckduckgo_search.exceptions import RatelimitException

        fake_ddgs = type("FakeDDGS", (), {})
        fake_ddgs.__init__ = lambda self, timeout: None

        def fake_text(self, keywords, max_results, backend):
            raise RatelimitException("rate limit")

        fake_ddgs.text = fake_text
        monkeypatch.setattr("duckduckgo_search.DDGS", fake_ddgs)

        with pytest.raises(RatelimitException):
            _ddgs_text_search("q", 5)


class TestWebSearchFailSoft:

    def test_empty_query(self):
        result = _run(web_search_tool.ainvoke({"query": "  ", "max_results": 3}))
        assert "不能为空" in result

    def test_ratelimit_returns_readable_error(self, monkeypatch):
        class RatelimitException(Exception):
            pass

        def boom(query, max_results):
            raise RatelimitException("rate limit exceeded")

        monkeypatch.setattr("caspian.web.tools._ddgs_text_search", boom)
        result = _run(web_search_tool.ainvoke({"query": "test", "max_results": 3}))
        assert "频率受限" in result

    def test_generic_exception_returns_error(self, monkeypatch):
        def boom(query, max_results):
            raise RuntimeError("search engine down")

        monkeypatch.setattr("caspian.web.tools._ddgs_text_search", boom)
        result = _run(web_search_tool.ainvoke({"query": "test", "max_results": 3}))
        assert "搜索失败" in result

    def test_success_formats_and_clamps_max_results(self, monkeypatch):
        captured = {}

        def fake_search(query, max_results):
            captured["max_results"] = max_results
            return [{"title": "T", "href": "https://t.com", "body": "B"}]

        monkeypatch.setattr("caspian.web.tools._ddgs_text_search", fake_search)
        result = _run(web_search_tool.ainvoke({"query": "test", "max_results": 99}))
        assert captured["max_results"] == 10
        assert "1. T" in result


# ---------------------------------------------------------------------------
# web_fetch_tool
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text

    def json(self):
        if self._json_data is None:
            raise ValueError("not json")
        return self._json_data


class FakeAsyncClient:
    """记录请求头并按预置响应返回的 httpx.AsyncClient 替身。"""

    def __init__(self, timeout, response=None, error=None):
        self.timeout = timeout
        self.response = response
        self.error = error
        self.captured = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, headers=None):
        self.captured["url"] = url
        self.captured["headers"] = headers or {}
        if self.error is not None:
            raise self.error
        return self.response


class TestWebFetchProtocol:

    def test_rejects_file_url(self):
        result = _run(web_fetch_tool.ainvoke({"url": "file:///etc/passwd"}))
        assert "仅允许 http:// 与 https://" in result

    def test_rejects_data_url(self):
        result = _run(web_fetch_tool.ainvoke({"url": "data:text/plain,hi"}))
        assert "仅允许 http:// 与 https://" in result

    def test_empty_url(self):
        result = _run(web_fetch_tool.ainvoke({"url": " "}))
        assert "不能为空" in result


class TestWebFetchFailSoft:

    def test_http_error_returns_readable_error(self, monkeypatch):
        client = FakeAsyncClient(timeout=0, error=__import__("httpx").ConnectError("boom"))
        monkeypatch.setattr("caspian.web.tools.httpx.AsyncClient", lambda timeout: client)
        result = _run(web_fetch_tool.ainvoke({"url": "https://a.com"}))
        assert "网络错误" in result

    def test_non_2xx_returns_error(self, monkeypatch):
        client = FakeAsyncClient(
            timeout=0, response=FakeResponse(status_code=404, text="not found")
        )
        monkeypatch.setattr("caspian.web.tools.httpx.AsyncClient", lambda timeout: client)
        result = _run(web_fetch_tool.ainvoke({"url": "https://a.com"}))
        assert "HTTP 404" in result

    def test_business_code_non_200_returns_error(self, monkeypatch):
        client = FakeAsyncClient(
            timeout=0, response=FakeResponse(status_code=200, json_data={"code": 500})
        )
        monkeypatch.setattr("caspian.web.tools.httpx.AsyncClient", lambda timeout: client)
        result = _run(web_fetch_tool.ainvoke({"url": "https://a.com"}))
        assert "抓取失败" in result


class TestWebFetchSuccess:

    def test_returns_markdown_data(self, monkeypatch):
        client = FakeAsyncClient(
            timeout=0, response=FakeResponse(status_code=200, json_data={"code": 200, "data": "# hello"})
        )
        monkeypatch.setattr("caspian.web.tools.httpx.AsyncClient", lambda timeout: client)
        result = _run(web_fetch_tool.ainvoke({"url": "https://a.com"}))
        assert result == "# hello"

    def test_sends_expected_headers(self, monkeypatch):
        client = FakeAsyncClient(
            timeout=0, response=FakeResponse(status_code=200, json_data={"code": 200, "data": "x"})
        )
        monkeypatch.setattr("caspian.web.tools.httpx.AsyncClient", lambda timeout: client)
        _run(web_fetch_tool.ainvoke({"url": "https://a.com", "max_tokens": 2000}))
        headers = client.captured["headers"]
        assert headers["Accept"] == "application/json"
        assert headers["X-Respond-With"] == "markdown"
        assert headers["X-Max-Tokens"] == "2000"
        assert client.captured["url"] == "https://r.jina.ai/https://a.com"
