"""本文件验证决策等级表面板的实际浏览器交互与状态区分。

输入为模拟认证、当前表、历史、权限、调用及复核 API；输出为可见 DOM 和用户点击后的详情。
工作流在本地 Chromium 加载真实网关静态页，拦截 API，再检查空表、历史追溯、故障及审计展示。
示例：`pytest tests/test_decision_table_ui.py`。
"""

import json
from pathlib import Path
from urllib.parse import urlparse

import pytest

playwright = pytest.importorskip("playwright.sync_api")


STATIC = Path(__file__).resolve().parents[3] / "app" / "gateway" / "static"


def test_decision_panel_shows_current_history_approval_permissions_and_audit():
    current = {"read_status": "verified_empty", "revision": 0, "rows": []}
    operation = {
        "operation_id": "op-1", "run_id": "run-b", "status": "committed",
        "reason": "加强保密", "base_revision": 0, "result_revision": 1,
        "candidate_rows": [{"id": "r", "requirement": "必须加密", "decision": "保留", "priority": 3}],
        "changes": [{"type": "add", "row_id": "r"}], "checks": [],
    }
    detail = {
        "operation": operation,
        "run": {"run_id": "run-b", "origin_run_id": "run-b", "kind": "decision_edit", "status": "success"},
        "approvals": [{"approval_id": "approval-1", "decision": "approve", "candidate_hash": "hash"}],
        "events": [{"event_type": "committed", "payload": {"revision": 1}}],
        "before": [], "after": operation["candidate_rows"],
    }
    api = {
        "/api/auth/me": {"user": {"id": "u", "display_name": "User", "email": "u@example.test"}},
        "/api/contexts/tree": [],
        "/api/models": {"models": []},
        "/api/threads/t/messages": {"messages": []},
        "/api/threads/t/decision-table": current,
        "/api/threads/t/decision-table/permissions": {"revision": 1, "policy": {"subjects": {"user:u": {"view": ["add"]}}}},
        "/api/threads/t/decision-table/operations": {"operations": [operation, {
            **operation, "operation_id": "op-2", "run_id": "run-c", "status": "awaiting_approval",
            "reason": "候选未生效", "result_revision": None,
        }, {
            **operation, "operation_id": "op-3", "run_id": "run-d", "status": "version_conflict",
            "reason": "旧版本提交", "result_revision": None,
        }, {
            **operation, "operation_id": "op-4", "run_id": "run-e", "status": "rejected",
            "reason": "无权修改", "result_revision": None, "error": "权限不足",
        }]},
        "/api/threads/t/decision-table/operations/op-1": detail,
        "/api/threads/t/decision-table/model-calls": {"calls": [{"call_id": "call-1", "run_id": "run-a", "table_revision": 0, "status": "completed", "reminder_reason": None}]},
        "/api/threads/t/decision-table/action-reviews": {"reviews": [{"review_id": "review-1", "action_name": "bash_tool", "action_args": {"command": "echo checked"}, "run_id": "run-a", "table_revision": 1, "status": "executed"}, {"review_id": "review-2", "action_name": "bash_tool", "action_args": {"command": "alembic upgrade head"}, "run_id": "run-a", "table_revision": 1, "risk_outcome": "high", "risk_reason": "数据库迁移会改变持久资源", "status": "paused"}]},
    }

    def route_request(route):
        path = urlparse(route.request.url).path
        if path in api:
            value = api[path]
            status, value = value if isinstance(value, tuple) else (200, value)
            route.fulfill(status=status, content_type="application/json", body=json.dumps(value, ensure_ascii=False))
            return
        if path.startswith("/api/"):
            route.fulfill(status=200, content_type="application/json", body="{}")
            return
        file = STATIC / ("index.html" if path == "/" else path.removeprefix("/assets/"))
        if not file.is_file() or not file.resolve().is_relative_to(STATIC.resolve()):
            route.fulfill(status=404, body="not found")
            return
        route.fulfill(status=200, path=str(file))

    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True)
        page = browser.new_page()
        page.add_init_script("""localStorage.setItem('caspian.current_thread', 't');
            localStorage.setItem('caspian.threads', JSON.stringify([{id:'t', title:'Test', updatedAt:1}]));""")
        page.route("https://caspian.test/**", route_request)
        try:
            page.goto("https://caspian.test/", wait_until="domcontentloaded")
            page.locator("#decision-table-toggle").click()
            page.locator("#decision-table-version").get_by_text("当前为空表 · 修订 0").wait_for()
            page.get_by_text("改表权限 · 修订 1").wait_for()
            page.get_by_text("模型 call-1").wait_for()
            page.get_by_text("bash_tool · 旧版复核 · 已执行", exact=False).wait_for()
            page.get_by_text("bash_tool · 高影响复核 · 已暂停，未执行", exact=False).wait_for()
            page.get_by_text("bash_tool · 旧版复核 · 已执行", exact=False).click()
            page.get_by_text('"command": "echo checked"').wait_for()
            page.get_by_role("button", name="已生效 · 加强保密 · Run run-b").click()
            page.get_by_text('"kind": "decision_edit"').wait_for()
            page.get_by_text('"approval_id": "approval-1"').wait_for()
            page.get_by_text('"before": []').wait_for()
            page.get_by_text('"after": [').wait_for()
            page.get_by_role("button", name="等待审批 · 候选未生效 · Run run-c").wait_for()
            page.get_by_role("button", name="版本冲突 · 旧版本提交 · Run run-d").wait_for()
            page.get_by_role("button", name="权限不足 · 无权修改 · Run run-e").wait_for()
            current.update({"read_status": "verified_current", "revision": 1, "rows": operation["candidate_rows"]})
            page.locator("#decision-table-toggle").click()
            page.locator("#decision-table-toggle").click()
            page.locator("#decision-table-version").get_by_text("当前有效 · 修订 1").wait_for()
            assert page.locator('#decision-table-body [data-field="requirement"]').input_value() == "必须加密"
            api["/api/threads/t/decision-table"] = (503, {"detail": "权威快照校验失败"})
            page.locator("#decision-table-toggle").click()
            page.locator("#decision-table-toggle").click()
            page.locator("#decision-table-version").get_by_text("读取失败").wait_for()
            page.get_by_text("决策表读取或校验失败：权威快照校验失败").wait_for()
        finally:
            browser.close()
