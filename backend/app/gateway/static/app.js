const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const STAGES = {
  1: "明确用户目标",
  2: "汇总要求与矛盾",
  3: "确认要求等级",
  4: "汇总必要输入",
  5: "确认技术版本",
  6: "确认理论知识",
  7: "写入任务合同",
  8: "生成最终合同",
  9: "移交 Lead Agent",
};

const TRACE_ACTORS = {
  supervisor: "Supervisor",
  worker: "Worker",
  evaluator: "Evaluator",
  tool: "工具",
  human: "人工",
  system: "系统",
};

const state = {
  user: null,
  threads: JSON.parse(localStorage.getItem("caspian.threads") || "[]"),
  threadId: null,
  running: false,
  pendingInterrupt: null,
  uploads: [],
  renderedMessageIds: new Set(),
  tracePanel: null,
};

function csrfToken() {
  const item = document.cookie
    .split("; ")
    .find((part) => part.startsWith("csrf_token="));
  return item ? decodeURIComponent(item.split("=").slice(1).join("=")) : "";
}

function threadId() {
  return crypto.randomUUID
    ? crypto.randomUUID()
    : `thread-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function saveThreads() {
  localStorage.setItem("caspian.threads", JSON.stringify(state.threads.slice(0, 20)));
}

function currentThread() {
  return state.threads.find((item) => item.id === state.threadId);
}

function ensureThread() {
  if (state.threadId) return;
  const thread = { id: threadId(), title: "新会话", updatedAt: Date.now() };
  state.threads.unshift(thread);
  state.threadId = thread.id;
  saveThreads();
  renderThreads();
}

function renderThreads() {
  const list = $("#thread-list");
  list.replaceChildren();
  state.threads.forEach((thread) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `thread-item${thread.id === state.threadId ? " active" : ""}`;
    button.innerHTML = `<strong></strong><span></span>`;
    $("strong", button).textContent = thread.title;
    $("span", button).textContent = thread.id.slice(0, 13);
    button.addEventListener("click", () => selectThread(thread.id));
    list.append(button);
  });
  const thread = currentThread();
  $("#thread-title").textContent = thread?.title || "新会话";
  $("#thread-id").textContent = state.threadId || "";
}

function selectThread(id) {
  if (state.running) return;
  state.threadId = id;
  state.pendingInterrupt = null;
  state.uploads = [];
  state.renderedMessageIds.clear();
  state.tracePanel = null;
  $("#messages").replaceChildren(emptyState());
  renderAttachments();
  renderThreads();
  setProgress(0, false);
}

function emptyState() {
  const original = $("#empty-state");
  if (original?.isConnected) return original;
  const wrapper = document.createElement("div");
  wrapper.id = "empty-state";
  wrapper.className = "empty-state";
  wrapper.innerHTML = `
    <span class="empty-mark" aria-hidden="true">C</span>
    <h2>把目标交给 Caspian</h2>
    <div class="prompt-row">
      <button type="button" data-prompt="分析这个项目并完成当前需求">分析项目需求</button>
      <button type="button" data-prompt="根据我提供的资料制定并执行任务">基于资料执行</button>
    </div>`;
  bindPrompts(wrapper);
  return wrapper;
}

function bindPrompts(root = document) {
  $$("[data-prompt]", root).forEach((button) => {
    button.addEventListener("click", () => {
      $("#message-input").value = button.dataset.prompt;
      resizeComposer();
      $("#message-input").focus();
    });
  });
}

function setStatus(kind, label) {
  const element = $("#run-status");
  element.className = `status status-${kind}`;
  element.innerHTML = "<span></span>";
  element.append(document.createTextNode(label));
}

function setProgress(stage, visible = true) {
  const panel = $("#commitment-progress");
  panel.hidden = !visible;
  if (!visible) return;
  const list = $("#progress-steps");
  list.replaceChildren();
  for (let number = 1; number <= 9; number += 1) {
    const item = document.createElement("li");
    item.className = number < stage ? "done" : number === stage ? "active" : "";
    item.title = `${number}. ${STAGES[number]}`;
    list.append(item);
  }
  $("#progress-label").textContent = STAGES[stage] || "正在准备任务合同";
}

function setBusy(value) {
  state.running = value;
  $("#message-input").disabled = value || Boolean(state.pendingInterrupt);
  $("#send-button").disabled = value || Boolean(state.pendingInterrupt);
  $("#attach-button").disabled = value || Boolean(state.pendingInterrupt);
  if (value) setStatus("running", "处理中");
}

function removeEmptyState() {
  $("#empty-state")?.remove();
}

function addMessage(role, content, id = "") {
  const text = String(content || "").trim();
  if (!text) return;
  const key = id || `${role}:${text}`;
  if (state.renderedMessageIds.has(key)) return;
  state.renderedMessageIds.add(key);
  removeEmptyState();
  const message = document.createElement("article");
  message.className = `message message-${role}`;
  const body = document.createElement("div");
  body.className = "message-content";
  body.textContent = text;
  message.append(body);
  $("#messages").append(message);
  scrollMessages();
}

function showThinking() {
  removeThinking();
  const item = document.createElement("div");
  item.id = "thinking";
  item.className = "message message-agent";
  item.innerHTML = '<div class="thinking"><span>处理中</span><i></i><i></i><i></i></div>';
  $("#messages").append(item);
  scrollMessages();
}

function removeThinking() {
  $("#thinking")?.remove();
}

function scrollMessages() {
  const messages = $("#messages");
  requestAnimationFrame(() => {
    messages.scrollTop = messages.scrollHeight;
  });
}

function contentText(content) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .map((item) => typeof item === "string" ? item : item?.text || "")
    .filter(Boolean)
    .join("\n");
}

function collectMessages(value, found = []) {
  if (!value || typeof value !== "object") return found;
  if (Array.isArray(value)) {
    value.forEach((item) => collectMessages(item, found));
    return found;
  }
  if (Array.isArray(value.messages)) {
    value.messages.forEach((message) => found.push(message));
  }
  Object.values(value).forEach((item) => collectMessages(item, found));
  return found;
}

function consumeGraphEvent(data) {
  const traces = collectCommitmentTraces(data);
  if (traces.length) {
    traces.forEach(appendCommitmentTrace);
    return;
  }
  collectMessages(data).forEach((message) => {
    const type = message.type || message.role;
    if (type !== "ai" && type !== "assistant") return;
    const text = contentText(message.content);
    if (text) addMessage("agent", text, message.id);
  });
}

function collectCommitmentTraces(value, found = []) {
  if (!value || typeof value !== "object") return found;
  if (value.type === "commitment_trace") {
    found.push(value);
    return found;
  }
  Object.values(value).forEach((item) => collectCommitmentTraces(item, found));
  return found;
}

function appendCommitmentTrace(trace) {
  removeEmptyState();
  if (!state.tracePanel?.isConnected) {
    const fragment = $("#trace-template").content.cloneNode(true);
    state.tracePanel = $(".trace-panel", fragment);
    const thinking = $("#thinking");
    thinking ? thinking.before(state.tracePanel) : $("#messages").append(state.tracePanel);
  }
  const item = document.createElement("li");
  item.className = `trace-item trace-${trace.actor} trace-${trace.status}`;
  const meta = document.createElement("div");
  meta.className = "trace-meta";
  const actor = document.createElement("span");
  actor.className = "trace-actor";
  actor.textContent = TRACE_ACTORS[trace.actor] || trace.actor;
  const position = document.createElement("span");
  position.textContent = `第 ${trace.stage} 步${trace.attempt ? ` · 第 ${trace.attempt} 轮` : ""}`;
  meta.append(actor, position);
  const title = document.createElement("strong");
  title.textContent = trace.title;
  item.append(meta, title);
  if (trace.detail) {
    const detail = document.createElement("p");
    detail.textContent = trace.detail;
    item.append(detail);
  }
  if (trace.payload?.reasoning_summary) {
    const reasoning = document.createElement("p");
    reasoning.className = "trace-reasoning";
    reasoning.textContent = trace.payload.reasoning_summary;
    item.append(reasoning);
  }
  if (trace.payload !== undefined) {
    const payload = document.createElement("details");
    payload.className = "trace-payload";
    payload.innerHTML = "<summary>查看输入与输出</summary><pre></pre>";
    $("pre", payload).textContent = prettyDraft(trace.payload);
    item.append(payload);
  }
  $(".trace-list", state.tracePanel).append(item);
  $(".trace-count", state.tracePanel).textContent =
    `${$$(".trace-item", state.tracePanel).length} 项`;
  setProgress(Number(trace.stage || 0), true);
  scrollMessages();
}

function prettyDraft(value) {
  if (typeof value === "string") return value;
  return JSON.stringify(value ?? {}, null, 2);
}

function showReview(interrupt) {
  removeThinking();
  state.pendingInterrupt = interrupt;
  const payload = interrupt.value || {};
  const stage = Number(payload.stage || 0);
  setBusy(false);
  setStatus("review", "等待确认");
  setProgress(stage, true);
  removeEmptyState();

  const fragment = $("#review-template").content.cloneNode(true);
  const panel = $(".review-panel", fragment);
  $(".review-kicker", panel).textContent = `第 ${stage} 步`;
  $("h3", panel).textContent = STAGES[stage] || "人工确认";
  $(".review-draft", panel).textContent = prettyDraft(payload.draft);
  $(".review-error", panel).textContent = payload.error || "";
  const allowed = payload.allowed_decisions || ["approve", "revise"];
  $(".approve-button", panel).hidden = !allowed.includes("approve");
  if (payload.revise_label || !allowed.includes("approve")) {
    $(".revise-toggle", panel).textContent =
      payload.revise_label || (stage === 2 ? "解决矛盾" : "提出修订");
  }
  bindReview(panel);
  $("#messages").append(panel);
  scrollMessages();
}

function bindReview(panel) {
  const form = $(".revision-form", panel);
  const input = $(".revision-input", panel);
  let mode = "feedback";

  $(".approve-button", panel).addEventListener("click", () => {
    disableReview(panel);
    resumeRun({ decision: "approve" });
  });

  $(".revise-toggle", panel).addEventListener("click", () => {
    $(".review-actions", panel).hidden = true;
    form.hidden = false;
    input.focus();
  });

  $(".cancel-revision", panel).addEventListener("click", () => {
    form.hidden = true;
    $(".review-actions", panel).hidden = false;
  });

  $$(".segmented button", panel).forEach((button) => {
    button.addEventListener("click", () => {
      mode = button.dataset.mode;
      $$(".segmented button", panel).forEach((item) => item.classList.toggle("active", item === button));
      input.value = "";
      input.placeholder = mode === "feedback"
        ? "输入需要调整的内容"
        : "输入完整 JSON 替换草稿";
    });
  });

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const value = input.value.trim();
    if (!value) return;
    const payload = { decision: "revise" };
    if (mode === "feedback") {
      payload.feedback = value;
    } else {
      try {
        payload.replacement = JSON.parse(value);
      } catch {
        $(".review-error", panel).textContent = "替换草稿必须是有效 JSON";
        return;
      }
    }
    disableReview(panel);
    resumeRun(payload);
  });
}

function disableReview(panel) {
  $$("button, textarea", panel).forEach((control) => {
    control.disabled = true;
  });
  panel.classList.add("review-submitted");
}

async function streamRun(body) {
  ensureThread();
  state.tracePanel = null;
  setBusy(true);
  showThinking();
  const response = await fetch(`/api/threads/${encodeURIComponent(state.threadId)}/runs/stream`, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrfToken(),
    },
    body: JSON.stringify({ ...body, stream_mode: ["values", "custom"] }),
  });

  if (response.status === 401) {
    showLogin();
    throw new Error("登录已失效");
  }
  if (!response.ok || !response.body) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.detail || `请求失败 (${response.status})`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const frames = buffer.split(/\r?\n\r?\n/);
    buffer = frames.pop() || "";
    frames.forEach(handleSseFrame);
    if (done) break;
  }
}

function handleSseFrame(frame) {
  if (!frame || frame.startsWith(":")) return;
  let event = "message";
  const dataLines = [];
  frame.split(/\r?\n/).forEach((line) => {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  });
  let data = dataLines.join("\n");
  try {
    data = JSON.parse(data);
  } catch {
    // Keep plain-text SSE payloads readable.
  }
  if (event === "events") consumeGraphEvent(data);
  if (event === "commitment_trace") appendCommitmentTrace(data);
  if (event === "interrupt") showReview(data);
  if (event === "error") {
    throw new Error(data?.error || "运行失败");
  }
}

async function submitTask(content) {
  const thread = currentThread();
  if (thread && thread.title === "新会话") {
    thread.title = content.replace(/\s+/g, " ").slice(0, 32);
    thread.updatedAt = Date.now();
    saveThreads();
    renderThreads();
  }
  addMessage("user", content);
  const files = state.uploads.map(({ filename, size }) => ({ filename, size }));
  state.uploads = [];
  renderAttachments();
  await streamRun({
    input: {
      messages: [{
        role: "user",
        content,
        additional_kwargs: files.length ? { files } : undefined,
      }],
    },
  });
}

async function resumeRun(payload) {
  state.pendingInterrupt = null;
  setBusy(true);
  try {
    await streamRun({ resume: payload });
  } catch (error) {
    handleError(error);
  } finally {
    if (!state.pendingInterrupt) {
      removeThinking();
      setBusy(false);
      setStatus("ready", "就绪");
      setProgress(9, true);
    }
  }
}

async function uploadFiles(files) {
  ensureThread();
  const form = new FormData();
  [...files].forEach((file) => form.append("files", file));
  const response = await fetch(`/api/threads/${encodeURIComponent(state.threadId)}/uploads`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrfToken() },
    body: form,
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.detail || "文件上传失败");
  }
  const result = await response.json();
  state.uploads.push(...(result.files || []));
  renderAttachments();
}

function renderAttachments() {
  const container = $("#attachments");
  container.replaceChildren();
  state.uploads.forEach((file, index) => {
    const item = document.createElement("span");
    item.className = "attachment";
    item.innerHTML = "<span></span><button type=\"button\" aria-label=\"移除\">×</button>";
    $("span", item).textContent = file.filename;
    $("button", item).addEventListener("click", () => {
      state.uploads.splice(index, 1);
      renderAttachments();
    });
    container.append(item);
  });
  container.hidden = state.uploads.length === 0;
}

function handleError(error) {
  removeThinking();
  setBusy(false);
  setStatus("error", "失败");
  addMessage("error", error.message || String(error));
}

function resizeComposer() {
  const input = $("#message-input");
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
}

function showLogin() {
  $("#login-view").hidden = false;
  $("#app-view").hidden = true;
}

function showApp(user) {
  state.user = user;
  $("#login-view").hidden = true;
  $("#app-view").hidden = false;
  const name = user.display_name || user.email;
  $("#user-name").textContent = name;
  $("#user-avatar").textContent = name.slice(0, 1).toUpperCase();
  ensureThread();
  renderThreads();
}

async function restoreSession() {
  const response = await fetch("/api/auth/me", { credentials: "same-origin" });
  if (!response.ok) {
    showLogin();
    return;
  }
  const result = await response.json();
  showApp(result.user);
}

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#login-error").textContent = "";
  const button = $("button[type=submit]", event.currentTarget);
  button.disabled = true;
  try {
    const response = await fetch("/api/auth/login", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email: $("#email").value,
        password: $("#password").value,
      }),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(result.detail || `登录失败 (${response.status})`);
    }
    showApp(result.user);
  } catch (error) {
    $("#login-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

$("#logout").addEventListener("click", async () => {
  await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" });
  showLogin();
});

$("#new-thread").addEventListener("click", () => {
  if (state.running) return;
  state.threadId = null;
  ensureThread();
  selectThread(state.threadId);
  $("#message-input").focus();
});

$("#attach-button").addEventListener("click", () => $("#file-input").click());
$("#file-input").addEventListener("change", async (event) => {
  try {
    await uploadFiles(event.target.files);
  } catch (error) {
    handleError(error);
  } finally {
    event.target.value = "";
  }
});

$("#message-input").addEventListener("input", resizeComposer);
$("#message-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("#composer").requestSubmit();
  }
});

$("#composer").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("#message-input");
  const content = input.value.trim();
  if (!content || state.running || state.pendingInterrupt) return;
  input.value = "";
  resizeComposer();
  try {
    await submitTask(content);
  } catch (error) {
    handleError(error);
  } finally {
    if (!state.pendingInterrupt) {
      removeThinking();
      setBusy(false);
      setStatus("ready", "就绪");
    }
  }
});

bindPrompts();
restoreSession().catch(handleError);
