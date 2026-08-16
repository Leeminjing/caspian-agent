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
        self.assertIn('event === "end" && streamId === state.activeStreamId', script)
        self.assertIn("!state.pendingInterrupt && !state.interruptedByUser", script)
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

    def test_commit_command_merged_into_skill_picker(self):
        markup = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        picker = (STATIC_DIR / "skills.js").read_text(encoding="utf-8")

        self.assertIn('role="combobox"', markup)
        self.assertIn('aria-autocomplete="list"', markup)
        self.assertNotIn("command-menu", markup)
        self.assertNotIn("command-option-commit", markup)

        self.assertNotIn("closeCommandMenu", script)
        self.assertNotIn("updateCommandMenu", script)
        self.assertNotIn("selectCommitCommand", script)
        self.assertIn("event.defaultPrevented", script)
        self.assertIn('$("#composer").requestSubmit()', script)

        self.assertIn('"commit".startsWith', picker)
        self.assertIn("COMMIT_ENTRY", picker)
        self.assertIn('replaceToken(input.value, info, "commit")', picker)
        self.assertIn("commitVisible(info.query)", picker)

    def test_skill_picker_assets_are_registered(self):
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn("/assets/skills.css", html)
        self.assertIn("/assets/skills.js", html)
        self.assertTrue((STATIC_DIR / "skills.css").exists())

    def test_subtask_events_rendered_as_cards(self):
        script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")

        self.assertIn("function consumeTaskEvent", script)
        self.assertIn("function handleSubtaskEvent", script)
        self.assertIn('type.startsWith("task_")', script)
        self.assertIn('"task_started"', script)
        self.assertIn('"task_running"', script)
        self.assertIn('"task_completed"', script)
        self.assertIn("subtaskEvents.clear()", script)

        self.assertIn(".subtask-card {", css)
        self.assertIn(".subtask-steps {", css)
        self.assertTrue((STATIC_DIR / "skills.js").exists())

    def test_context_ui_assets_are_registered(self):
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn("/assets/context.css", html)
        self.assertIn("/assets/context-editor.js", html)
        self.assertIn("/assets/context-ui.js", html)
        self.assertTrue((STATIC_DIR / "context.css").exists())
        self.assertTrue((STATIC_DIR / "context-editor.js").exists())
        self.assertTrue((STATIC_DIR / "context-ui.js").exists())
        css = (STATIC_DIR / "context.css").read_text(encoding="utf-8")
        self.assertIn(".context-rail {", css)
        self.assertIn(".context-drag-preview {", css)
        self.assertIn("prefers-reduced-motion", css)


if __name__ == "__main__":
    unittest.main()
