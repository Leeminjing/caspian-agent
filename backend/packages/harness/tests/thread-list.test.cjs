/* CaspianThreadList 纯函数检查（Node 直跑，无依赖）。 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

global.window = global;
const source = path.join(__dirname, "..", "..", "..", "app", "gateway", "static", "thread-list.js");
vm.runInThisContext(fs.readFileSync(source, "utf8"));

const { mergeThreads, sortThreads } = global.CaspianThreadList;

const ids = list => list.map(item => item.id);
const order = (nodes, local) => ids(sortThreads(mergeThreads(nodes, local)));

const node = (id, updatedAt, title = null) => ({
  context_id: id,
  title,
  created_at: "2026-08-01T00:00:00+00:00",
  updated_at: updatedAt,
});

// 1. 纯服务端列表按 updated_at 倒序
assert.deepEqual(
  order(
    [
      node("old", "2026-08-01T00:00:00+00:00"),
      node("new", "2026-08-30T00:00:00+00:00"),
      node("mid", "2026-08-15T00:00:00+00:00"),
    ],
    [],
  ),
  ["new", "mid", "old"],
);

// 2. 未入库会话不再特殊置顶，按 updatedAt 参与整体倒序（陈年空会话自然下沉）
assert.deepEqual(
  order(
    [node("server", "2026-08-30T00:00:00+00:00")],
    [{ id: "stale-fresh", title: "新会话", updatedAt: Date.parse("2026-08-01T00:00:00+00:00") }],
  ),
  ["server", "stale-fresh"],
);

// 2b. 刚新建的未入库会话时间戳最新，自然排在首位
assert.deepEqual(
  order(
    [node("server", "2026-08-30T00:00:00+00:00")],
    [{ id: "just-created", title: "新会话", updatedAt: Date.parse("2026-09-01T00:00:00+00:00") }],
  ),
  ["just-created", "server"],
);

// 3. 同一 id 同时存在于两侧时只出现一次，且取服务端时间
const both = mergeThreads(
  [node("dup", "2026-08-30T00:00:00+00:00", "服务端标题")],
  [{ id: "dup", title: "本地标题", updatedAt: 0 }],
);
assert.equal(both.length, 1);
assert.equal(both[0].title, "服务端标题");
assert.equal(both[0].pending, false);
assert.equal(both[0].updatedAt, Date.parse("2026-08-30T00:00:00+00:00"));

// 4. 服务端 title 为空时回退本地标题；两者皆无时用默认标题
const fallback = mergeThreads(
  [node("a", "2026-08-30T00:00:00+00:00", null), node("b", "2026-08-30T00:00:00+00:00", "")],
  [{ id: "a", title: "本地标题", updatedAt: 0 }],
);
assert.equal(fallback.find(item => item.id === "a").title, "本地标题");
assert.equal(fallback.find(item => item.id === "b").title, "新会话");

// 5. 空输入与非法输入返回空数组，不抛异常
assert.deepEqual(mergeThreads([], []), []);
assert.deepEqual(mergeThreads(null, undefined), []);
assert.deepEqual(sortThreads(null), []);

// 6. 非法 updated_at 退化为 0，排在有效时间之后而不破坏排序
assert.deepEqual(
  order([node("bad", "not-a-date"), node("good", "2026-08-01T00:00:00+00:00")], []),
  ["good", "bad"],
);

// 7. sortThreads 不改动入参（返回副本）
const input = mergeThreads([node("x", "2026-08-01T00:00:00+00:00"), node("y", "2026-08-30T00:00:00+00:00")], []);
const snapshot = ids(input);
sortThreads(input);
assert.deepEqual(ids(input), snapshot);

console.log("thread list checks passed");
