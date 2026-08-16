/* CaspianContextEditor 纯函数检查（Node 直跑，无依赖）。 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

global.window = global;
const source = path.join(__dirname, "..", "..", "..", "app", "gateway", "static", "context-editor.js");
vm.runInThisContext(fs.readFileSync(source, "utf8"));

const editor = global.CaspianContextEditor;

const original = [{ role: "tool", content: "关注结果", tool_call_id: "call-1", name: "search" }];
const clone = editor.cloneMessages(original);
clone[0].content = "changed";
assert.equal(original[0].content, "关注结果");

const messages = [{ role: "human", content: "A" }, { role: "ai", content: "B" }];
editor.move(messages, 1, 0);
assert.equal(messages[0].role, "ai");

const uiKeys = editor.createUiKeys([{ id: "same" }, { id: "same" }, {}], (() => {
  let value = 0;
  return () => `ui-${value += 1}`;
})());
assert.deepEqual(uiKeys, ["ui-1", "ui-2", "ui-3"]);

const rendered = editor.renderMessages(original, ["ui-1"]);
assert.doesNotMatch(rendered, /data-context-message-json/);
assert.match(rendered, /data-context-pointer-handle/);
assert.match(rendered, /data-action="context-message-toggle"/);
const expanded = editor.renderMessages(original, ["ui-1"], "ui-1");
assert.match(expanded, /tool_call_id/);
assert.match(expanded, /data-context-message-json/);
const sourceRendered = editor.renderSourceMessages(original, "root");
assert.match(sourceRendered, /data-context-source-index="0"/);
assert.match(sourceRendered, /data-action="context-source-copy"/);
assert.doesNotMatch(sourceRendered, /textarea/);

const parsed = editor.readMessages({
  querySelectorAll() {
    return [{ value: JSON.stringify(original[0]), dataset: { contextMessageIndex: "0" } }];
  },
}, original);
assert.deepEqual(parsed, original);
assert.throws(
  () => editor.readMessages({ querySelectorAll: () => [{ value: "not json", dataset: { contextMessageIndex: "0" } }] }, original),
  /消息 1 不是合法 JSON/,
);

// Caspian 线程形状 { id }；树为单用户扁平列表
const threadRoot = { id: "root" };
const threadChild = { id: "child" };
const threadBlocked = { id: "blocked" };
const threadLegacy = { id: "legacy" };
const threadUnrelated = { id: "unrelated" };
const threadUnrelatedChild = { id: "unrelated-child" };
const tree = [
  { context_id: "child", parents: [{ context_id: "root" }] },
  { context_id: "blocked", parents: [{ context_id: "root" }] },
  { context_id: "root", parents: [] },
  { context_id: "unrelated", parents: [] },
  { context_id: "unrelated-child", parents: [{ context_id: "unrelated" }] },
];
assert.deepEqual(
  editor.orderTasksByTree([threadBlocked, threadChild, threadLegacy, threadUnrelatedChild, threadUnrelated, threadRoot], tree).map((t) => t.id),
  ["root", "child", "blocked", "unrelated", "unrelated-child", "legacy"],
);
assert.deepEqual(
  [...editor.contextFamilyIds(tree, "child")].sort(),
  ["blocked", "child", "root"],
);
assert.deepEqual(
  [...editor.contextFamilyIds(tree, "legacy")],
  [],
);
assert.deepEqual(
  [...editor.contextFamilyIds(tree, "unrelated-child")].sort(),
  ["unrelated", "unrelated-child"],
);

console.log("context editor checks passed");
