const assert = require("node:assert/strict");
const test = require("node:test");

const skills = require("../../../app/gateway/static/skills.js");

const catalog = [
  { name: "brainstorming", description: "Explore user intent" },
  { name: "data-analysis", description: "Analyze CSV and Excel" },
  { name: "docx", description: "Create and edit Word documents" },
];

test("leading slash opens and normal text slash does not", () => {
  assert.equal(skills.triggerInfo("/", 1).query, "");
  assert.equal(skills.triggerInfo("  /doc", 6).query, "doc");
  assert.equal(skills.triggerInfo("task /doc", 9), null);
  assert.equal(skills.triggerInfo("https://example.com/", 20), null);
});

test("additional leading slash after selected tokens opens", () => {
  const info = skills.triggerInfo("/docx /data", 11);
  assert.equal(info.query, "data");
  assert.equal(info.start, 6);
});

test("filter matches name and description case-insensitively", () => {
  assert.deepEqual(
    skills.filterSkills(catalog, "EXCEL").map((skill) => skill.name),
    ["data-analysis"],
  );
  assert.deepEqual(
    skills.filterSkills(catalog, "doc").map((skill) => skill.name),
    ["docx"],
  );
});

test("selected names preserve order and dedupe", () => {
  assert.deepEqual(
    skills.selectedNames("/docx /data-analysis /docx summarize", catalog),
    ["docx", "data-analysis"],
  );
});

test("remove token clears the selected slash token from the textarea", () => {
  const value = "/docx write a memo";
  const info = skills.triggerInfo(value, 5);
  assert.equal(skills.removeToken(value, info), "write a memo");
});

test("deleted tokens are recalculated from current text", () => {
  assert.deepEqual(
    skills.selectedNames("/data-analysis summarize", catalog),
    ["data-analysis"],
  );
});

test("replace token keeps slash text representation", () => {
  const value = "/do summarize";
  const info = skills.triggerInfo(value, 3);
  assert.equal(skills.replaceToken(value, info, "docx"), "/docx summarize");
});

test("messageText keeps /commit text verbatim", () => {
  assert.equal(skills.messageText("/commit 做X"), "/commit 做X");
  assert.equal(skills.messageText("  /commit  做X  "), "/commit  做X");
  assert.equal(skills.messageText("/commit"), "/commit");
});

test("messageText keeps trailing slash token inside /commit instruction", () => {
  assert.equal(skills.messageText("/commit 做X /docx"), "/commit 做X /docx");
});

test("messageText leaves non-commit text unchanged without selection", () => {
  assert.equal(skills.messageText("帮我写文档"), "帮我写文档");
  assert.equal(skills.messageText("/commitment 做X"), "/commitment 做X");
});

test("commit command visibility follows commit prefix", () => {
  assert.equal(skills.commitVisible(""), true);
  assert.equal(skills.commitVisible("c"), true);
  assert.equal(skills.commitVisible("co"), true);
  assert.equal(skills.commitVisible("commit"), true);
  assert.equal(skills.commitVisible("COMMIT"), true);
  assert.equal(skills.commitVisible("doc"), false);
  assert.equal(skills.commitVisible("comm"), true);
});
