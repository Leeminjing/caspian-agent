import unittest
from html.parser import HTMLParser
from pathlib import Path

from backend.app.gateway.middleware.auth import (
    _AUTH_WHITELIST_PATHS,
    _AUTH_WHITELIST_PREFIXES,
)


STATIC_DIR = Path(__file__).resolve().parents[3] / "app" / "gateway" / "static"
ROUTERS_DIR = STATIC_DIR.parent / "routers"


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
        self.assertIn('stream_mode: ["messages", "values"]', script)
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

    def test_frontend_contains_knowledge_governance_panel(self):
        parser = IdCollector()
        parser.feed((STATIC_DIR / "index.html").read_text(encoding="utf-8"))
        self.assertIn("knowledge-template", parser.ids)
        script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn('"knowledge_governance"', script)
        self.assertIn("renderKnowledgePanel", script)
        self.assertIn("KNOWLEDGE_STATUS", script)
        self.assertIn("knowledge-ledger", script)

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
        self.assertIn("PLAN_ENTRY", picker)
        self.assertIn('replaceToken(input.value, info, token)', picker)
        self.assertIn("commitVisible(info.query)", picker)
        self.assertIn("planVisible(info.query)", picker)

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

    def test_ui_polish_assets_semantics_and_lifecycle_are_registered(self):
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        css = (STATIC_DIR / "ui-polish.css").read_text(encoding="utf-8")
        polish = (STATIC_DIR / "ui-polish.js").read_text(encoding="utf-8")
        app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        plugins = (STATIC_DIR / "plugins.js").read_text(encoding="utf-8")

        self.assertIn('/assets/ui-polish.css?v=polish-10', html)
        self.assertIn('/assets/ui-polish.js?v=polish-5', html)
        self.assertIn('id="model-toggle"', html)
        self.assertIn('id="model-popover"', html)
        self.assertIn('class="model-toggle"', html)
        self.assertIn('id="ui-status-announcer"', html)
        self.assertIn('role="status"', html)
        self.assertIn('aria-atomic="true"', html)
        self.assertIn('id="mobile-thread-toggle"', html)
        self.assertIn('id="mobile-context-toggle"', html)
        self.assertIn('id="mobile-drawer-backdrop"', html)
        self.assertIn('aria-live="off"', html)
        self.assertIn('data-ui-surface="decision-table"', html)
        self.assertIn('data-ui-surface="plugins"', html)

        self.assertIn("@media (max-width: 720px)", css)
        self.assertIn("100dvh", css)
        self.assertIn("body.mobile-threads-open .sidebar", css)
        self.assertIn("body.mobile-context-open .context-rail", css)
        self.assertIn("prefers-reduced-motion", css)

        self.assertIn('document.addEventListener("ui:surface-open"', polish)
        self.assertIn('document.addEventListener("ui:surface-close"', polish)
        self.assertIn('document.addEventListener("ui:status"', polish)
        self.assertIn("state.modalBackground", polish)
        self.assertIn("element.inert = true", polish)
        self.assertIn('event.target.closest?.(".thread-item, #new-thread")', polish)
        self.assertIn("state.followMessages", app)
        self.assertIn("if (!force && !state.followMessages) return", app)
        self.assertIn('new CustomEvent("ui:status"', app)
        self.assertIn('new CustomEvent("ui:surface-open"', app)
        self.assertIn('new CustomEvent("ui:surface-open"', plugins)

    def test_compaction_summary_rendered_as_fold(self):
        script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")

        self.assertIn("caspian_summary", script)
        self.assertIn("renderCompactionSummary", script)
        self.assertIn("upsertCompactionSummary", script)
        self.assertIn("refreshCompactionArchive", script)
        self.assertIn("buildCompactionBody", script)
        self.assertIn("compactionArchived", script)
        self.assertIn("历史已压缩", script)
        self.assertIn("已压缩的原始消息", script)
        self.assertIn('data.archived', script)
        self.assertIn("compaction_status", script)
        self.assertIn("上下文正在压缩中", script)
        self.assertIn("handleCompactionStatus", script)
        self.assertIn("toolCallsText", script)
        self.assertIn("renderToolCallItem", script)
        self.assertIn("renderToolResultItem", script)
        self.assertIn("renderedToolIds", script)
        self.assertIn("调用工具", script)
        self.assertIn("工具结果", script)
        self.assertIn(".compaction-summary {", css)
        self.assertIn(".compaction-summary summary {", css)
        self.assertIn(".compaction-status {", css)
        self.assertIn(".tool-item {", css)
        self.assertIn(".tool-item summary {", css)

    def test_model_selector_wiring(self):
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        css = (STATIC_DIR / "ui-polish.css").read_text(encoding="utf-8")
        router = (ROUTERS_DIR / "models.py").read_text(encoding="utf-8")

        self.assertIn('id="model-toggle"', html)
        self.assertIn('id="model-popover"', html)
        self.assertIn('class="model-toggle"', html)
        self.assertIn("loadModels", script)
        self.assertIn("/api/models", script)
        self.assertIn("context: state.modelName", script)
        self.assertIn("model_name", script)
        self.assertIn(".model-popover {", css)
        self.assertIn(".model-option {", css)
        self.assertIn('@router.get("/models")', router)
        self.assertIn('"models"', router)


    def test_plan_review_surface_and_wiring(self):
        markup = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")

        # 计划审阅卡模板与三条操作路径
        self.assertIn('id="plan-review-template"', markup)
        self.assertIn("plan-approve-button", markup)
        self.assertIn("plan-keep-toggle", markup)
        self.assertIn("plan-discuss-button", markup)
        self.assertIn("plan-feedback-input", markup)

        # interrupt 按 plan_review 负载分发到专用渲染；复用既有 resumeRun 通道
        self.assertIn('data?.value?.type === "plan_review"', script)
        self.assertIn("showPlanReview", script)
        self.assertIn("bindPlanReview", script)
        self.assertIn('resumeRun({ decision: "approve" })', script)
        self.assertIn('resumeRun({ decision: "keep", feedback: value })', script)
        self.assertIn('resumeRun({ decision: "dismiss" })', script)

        # 计划正文样式
        self.assertIn(".plan-review-body {", css)


if __name__ == "__main__":
    unittest.main()
