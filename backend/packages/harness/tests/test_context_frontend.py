"""Recursive Context Forking 前端表面检查与纯函数 Node 测试。"""

import os
import shutil
import subprocess
import unittest
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parents[3] / "app" / "gateway" / "static"
NODE_TEST = Path(__file__).resolve().with_name("context-editor.test.cjs")
E2E_SCRIPT = Path(__file__).resolve().with_name("context-ui.e2e.mjs")


class ContextFrontendTests(unittest.TestCase):
    def test_index引入context资源与脚本顺序(self):
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="/assets/context.css?v=f49"', html)
        self.assertIn('src="/assets/context-editor.js?v=f49"', html)
        self.assertIn('src="/assets/context-ui.js?v=rename-2"', html)
        self.assertLess(
            html.index("context-ui.js"),
            html.index("app.js"),
        )

    def test_appjs包含Context集成点(self):
        script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn("CaspianContextUi?.init(", script)
        self.assertIn("CaspianContextUi?.onThreadSelected()", script)
        self.assertIn("CaspianContextUi?.onRunEnded()", script)
        self.assertIn('detail?.code === "context_projection_blocked"', script)
        self.assertIn("CaspianContextUi?.openDecisionFromBlock(detail)", script)
        self.assertIn("CaspianContextUi.orderThreads(state.threads)", script)

    def test_appjs_reasoning_panel_keyed_by_message_id(self):
        # 回归守卫：推理必须挂到消息自身的气泡（agentArticle 按 data-message-id 定位/新建），
        # 不得经模糊回退挂到上一条消息（曾导致空正文 AI 消息的推理/工具调用错位到兄弟气泡）。
        # agent 气泡内容收进 .message-body 块级容器，避免 flex 布局把推理/工具调用并排错乱。
        # 推理展示为内联「· Think」流式行（.think-line），流式逐 token 追加、完成态可展开收起。
        script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn("function renderAgentMessage(message)", script)
        self.assertIn("function agentArticle(id)", script)
        self.assertIn("function agentBody(article)", script)
        self.assertIn('data-message-id="${CSS.escape(id)}"', script)
        self.assertIn("renderToolCallItem(call, id, h.body)", script)
        # 内联 Think 行 + 统一交互对象（单路径）：流式与 values 整帧共享同一批元素
        self.assertIn("function ensureAgentHandle(id)", script)
        self.assertIn("function ensureThinkLine(h)", script)
        self.assertIn("function tailSummary(text, max)", script)
        self.assertIn("think-badge", script)
        self.assertIn("think-excerpt", script)
        # 回归守卫：think-line 默认收起（不自动弹出）；推理仍逐 token 追加到 <pre>，点开可见完整推理
        self.assertNotIn('line.setAttribute("open", "")', script)
        self.assertNotIn("line.open = true", script)
        # 双流模式（messages 逐 token + values 整帧）
        self.assertIn("function consumeTokenChunk(data)", script)
        self.assertIn('stream_mode: ["messages", "values"]', script)
        self.assertIn("if (event === \"stream\") consumeTokenChunk(data);", script)
        # 回归守卫：流式 AIMessageChunk 的 type 是 "AIMessageChunk"，不能因严格 === "ai"/"assistant"
        # 而被跳过（曾导致推理增量全被忽略、只剩 values 整帧一次性渲染）
        self.assertIn("if (!/ai|assistant/i.test(tokenType)) return;", script)
        css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
        self.assertIn(".message-body", css)
        self.assertIn(".message-body .tool-item", css)
        self.assertIn(".think-line", css)
        self.assertIn(".think-badge", css)

    def test_context_ui包含rail编辑器与拖拽(self):
        script = (STATIC_DIR / "context-ui.js").read_text(encoding="utf-8")
        self.assertIn("/api/contexts/tree", script)
        self.assertIn("/api/contexts/derive", script)
        self.assertIn('data-action="derive-context"', script)
        self.assertIn("setPointerCapture", script)
        self.assertIn('"(prefers-reduced-motion: reduce)"', script)
        self.assertIn("requestAnimationFrame", script)
        self.assertIn(".animate([", script)
        self.assertIn('data-action="accept-context-projection"', script)
        self.assertIn('data-action="cancel-context-projection"', script)
        self.assertIn('overlay.setAttribute("role", "dialog")', script)
        self.assertIn('overlay.setAttribute("aria-modal", "true")', script)
        self.assertIn('overlay.dataset.uiSurface = "context-editor"', script)
        self.assertIn('new CustomEvent("ui:surface-open"', script)
        self.assertIn('new CustomEvent("ui:surface-close"', script)
        self.assertIn('new CustomEvent("ui:context-rail-change"', script)

    def test_context_css_editor_overlay_is_fixed_overlay_with_hidden_guard(self):
        css = (STATIC_DIR / "context.css").read_text(encoding="utf-8")
        # 回归守卫：overlay 必须 fixed 定位盖住视口（曾因 static 定位渲染在首屏之下导致“打开无反应”），
        # 且作者 display:grid 压过 UA [hidden] 时必须显式兜底
        self.assertIn("position: fixed", css)
        self.assertIn("inset: 0", css)
        self.assertIn(".context-editor-view[hidden]", css)
        self.assertIn("display: none !important", css)

    def test_context_css_drop_placeholder_is_absolute_floating(self):
        # 回归守卫：拖拽占位条必须绝对定位悬浮（曾因占位条占布局空间推移卡片，
        # 导致 dropIndex 震荡、拖拽换位失效与视觉闪烁）
        css = (STATIC_DIR / "context.css").read_text(encoding="utf-8")
        block = css.split(".context-drop-placeholder {", 1)[1].split("}", 1)[0]
        self.assertIn("position: absolute", block)
        self.assertIn("pointer-events: none", block)
        self.assertIn(".context-message-list {\n  position: relative;", css)

    @unittest.skipUnless(shutil.which("node"), "node 不可用，跳过 JS 纯函数测试")
    def test_context_editor_pure_functions(self):
        result = subprocess.run(
            ["node", str(NODE_TEST)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"context-editor.test.cjs 失败:\n{result.stdout}\n{result.stderr}",
        )
        self.assertIn("context editor checks passed", result.stdout)

    @unittest.skipUnless(
        os.environ.get("CASPIAN_E2E") == "1" and shutil.which("node"),
        "设置 CASPIAN_E2E=1 与 CASPIAN_E2E_NODE_DIR（装有 playwright 的目录）且服务器运行于 127.0.0.1:8000 时执行浏览器场景回归",
    )
    def test_context_ui_e2e_browser(self):
        node_dir = os.environ.get("CASPIAN_E2E_NODE_DIR")
        self.assertIsNotNone(node_dir, "CASPIAN_E2E_NODE_DIR 未设置")
        target = Path(node_dir) / "context-ui.e2e.mjs"
        shutil.copy2(E2E_SCRIPT, target)
        try:
            result = subprocess.run(
                ["node", str(target)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
            )
        finally:
            target.unlink(missing_ok=True)
        self.assertEqual(
            result.returncode,
            0,
            f"context-ui.e2e.mjs 失败 (returncode={result.returncode}):\n{result.stdout}\n{result.stderr}",
        )
        self.assertIn("E2E DONE", result.stdout or "")


if __name__ == "__main__":
    unittest.main()
