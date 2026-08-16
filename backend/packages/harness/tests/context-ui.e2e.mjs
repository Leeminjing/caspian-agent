/* Context UI 浏览器场景回归（W1 收口）：
   Escape 取消拖拽 / Alt+方向键排序 / 删除撤销 / 多来源加入与移除 /
   rail 缓存文案（— / 0%）/ 受阻 Context 重进决断。

   运行前置：
   1. 服务器运行于 http://127.0.0.1:8000
   2. playwright 可解析（如 NODE_PATH 指向安装了 playwright 的 node_modules）
   3. 由 test_context_frontend.py 的 CASPIAN_E2E=1 用例调用，或手动 node 执行
*/
import { chromium } from "playwright";
import { spawnSync } from "node:child_process";
import fs from "node:fs";

const ROOT = "C:/tmp/caspian/change49";
const EMAIL = `e2e-${Date.now()}@test.local`;
const PASSWORD = "e2e-pass-123";
const USER_ID = crypto.randomUUID();
const THREAD_A = `e2e-a-${USER_ID.slice(0, 8)}`;
const THREAD_B = `e2e-b-${USER_ID.slice(0, 8)}`;
const PY = ROOT + "/backend/packages/harness/.venv/Scripts/python.exe";
let blockedContextId = null;

function runPython(body, label) {
  const file = `C:/Users/brubing/AppData/Local/Temp/uiv-${label}.py`;
  fs.writeFileSync(file, body, "utf8");
  const r = spawnSync(PY, [file], { cwd: ROOT, encoding: "utf8", timeout: 120000, stdio: "inherit" });
  if (r.status !== 0) process.exit(1);
}

runPython(`
import asyncio, sys
sys.path.insert(0, r"${ROOT}")
asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
from dotenv import load_dotenv
load_dotenv(".env")
from backend.app.gateway.auth.security import hash_password
from backend.app.gateway.models.user import User
from backend.app.gateway.context.service import ContextService
from backend.app.gateway.context.models import WebThread
from caspian.persistence.engine import init_engine, get_session, dispose_engine
from caspian.config import get_app_config
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langchain_core.messages import HumanMessage

async def main():
    init_engine(get_app_config("config.yaml"))
    async with get_session() as session:
        session.add(User(id="${USER_ID}", email="${EMAIL}", password_hash=hash_password("${PASSWORD}"), token_version=0))
        await session.commit()
    async with AsyncPostgresSaver.from_conn_string("postgresql://caspian:qweasdzxc123@127.0.0.1:7221/caspian") as saver:
        service = ContextService(saver)
        for tid, contents in (("${THREAD_A}", ["消息甲", "消息乙"]), ("${THREAD_B}", ["消息丙"])):
            await service.register_main_run("${USER_ID}", tid)
            await saver.aput({"configurable": {"thread_id": tid, "checkpoint_ns": ""}}, {
                "v": 4, "ts": "2026-08-16T00:00:00+00:00", "id": "cp-" + tid,
                "channel_values": {"messages": [HumanMessage(content=c, id=f"e2e-{tid}-{i}") for i, c in enumerate(contents)]},
                "channel_versions": {"messages": 1}, "versions_seen": {},
            }, {}, {"messages": 1})
    # B 预置 usage：input=100、hit=0 → rail 应显示“缓存 0%”；A 无 usage → “缓存 —”
    async with get_session() as session:
        row = await session.get(WebThread, "${THREAD_B}")
        row.prompt_input_tokens = 100
        row.prompt_cache_hit_tokens = 0
        await session.commit()
    dispose_engine()

asyncio.run(main())
`, "e2e-seed");

const browser = await chromium.launch({ channel: "msedge", headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const errors = [];
page.on("pageerror", (err) => errors.push(`pageerror: ${err.message}`));
page.on("console", (msg) => { if (msg.type() === "error") errors.push(msg.text()); });

function assert(cond, label) {
  if (!cond) { console.error(`FAIL: ${label}`); process.exitCode = 1; }
  else console.log(`ok: ${label}`);
}

const order = () => page.evaluate(() =>
  [...document.querySelectorAll("#context-editor-overlay .context-message-editor")]
    .map((row) => row.querySelector(".context-message-preview")?.textContent?.slice(0, 12))
);

await page.goto("http://127.0.0.1:8000/");
await page.fill("#email", EMAIL);
await page.fill("#password", PASSWORD);
await page.click('button[type="submit"]');
await page.waitForSelector("#app-view:not([hidden])", { timeout: 15000 });
await page.evaluate((ids) => {
  localStorage.setItem("caspian.threads", JSON.stringify([
    { id: ids.a, title: "场景会话A", updatedAt: Date.now() },
    { id: ids.b, title: "场景会话B", updatedAt: Date.now() },
  ]));
}, { a: THREAD_A, b: THREAD_B });
await page.reload();
await page.waitForSelector("#app-view:not([hidden])", { timeout: 15000 });
await page.waitForTimeout(600);
await page.locator("#thread-list .thread-item", { hasText: "场景会话A" }).click();
await page.waitForTimeout(500);

// 打开派生编辑器并拷贝两条消息
await page.locator('[data-action="derive-context"]').click();
await page.waitForSelector("#context-editor-overlay:not([hidden])", { timeout: 10000 });
await page.waitForTimeout(300);
const addButtons = page.locator('[data-action="context-source-copy"]');
await addButtons.nth(0).click();
await page.waitForTimeout(150);
await addButtons.nth(1).click();
await page.waitForTimeout(300);
assert(JSON.stringify(await order()) === JSON.stringify(["消息甲", "消息乙"]), "初始顺序 甲,乙");

// 场景 1：Escape 取消拖拽
const firstHandle = page.locator("#context-editor-overlay [data-context-drag-origin='draft']").nth(0);
const hb = await firstHandle.boundingBox();
await page.mouse.move(hb.x + hb.width / 2, hb.y + hb.height / 2);
await page.mouse.down();
await page.mouse.move(hb.x + 40, hb.y + 80, { steps: 4 });
await page.waitForTimeout(200);
const dragging = await page.evaluate(() => Boolean(document.querySelector(".context-drag-preview")));
assert(dragging, "拖拽进行中（预览出现）");
await page.keyboard.press("Escape");
await page.waitForTimeout(250);
const afterEscape = await page.evaluate(() => ({
  preview: Boolean(document.querySelector(".context-drag-preview")),
  placeholder: Boolean(document.querySelector(".context-drop-placeholder")),
}));
assert(!afterEscape.preview && !afterEscape.placeholder, "Escape 清理预览与占位");
assert(JSON.stringify(await order()) === JSON.stringify(["消息甲", "消息乙"]), "Escape 后顺序不变");

// 场景 2：Alt+ArrowDown 键盘排序
await firstHandle.focus();
await page.keyboard.press("Alt+ArrowDown");
await page.waitForTimeout(250);
assert(JSON.stringify(await order()) === JSON.stringify(["消息乙", "消息甲"]), "Alt+Down 后顺序 乙,甲");

// 场景 3：删除 + 撤销
await page.locator("#context-editor-overlay .context-message-editor").nth(0)
  .locator('[data-action="context-message-delete"]').click();
await page.waitForTimeout(300);
assert(JSON.stringify(await order()) === JSON.stringify(["消息甲"]), "删除后剩 甲");
await page.locator('[data-action="context-message-undo"]').click();
await page.waitForTimeout(300);
assert(JSON.stringify(await order()) === JSON.stringify(["消息乙", "消息甲"]), "撤销后恢复 乙,甲");

// 场景 4：多来源加入与移除
await page.locator(".context-source-add summary").click();
await page.waitForTimeout(200);
await page.locator('[data-action="add-context-source"]').nth(0).click();
await page.waitForTimeout(400);
let tabCount = await page.locator(".context-source-tab").count();
assert(tabCount === 2, `加入来源后 tabs=2（实际 ${tabCount}）`);
await page.locator('[data-action="remove-context-source"]').nth(1).click();
await page.waitForTimeout(300);
tabCount = await page.locator(".context-source-tab").count();
assert(tabCount === 1, `移除来源后 tabs=1（实际 ${tabCount}）`);

// 场景 5：rail 缓存文案 — / 0%
await page.keyboard.press("Escape");
await page.waitForTimeout(300);
await page.locator("#thread-list .thread-item", { hasText: "场景会话B" }).click();
await page.waitForTimeout(500);
let railMeta = await page.locator(".context-rail-card.is-current .context-rail-meta").textContent();
assert(railMeta.includes("缓存 0%"), `B 显示“缓存 0%”（实际 ${railMeta.trim()}）`);
await page.locator("#thread-list .thread-item", { hasText: "场景会话A" }).click();
await page.waitForTimeout(500);
railMeta = await page.locator(".context-rail-card.is-current .context-rail-meta").textContent();
assert(railMeta.includes("缓存 —"), `A 显示“缓存 —”（实际 ${railMeta.trim()}）`);

// 场景 6：受阻 Context 从 rail 重进决断
blockedContextId = await page.evaluate(async (rootThread) => {
  const csrf = document.cookie.split("; ").find((p) => p.startsWith("csrf_token=")).split("=").slice(1).join("=");
  const resp = await fetch("/api/contexts/derive", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
    body: JSON.stringify({
      title: "受阻场景",
      sources: [{ context_id: rootThread, checkpoint_id: "cp-" + rootThread }],
      messages: [{ role: "tool", content: "缺协议字段" }],
    }),
  });
  const data = await resp.json();
  return data.context_id;
}, THREAD_A);
await page.locator("#thread-list .thread-item", { hasText: "场景会话A" }).click();
await page.waitForTimeout(600);
const blockedCard = page.locator(".context-rail-card.is-blocked");
assert((await blockedCard.count()) === 1, "rail 显示受阻节点");
await blockedCard.locator("xpath=..").locator('[data-action="edit-context-definition"]').click();
await page.waitForSelector("#context-editor-overlay:not([hidden])", { timeout: 10000 });
const decisionText = await page.locator(".context-definition-panel").textContent();
assert(decisionText.includes("需要你的决断"), "重进决断面板（需要你的决断）");

await page.screenshot({ path: "C:/Users/brubing/Downloads/caspian-e2e-final.png" });
console.log("page errors:", errors.slice(0, 5));
await browser.close();

runPython(`
import asyncio, sys
sys.path.insert(0, r"${ROOT}")
asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
from dotenv import load_dotenv
load_dotenv(".env")
from backend.app.gateway.models.user import User
from backend.app.gateway.context.models import WebThread, WebContextSource, WebContextDefinition
from caspian.persistence.engine import init_engine, get_session, dispose_engine
from caspian.config import get_app_config
from sqlalchemy import delete

async def main():
    init_engine(get_app_config("config.yaml"))
    blocked = "${blockedContextId}" if "${blockedContextId}" != "None" else None
    ids = ["${THREAD_A}", "${THREAD_B}"] + ([blocked] if blocked else [])
    async with get_session() as session:
        for tid in ids:
            # 按子/父双向清理：受阻 Context 的 parent FK 指向 THREAD_A（RESTRICT），
            # 先清 sources 再删线程，且必须覆盖 parent_context_id 方向
            await session.execute(delete(WebContextSource).where(
                (WebContextSource.context_id == tid) | (WebContextSource.parent_context_id == tid)
            ))
            await session.execute(delete(WebContextDefinition).where(WebContextDefinition.context_id == tid))
        for tid in ids:
            await session.execute(delete(WebThread).where(WebThread.thread_id == tid))
        await session.execute(delete(User).where(User.id == "${USER_ID}"))
        await session.commit()
    dispose_engine()

asyncio.run(main())
`, "e2e-cleanup");
console.log("E2E DONE");
