/* CaspianImagePaste 纯函数检查（Node 直跑，无依赖）。 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

global.window = global;
const source = path.join(__dirname, "..", "..", "..", "app", "gateway", "static", "image-paste.js");
vm.runInThisContext(fs.readFileSync(source, "utf8"));

const { buildUserMessageContent } = global.CaspianImagePaste;

// 1. 无图片 → 返回原字符串（向后兼容），空白保留
assert.equal(buildUserMessageContent("看图"), "看图");
assert.equal(buildUserMessageContent(""), "");

// 2. 有图片 + 文本 → [text 块, image_url 块]
const blocks = buildUserMessageContent("看图", [{ dataUrl: "data:image/png;base64,AA==" }]);
assert.equal(blocks.length, 2);
assert.deepEqual(blocks[0], { type: "text", text: "看图" });
assert.deepEqual(blocks[1], { type: "image_url", image_url: { url: "data:image/png;base64,AA==" } });

// 3. 只有图片、无文本 → 只产 image_url 块，不产生空 text 块
const onlyImg = buildUserMessageContent("", [{ dataUrl: "data:image/png;base64,AA==" }]);
assert.equal(onlyImg.length, 1);
assert.deepEqual(onlyImg[0], { type: "image_url", image_url: { url: "data:image/png;base64,AA==" } });

// 4. 多图顺序保持
const multi = buildUserMessageContent("x", [{ dataUrl: "a" }, { dataUrl: "b" }]);
assert.equal(multi.length, 3);
assert.equal(multi[1].image_url.url, "a");
assert.equal(multi[2].image_url.url, "b");

// 5. 缺 dataUrl 的项被跳过
const skip = buildUserMessageContent("x", [{ dataUrl: "" }, { dataUrl: "b" }]);
assert.equal(skip.length, 2);

console.log("image paste checks passed");
