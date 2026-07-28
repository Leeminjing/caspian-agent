import unittest
from html.parser import HTMLParser
from pathlib import Path

from backend.app.gateway.middleware.auth import (
    _AUTH_WHITELIST_PATHS,
    _AUTH_WHITELIST_PREFIXES,
)


STATIC_DIR = Path(__file__).resolve().parents[3] / "app" / "gateway" / "static"


class IdCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()

    def handle_starttag(self, _tag, attrs):
        element_id = dict(attrs).get("id")
        if element_id:
            self.ids.add(element_id)


class FrontendTests(unittest.TestCase):
    def test_workbench_contains_required_surfaces(self):
        parser = IdCollector()
        parser.feed((STATIC_DIR / "index.html").read_text(encoding="utf-8"))
        self.assertTrue(
            {
                "login-form",
                "messages",
                "composer",
                "commitment-progress",
                "review-template",
                "trace-template",
            }.issubset(parser.ids)
        )

    def test_frontend_uses_existing_stream_and_resume_protocol(self):
        script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn("/runs/stream", script)
        self.assertIn('event === "interrupt"', script)
        self.assertIn("resume: payload", script)
        self.assertIn('allowed.includes("approve")', script)
        self.assertIn("/uploads", script)
        self.assertIn('stream_mode: ["values", "custom"]', script)
        self.assertIn('event === "commitment_trace"', script)
        self.assertIn("collectCommitmentTraces", script)
        self.assertIn("reasoning_summary", script)
        self.assertIn('trace.event === "output_delta"', script)
        self.assertIn("trace-elapsed", script)
        self.assertIn("isNearBottom", script)
        self.assertIn('event === "end" && !state.pendingInterrupt', script)
        self.assertIn('setStatus("ready", "就绪")', script)

    def test_frontend_assets_are_public(self):
        self.assertIn("/", _AUTH_WHITELIST_PATHS)
        self.assertIn("/assets/", _AUTH_WHITELIST_PREFIXES)


if __name__ == "__main__":
    unittest.main()
