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
                "command-menu",
                "command-option-commit",
                "commitment-progress",
                "skill-picker",
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
        self.assertIn('stream_mode: ["values"]', script)
        self.assertIn("renderedMessageIds: new Set()", script)
        self.assertIn("if (state.renderedMessageIds.has(key)) return", script)
        self.assertIn('type !== "ai" && type !== "assistant"', script)
        self.assertIn('"commitment_messages"', script)
        self.assertIn("collectCommitmentMessages", script)
        self.assertIn("commitmentTraceItems", script)
        self.assertIn("existing.signature === signature", script)
        self.assertIn("reasoning_summary", script)
        self.assertIn("appendTraceOutputDelta", script)
        self.assertIn("trace-elapsed", script)
        self.assertIn("isNearBottom", script)
        self.assertIn('event === "end" && !state.pendingInterrupt', script)
        self.assertIn("selected_skills", script)
        self.assertIn("state.activeSelectedSkills", script)
        self.assertIn("window.CaspianSkills?.selectedNames", script)
        self.assertIn("event.defaultPrevented", script)
        self.assertIn('setStatus("ready", "就绪")', script)
        self.assertIn("review-contract-editor", script)
        self.assertIn("提交编辑并审核", script)
        self.assertIn(
            "payload?.stage || message.tool_calls?.[0]?.args?.stage || stage",
            script,
        )

    def test_frontend_assets_are_public(self):
        self.assertIn("/", _AUTH_WHITELIST_PATHS)
        self.assertIn("/assets/", _AUTH_WHITELIST_PREFIXES)

    def test_commit_command_menu_behavior_and_accessibility(self):
        markup = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

        self.assertIn('role="combobox"', markup)
        self.assertIn('aria-autocomplete="list"', markup)
        self.assertIn('aria-controls="command-menu"', markup)
        self.assertIn('id="command-menu" class="command-menu" role="listbox"', markup)
        self.assertIn('id="command-option-commit" class="command-option"', markup)
        self.assertIn('role="option"', markup)
        self.assertIn('aria-selected="false"', markup)

        self.assertIn('/^\\/[^\\s]*$/.test(input.value)', script)
        self.assertIn('"/commit".startsWith(input.value)', script)
        self.assertIn('input.selectionStart !== input.value.length', script)
        self.assertIn('event.key === "ArrowDown" || event.key === "ArrowUp"', script)
        self.assertIn('event.key === "Escape"', script)
        self.assertIn('addEventListener("blur", closeCommandMenu)', script)
        self.assertIn('addEventListener("mousedown"', script)
        self.assertIn('if (value || state.pendingInterrupt) closeCommandMenu()', script)
        self.assertIn('input.setAttribute("aria-expanded", "true")', script)
        self.assertIn('input.removeAttribute("aria-activedescendant")', script)

        selection = script.split("function selectCommitCommand()", 1)[1].split("\n}", 1)[0]
        self.assertIn('input.value = "/commit "', selection)
        self.assertIn("input.setSelectionRange", selection)
        self.assertNotIn("requestSubmit", selection)

    def test_skill_picker_assets_are_registered(self):
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn("/assets/skills.css", html)
        self.assertIn("/assets/skills.js", html)
        self.assertTrue((STATIC_DIR / "skills.css").exists())
        self.assertTrue((STATIC_DIR / "skills.js").exists())


if __name__ == "__main__":
    unittest.main()
