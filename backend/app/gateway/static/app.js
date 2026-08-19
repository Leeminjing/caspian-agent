const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const STAGES = {
  1: "明确用户目标",
  2: "汇总要求与矛盾",
  3: "确认要求等级",
  4: "汇总必要输入",
  5: "确认技术版本",
  6: "确认理论知识",
  7: "确认并写入任务合同",
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

const KNOWLEDGE_STATUS = {
  retained: "保留",
  retained_partial: "部分压制",
  suppressed: "已压制",
  conflict_same_level: "同等级冲突",
  potential_conflict: "潜在分歧",
};

const state = {
  user: null,
  threads: JSON.parse(localStorage.getItem("caspian.threads") || "[]"),
  threadId: null,
  running: false,
  pendingInterrupt: null,
  currentRunId: null,
  interruptedByUser: false,
  streamSeq: 0,
  activeStreamId: 0,
  uploads: [],
  renderedMessageIds: new Set(),
  renderedToolIds: new Set(),
  commitmentMessageIds: new Set(),
  commitmentTraceItems: new Map(),
  tracePanel: null,
  traceStartedAt: 0,
  traceTimer: null,
  traceStreams: new Map(),
  traceScrollScheduled: false,
  activeSelectedSkills: [],
  compactionNoticeTimer: null,
  compactionArchived: [],
  compactionSummaryText: "",
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
  window.CaspianContextUi?.onThreadSelected();
}

function renderThreads() {
  const list = $("#thread-list");
  list.replaceChildren();
  const threads = window.CaspianContextUi
    ? window.CaspianContextUi.orderThreads(state.threads)
    : state.threads;
  threads.forEach((thread) => {
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
  finishTracePanel("已停止");
  state.threadId = id;
  state.pendingInterrupt = null;
  state.currentRunId = null;
  state.interruptedByUser = false;
  state.uploads = [];
  state.renderedMessageIds.clear();
  state.renderedToolIds.clear();
  state.commitmentMessageIds.clear();
  state.commitmentTraceItems.clear();
  state.tracePanel = null;
  state.activeSelectedSkills = [];
  subtaskEvents.clear();
  window.CaspianSkills?.clearSelection();
  setBusy(false);
  setStatus("ready", "就绪");
  $("#messages").replaceChildren(emptyState());
  renderAttachments();
  renderThreads();
  setProgress(0, false);
  loadThreadHistory();
  window.CaspianContextUi?.onThreadSelected();
}

async function loadThreadHistory() {
  const threadId = state.threadId;
  if (!threadId) return;
  try {
    const response = await fetch(`/api/threads/${encodeURIComponent(threadId)}/messages`, {
      credentials: "same-origin",
    });
    if (response.status === 401) {
      showLogin();
      return;
    }
    if (!response.ok) return;
    const data = await response.json();
    if (state.threadId !== threadId) return;
    state.compactionArchived = Array.isArray(data.archived) ? data.archived : [];
    renderHistoryMessages(
      Array.isArray(data.messages) ? data.messages : [],
      state.compactionArchived,
    );
  } catch (error) {
    console.warn("历史消息加载失败:", error);
  }
}

function renderHistoryMessages(messages, archived = []) {
  if (!messages.length) return;
  removeEmptyState();
  let showArchived = true;
  messages.forEach((message) => {
    if (message.additional_kwargs?.caspian_summary) {
      renderCompactionSummary(message, showArchived ? archived : [], showArchived);
      showArchived = false;
      return;
    }
    const type = message.type || message.role;
    if (type === "tool") {
      renderToolResultItem(message);
      return;
    }
    if (type !== "human" && type !== "ai" && type !== "assistant") return;
    const text = contentText(message.content);
    if (!text) return;
    if (type === "human") {
      addMessage("user", text, message.id);
      return;
    }
    addMessage("agent", text, message.id);
    (Array.isArray(message.tool_calls) ? message.tool_calls : []).forEach(
      (call) => renderToolCallItem(call, message.id),
    );
  });
}

function buildCompactionBody(text, archived = []) {
  let body = text;
  if (Array.isArray(archived) && archived.length) {
    const parts = archived.map((record) => {
      const role = record.type === "human" ? "用户" : record.type === "tool" ? "工具" : "AI";
      const toolLabel = record.type === "tool" && record.name ? `(${record.name})` : "";
      let part = `[${role}${toolLabel}] ${contentText(record.content)}`;
      if (Array.isArray(record.tool_calls) && record.tool_calls.length) {
        const calls = record.tool_calls.map((call) => {
          const args = JSON.stringify(call.args ?? {});
          return `  调用工具 ${call.name}(${args})`;
        });
        part += "\n" + calls.join("\n");
      }
      return part;
    });
    body += "\n\n--- 已压缩的原始消息 ---\n\n" + parts.join("\n\n");
  }
  return body;
}

function renderCompactionSummary(message, archived = [], showArchived = true) {
  const text = contentText(message.content);
  if (!text) return;
  const details = document.createElement("details");
  details.className = "compaction-summary";
  details.innerHTML = "<summary>历史已压缩</summary><pre></pre>";
  $("pre", details).textContent = buildCompactionBody(
    text,
    showArchived ? archived : [],
  );
  $("#messages").append(details);
  scrollMessages();
}

function upsertCompactionSummary(message) {
  const text = contentText(message.content);
  if (!text) return;
  state.compactionSummaryText = text;
  const topStrip = $(".compaction-summary", $("#messages"));
  if (topStrip) {
    $("pre", topStrip).textContent = buildCompactionBody(text, state.compactionArchived);
    scrollMessages();
    return;
  }
  const details = document.createElement("details");
  details.className = "compaction-summary";
  details.innerHTML = "<summary>历史已压缩</summary><pre></pre>";
  $("pre", details).textContent = buildCompactionBody(text, state.compactionArchived);
  $("#messages").prepend(details);
  scrollMessages();
}

async function refreshCompactionArchive() {
  const threadId = state.threadId;
  if (!threadId) return;
  try {
    const response = await fetch(`/api/threads/${encodeURIComponent(threadId)}/messages`, {
      credentials: "same-origin",
    });
    if (!response.ok) return;
    const data = await response.json();
    state.compactionArchived = Array.isArray(data.archived) ? data.archived : [];
    const topStrip = $(".compaction-summary", $("#messages"));
    if (topStrip && state.compactionSummaryText) {
      $("pre", topStrip).textContent = buildCompactionBody(
        state.compactionSummaryText,
        state.compactionArchived,
      );
    }
  } catch (error) {
    console.warn("压缩存档刷新失败:", error);
  }
}

function consumeCompactionStatus(value) {
  if (!value || typeof value !== "object") return false;
  if (value.type === "compaction_status" && typeof value.status === "string") {
    handleCompactionStatus(value.status);
    return true;
  }
  if (Array.isArray(value) && value.length === 2
      && value[0] === "custom" && value[1] && value[1].type === "compaction_status") {
    handleCompactionStatus(value[1].status);
    return true;
  }
  return Object.values(value).some(consumeCompactionStatus);
}

function handleCompactionStatus(status) {
  if (status === "started") {
    removeCompactionNotice();
    const notice = document.createElement("div");
    notice.id = "compaction-status";
    notice.className = "compaction-status";
    notice.textContent = "上下文正在压缩中…";
    const thinking = $("#thinking");
    thinking ? thinking.before(notice) : $("#messages").append(notice);
    scrollMessages();
    return;
  }
  if (status === "done") {
    // 压缩完成:提示保持为完成态数秒,保证用户看到完整生命周期(开始→完成)
    const notice = $("#compaction-status");
    if (notice) {
      notice.textContent = "历史已压缩";
      notice.classList.add("is-done");
      clearTimeout(state.compactionNoticeTimer);
      state.compactionNoticeTimer = setTimeout(removeCompactionNotice, 3000);
    }
    refreshCompactionArchive();
    return;
  }
  if (["failed", "skipped"].includes(status)) {
    removeCompactionNotice();
  }
}

function removeCompactionNotice() {
  clearTimeout(state.compactionNoticeTimer);
  state.compactionNoticeTimer = null;
  $("#compaction-status")?.remove();
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
      window.CaspianSkills?.clearSelection();
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
  // 打断按钮仅在 run 执行中且非承诺审查等待时可用
  $("#interrupt-button").hidden = !(value && !state.pendingInterrupt);
  if (value) setStatus("running", "处理中");
}

function removeEmptyState() {
  $("#empty-state")?.remove();
}

function renderMarkdown(text) {
  if (!window.marked || !window.DOMPurify) return null;
  return window.DOMPurify.sanitize(window.marked.parse(text, { gfm: true }));
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
  if (role === "agent") {
    const html = renderMarkdown(text);
    if (html !== null) {
      body.innerHTML = html;
    } else {
      body.textContent = text;
    }
  } else {
    body.textContent = text;
  }
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

function isNearBottom(element, threshold = 80) {
  return element.scrollHeight - element.scrollTop - element.clientHeight <= threshold;
}

function contentText(content) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .map((item) => typeof item === "string" ? item : item?.text || "")
    .filter(Boolean)
    .join("\n");
}

function toolCallsText(message) {
  const calls = Array.isArray(message.tool_calls) ? message.tool_calls : [];
  if (!calls.length) return "";
  return calls.map((call) => {
    let args = "";
    try {
      args = JSON.stringify(call.args ?? {});
    } catch {
      args = String(call.args ?? "");
    }
    if (args.length > 200) args = `${args.slice(0, 200)}…`;
    return `调用工具 \`${call.name}\`(${args})`;
  }).join("\n");
}

function renderToolCallItem(call, messageId) {
  if (!call || !call.name) return;
  const key = `toolcall:${messageId || ""}:${call.id || ""}:${call.name}`;
  if (state.renderedToolIds.has(key)) return;
  state.renderedToolIds.add(key);
  removeEmptyState();
  const details = document.createElement("details");
  details.className = "tool-item tool-call-item";
  details.innerHTML = "<summary><strong></strong></summary><pre></pre>";
  $("summary strong", details).textContent = `调用工具 ${call.name}`;
  let argsText = "";
  try {
    argsText = JSON.stringify(call.args ?? {}, null, 2);
  } catch {
    argsText = String(call.args ?? "");
  }
  $("pre", details).textContent = argsText;
  $("#messages").append(details);
  scrollMessages();
}

function renderToolResultItem(message) {
  const key = `toolresult:${message.id || ""}:${message.name || ""}`;
  if (state.renderedToolIds.has(key)) return;
  state.renderedToolIds.add(key);
  removeEmptyState();
  const brief = contentText(message.content).replace(/\s+/g, " ").slice(0, 80);
  const details = document.createElement("details");
  details.className = "tool-item tool-result-item";
  details.innerHTML = "<summary><strong></strong><span></span></summary><pre></pre>";
  $("summary strong", details).textContent = message.name ? `工具结果 ${message.name}` : "工具结果";
  $("summary span", details).textContent = brief ? ` — ${brief}` : "";
  $("pre", details).textContent = contentText(message.content);
  $("#messages").append(details);
  scrollMessages();
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

const subtaskEvents = new Map();

function consumeTaskEvent(value) {
  if (!value || typeof value !== "object") return false;
  if (typeof value.type === "string" && value.type.startsWith("task_")) {
    handleSubtaskEvent(value);
    return true;
  }
  return Object.values(value).some(consumeTaskEvent);
}

function consumeKnowledgeEvent(value) {
  if (!value || typeof value !== "object") return false;
  if (value.type === "knowledge_governance") {
    renderKnowledgePanel(value);
    return true;
  }
  return Object.values(value).some(consumeKnowledgeEvent);
}

function renderKnowledgePanel(event) {
  removeEmptyState();
  const fragment = $("#knowledge-template").content.cloneNode(true);
  const panel = $(".knowledge-panel", fragment);
  $(".knowledge-query", panel).textContent = String(event.query || "");
  const ledger = $(".knowledge-ledger", panel);
  (event.ledger || []).forEach((item) => {
    const row = document.createElement("li");
    row.className = "knowledge-row";
    const badge = document.createElement("span");
    badge.className = `level-badge level-${item.level_display}`;
    badge.textContent = item.level_display;
    const status = document.createElement("span");
    status.className = `knowledge-status status-${item.status}`;
    status.textContent = KNOWLEDGE_STATUS[item.status] || item.status;
    const text = document.createElement("span");
    text.className = "knowledge-row-text";
    text.textContent = item.reason || "参与最终回答";
    if (item.suppressed_claims?.length) {
      const claims = document.createElement("span");
      claims.className = "knowledge-claims";
      claims.textContent = `被压命题：${item.suppressed_claims.join("；")}`;
      text.append(document.createElement("br"), claims);
    }
    row.append(badge, status, text);
    ledger.append(row);
  });
  const notes = $(".knowledge-notes", panel);
  if (event.notes?.length) {
    notes.hidden = false;
    notes.textContent = event.notes.join(" ");
  }
  $("#messages").append(panel);
  scrollMessages();
}

function subtaskCard(taskId) {
  if (subtaskEvents.has(taskId)) return subtaskEvents.get(taskId);
  removeEmptyState();
  const card = document.createElement("article");
  card.className = "subtask-card";
  card.innerHTML = `
    <header>
      <strong class="subtask-title"></strong>
      <span class="subtask-status"></span>
    </header>
    <ol class="subtask-steps"></ol>
    <p class="subtask-result" hidden></p>`;
  $("#messages").append(card);
  subtaskEvents.set(taskId, card);
  scrollMessages();
  return card;
}

function subtaskStatusLabel(type) {
  const labels = {
    task_started: "运行中",
    task_running: "运行中",
    task_completed: "已完成",
    task_failed: "失败",
    task_cancelled: "已取消",
    task_timed_out: "超时",
  };
  return labels[type] || "运行中";
}

function handleSubtaskEvent(event) {
  const taskId = String(event.task_id || "");
  if (!taskId) return;
  const card = subtaskCard(taskId);
  const type = String(event.type);
  $(".subtask-status", card).textContent = subtaskStatusLabel(type);
  if (type === "task_started") {
    $(".subtask-title", card).textContent =
      String(event.description || taskId).slice(0, 64);
    $(".subtask-title", card).title = taskId;
    return;
  }
  if (type === "task_running") {
    const message = event.message || {};
    const step = document.createElement("li");
    const text = contentText(message.content) || message.tool_calls?.[0]?.name || "";
    step.textContent = String(text).slice(0, 300);
    $(".subtask-steps", card).append(step);
    scrollMessages();
    return;
  }
  const result = $(".subtask-result", card);
  result.hidden = false;
  if (type === "task_completed") {
    result.textContent = String(event.result || "").slice(0, 500);
  } else {
    result.textContent = String(event.error || "Task ended.").slice(0, 500);
  }
  card.classList.add("is-terminal");
}

function consumeGraphEvent(data) {
  if (consumeTaskEvent(data)) return;
  if (consumeKnowledgeEvent(data)) return;
  if (consumeCompactionStatus(data)) return;
  const batches = collectCommitmentMessages(data);
  if (batches.length) {
    batches.forEach(appendCommitmentMessages);
    return;
  }
  collectMessages(data).forEach((message) => {
    if (message.additional_kwargs?.caspian_summary) {
      upsertCompactionSummary(message);
      return;
    }
    const type = message.type || message.role;
    if (type === "tool") {
      renderToolResultItem(message);
      return;
    }
    if (type !== "ai" && type !== "assistant") return;
    const text = contentText(message.content);
    if (text) addMessage("agent", text, message.id);
    (Array.isArray(message.tool_calls) ? message.tool_calls : []).forEach(
      (call) => renderToolCallItem(call, message.id),
    );
  });
}

function collectCommitmentMessages(value, found = []) {
  if (!value || typeof value !== "object") return found;
  if (value.type === "commitment_messages" && Array.isArray(value.messages)) {
    found.push(value);
    return found;
  }
  Object.values(value).forEach((item) => collectCommitmentMessages(item, found));
  return found;
}

function appendCommitmentMessages(batch) {
  removeEmptyState();
  ensureTracePanel();
  const actor = batch.actor || "supervisor";
  const stage = Number(batch.stage || 0);
  const attempt = Number(batch.attempt || 0) || undefined;
  batch.messages.forEach((message) => {
    const type = String(message.type || message.role || "").toLowerCase();
    const text = contentText(message.content);
    const key = message.id || `${actor}:${stage}:${attempt || 0}:${type}:${text}`;
    const signature = JSON.stringify(message);
    if (type.includes("human")) {
      if (actor === "supervisor" || state.commitmentMessageIds.has(key)) return;
      state.commitmentMessageIds.add(key);
      appendCommitmentTrace({
        actor,
        stage,
        attempt,
        title: `${TRACE_ACTORS[actor] || actor} 收到输入`,
        status: "running",
        detail: "已接收当前任务、Supervisor 消息历史与验收条件。",
        payload: message,
      });
      return;
    }
    if (actor !== "supervisor" && (type.includes("chunk") || !message.id)) {
      appendTraceOutputDelta({
        actor,
        stage,
        attempt,
        title: `${TRACE_ACTORS[actor] || actor} 正在生成`,
        payload: {
          stream_id: `${actor}-${stage}-${attempt || 0}`,
          delta: text,
        },
      }, isNearBottom($("#messages")));
      return;
    }
    if (state.commitmentMessageIds.has(key)) {
      const existing = state.commitmentTraceItems.get(key);
      if (actor !== "supervisor" || !type.includes("tool")
          || !existing || existing.signature === signature) return;
      existing.item.remove();
      state.commitmentMessageIds.delete(key);
      state.commitmentTraceItems.delete(key);
    }
    state.commitmentMessageIds.add(key);
    let payload = message;
    let status = "running";
    let title = `${TRACE_ACTORS[actor] || actor} 消息`;
    if (type.includes("tool")) {
      try {
        payload = JSON.parse(text);
      } catch {
        payload = message;
      }
      status = ["approved", "revised"].includes(payload?.status)
        ? "completed"
        : "failed";
      title = "delegate_with_review 最终返回";
    } else if (actor === "supervisor") {
      title = "Supervisor 委派阶段任务";
    }
    const messageStage = Number(
      payload?.stage || message.tool_calls?.[0]?.args?.stage || stage
    );
    const item = appendCommitmentTrace({
      actor,
      stage: messageStage,
      attempt,
      title,
      status,
      detail: type.includes("tool") ? "" : text,
      payload,
    });
    if (actor === "supervisor" && type.includes("tool")) {
      state.commitmentTraceItems.set(key, { item, signature });
    }
  });
}

function appendCommitmentTrace(trace) {
  const followMessages = isNearBottom($("#messages"));
  removeEmptyState();
  ensureTracePanel();
  const item = document.createElement("li");
  item.className = `trace-item trace-${trace.actor} trace-${trace.status}`;
  item.dataset.actor = trace.actor;
  item.dataset.stage = String(trace.stage);
  if (trace.status === "running") item.classList.add("is-active");
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
    revealTraceText(detail, trace.detail);
    item.append(detail);
  }
  if (trace.payload?.reasoning_summary) {
    const reasoning = document.createElement("p");
    reasoning.className = "trace-reasoning";
    revealTraceText(reasoning, trace.payload.reasoning_summary);
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
  if (trace.status !== "running") completeTraceActivity(trace);
  updateTraceCount();
  setProgress(Number(trace.stage || 0), true);
  if (followMessages) scrollMessages();
  return item;
}

function ensureTracePanel() {
  if (state.tracePanel?.isConnected) return;
  const fragment = $("#trace-template").content.cloneNode(true);
  state.tracePanel = $(".trace-panel", fragment);
  state.traceStartedAt = Date.now();
  state.traceStreams = new Map();
  clearInterval(state.traceTimer);
  state.traceTimer = setInterval(updateTraceElapsed, 1000);
  const thinking = $("#thinking");
  thinking ? thinking.before(state.tracePanel) : $("#messages").append(state.tracePanel);
  updateTraceElapsed();
}

function updateTraceElapsed() {
  if (!state.tracePanel?.isConnected || !state.traceStartedAt) return;
  const seconds = Math.max(0, Math.floor((Date.now() - state.traceStartedAt) / 1000));
  $(".trace-elapsed", state.tracePanel).textContent = `已思考 ${seconds} 秒`;
}

function updateTraceCount() {
  if (!state.tracePanel?.isConnected) return;
  $(".trace-count", state.tracePanel).textContent =
    `${$$(".trace-item", state.tracePanel).length} 项`;
}

function appendTraceOutputDelta(trace, followMessages) {
  const streamId = trace.payload?.stream_id || `${trace.actor}-${trace.stage}`;
  const key = `${trace.actor}:${trace.stage}:${streamId}`;
  let item = state.traceStreams.get(key);
  if (!item?.isConnected) {
    item = document.createElement("li");
    item.className = `trace-item trace-${trace.actor} trace-stream-item is-active`;
    item.dataset.actor = trace.actor;
    item.dataset.stage = String(trace.stage);
    item.innerHTML = `
      <div class="trace-meta">
        <span class="trace-actor"></span>
        <span>第 ${trace.stage} 步 · 公开输出流</span>
      </div>
      <strong></strong>
      <pre class="trace-stream"><span></span><i aria-hidden="true"></i></pre>`;
    $(".trace-actor", item).textContent = TRACE_ACTORS[trace.actor] || trace.actor;
    $("strong", item).textContent = trace.title;
    $(".trace-list", state.tracePanel).append(item);
    state.traceStreams.set(key, item);
    updateTraceCount();
  }
  const stream = $(".trace-stream", item);
  const followStream = isNearBottom(stream, 24);
  $("span", stream).textContent += String(trace.payload?.delta || "");
  if (followStream) {
    requestAnimationFrame(() => {
      stream.scrollTop = stream.scrollHeight;
    });
  }
  scheduleTraceScroll(followMessages);
}

function completeTraceActivity(trace) {
  $$(`.trace-item.is-active[data-actor="${trace.actor}"][data-stage="${trace.stage}"]`,
    state.tracePanel).forEach((item) => item.classList.remove("is-active"));
  for (const [key, item] of state.traceStreams) {
    if (item.dataset.actor === trace.actor && item.dataset.stage === String(trace.stage)) {
      $("i", item)?.remove();
      state.traceStreams.delete(key);
    }
  }
}

function revealTraceText(element, text) {
  const value = String(text || "");
  if (!value || window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    element.textContent = value;
    return;
  }
  let index = 0;
  const size = Math.max(1, Math.ceil(value.length / 100));
  const step = () => {
    if (!element.isConnected || index >= value.length) return;
    index = Math.min(value.length, index + size);
    element.textContent = value.slice(0, index);
    requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function scheduleTraceScroll(shouldScroll) {
  if (!shouldScroll || state.traceScrollScheduled) return;
  state.traceScrollScheduled = true;
  requestAnimationFrame(() => {
    state.traceScrollScheduled = false;
    scrollMessages();
  });
}

function finishTracePanel(label = "已完成") {
  clearInterval(state.traceTimer);
  state.traceTimer = null;
  if (!state.tracePanel?.isConnected) return;
  updateTraceElapsed();
  state.tracePanel.classList.remove("is-live");
  state.tracePanel.classList.add("is-finished");
  $(".trace-state", state.tracePanel).textContent = label;
  $$(".trace-item.is-active", state.tracePanel).forEach(
    (item) => item.classList.remove("is-active")
  );
  $$(".trace-stream i", state.tracePanel).forEach((cursor) => cursor.remove());
}

function prettyDraft(value) {
  if (typeof value === "string") return value;
  return JSON.stringify(value ?? {}, null, 2);
}

function showReview(interrupt) {
  removeThinking();
  finishTracePanel("等待确认");
  state.pendingInterrupt = interrupt;
  if (state.tracePanel?.isConnected) state.tracePanel.open = true;
  const payload = interrupt.value || {};
  const stage = Number(payload.stage || 0);
  setBusy(false);
  setStatus("review", "等待确认");
  setProgress(stage, true);
  removeEmptyState();

  const fragment = $("#review-template").content.cloneNode(true);
  const panel = $(".review-panel", fragment);
  panel.dataset.stage = String(stage);
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
  const contract = payload.draft?.contract_markdown;
  if (stage === 7 && typeof contract === "string") {
    $(".review-draft", panel).hidden = true;
    const editor = $(".review-contract-editor", panel);
    editor.hidden = false;
    editor.value = contract;
    editor.dataset.original = contract.trim();
    $(".approve-button", panel).textContent = "确认并写入";
    $(".revise-toggle", panel).textContent = "反馈重写";
  }
  bindReview(panel);
  $("#messages").append(panel);
  scrollMessages();
}

function bindReview(panel) {
  const form = $(".revision-form", panel);
  const input = $(".revision-input", panel);
  const contractEditor = $(".review-contract-editor", panel);
  const approveButton = $(".approve-button", panel);
  const stage = Number(panel.dataset.stage || 0);
  let mode = "feedback";

  const updateContractAction = () => {
    if (stage !== 7) return;
    approveButton.textContent =
      contractEditor.value.trim() === contractEditor.dataset.original
        ? "确认并写入"
        : "提交编辑并审核";
  };

  contractEditor.addEventListener("input", updateContractAction);
  approveButton.addEventListener("click", () => {
    if (stage === 7) {
      const contract = contractEditor.value.trim();
      if (!contract) {
        $(".review-error", panel).textContent = "任务合同不能为空";
        return;
      }
      if (contract !== contractEditor.dataset.original) {
        disableReview(panel);
        resumeRun({
          decision: "revise",
          replacement: { contract_markdown: contract },
        });
        return;
      }
    }
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
  removeInterruptPanel();
  state.interruptedByUser = false;
  state.currentRunId = null;
  const streamId = ++state.streamSeq;
  state.activeStreamId = streamId;
  finishTracePanel();
  if (state.tracePanel?.isConnected) state.tracePanel.open = false;
  state.tracePanel = null;
  state.traceStreams = new Map();
  setBusy(true);
  showThinking();
  const response = await fetch(`/api/threads/${encodeURIComponent(state.threadId)}/runs/stream`, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrfToken(),
    },
    body: JSON.stringify({ ...body, stream_mode: ["values"] }),
  });

  if (response.status === 401) {
    showLogin();
    throw new Error("登录已失效");
  }
  if (!response.ok || !response.body) {
    const detail = await response.json().catch(() => ({}));
    if (detail?.code === "context_projection_blocked") {
      window.CaspianContextUi?.openDecisionFromBlock(detail);
    }
    throw new Error(detail.message || detail.detail || `请求失败 (${response.status})`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const frames = buffer.split(/\r?\n\r?\n/);
    buffer = frames.pop() || "";
    frames.forEach((frame) => handleSseFrame(frame, streamId));
    if (done) break;
  }
}

function handleSseFrame(frame, streamId = state.activeStreamId) {
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
  if (event === "metadata" && data?.run_id) state.currentRunId = data.run_id;
  if (event === "events") consumeGraphEvent(data);
  if (event === "interrupt") showReview(data);
  // 仅当前流的结束帧操作全局面板；被打断的旧流结束帧不覆盖新状态
  if (event === "end" && streamId === state.activeStreamId) {
    if (!state.pendingInterrupt && !state.interruptedByUser) finishTracePanel("已完成");
    window.CaspianContextUi?.onRunEnded();
  }
  if (event === "error") {
    finishTracePanel("执行失败");
    window.CaspianContextUi?.onRunEnded();
    throw new Error(data?.error || "运行失败");
  }
}

async function submitTask(content, selectedSkills = []) {
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
    selected_skills: selectedSkills,
  });
}

async function resumeRun(payload) {
  state.pendingInterrupt = null;
  setBusy(true);
  try {
    await streamRun({ resume: payload, selected_skills: state.activeSelectedSkills });
  } catch (error) {
    handleError(error);
  } finally {
    if (!state.pendingInterrupt) {
      removeThinking();
      setBusy(false);
      setStatus("ready", "就绪");
      setProgress(9, true);
      window.CaspianSkills?.clearSelection();
      state.activeSelectedSkills = [];
    }
  }
}

async function interruptRun() {
  if (!state.currentRunId) return;
  const url = `/api/threads/${encodeURIComponent(state.threadId)}/runs/${encodeURIComponent(state.currentRunId)}/interrupt`;
  let response;
  try {
    response = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrfToken() },
    });
  } catch (error) {
    handleError(error);
    return;
  }
  if (response.status === 409) {
    handleError(new Error("run 已结束，无法打断"));
    return;
  }
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    handleError(new Error(detail.detail || `打断失败 (${response.status})`));
    return;
  }
  state.interruptedByUser = true;
  setBusy(false);
  showInterruptPanel();
}

function showInterruptPanel() {
  removeThinking();
  finishTracePanel("已暂停");
  setStatus("interrupt", "已暂停");
  const fragment = $("#interrupt-template").content.cloneNode(true);
  const panel = $(".interrupt-panel", fragment);
  $(".continue-button", panel).addEventListener("click", continueRun);
  $(".abandon-button", panel).addEventListener("click", abandonRun);
  $("#messages").append(panel);
  scrollMessages();
}

function removeInterruptPanel() {
  $(".interrupt-panel")?.remove();
}

function continueRun() {
  streamRun({ input: {} });
}

function abandonRun() {
  removeInterruptPanel();
  state.interruptedByUser = false;
  state.currentRunId = null;
  setBusy(false);
  setStatus("ready", "就绪");
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
  finishTracePanel("执行失败");
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
  window.CaspianSkills?.clearCache();
  window.CaspianSkills?.clearSelection();
  state.activeSelectedSkills = [];
  $("#login-view").hidden = false;
  $("#app-view").hidden = true;
}

function showApp(user) {
  if (state.user?.id !== user.id) window.CaspianSkills?.clearCache();
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

$("#interrupt-button").addEventListener("click", () => {
  interruptRun();
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
  if (event.defaultPrevented) return;
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("#composer").requestSubmit();
  }
});

$("#composer").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("#message-input");
  const selectedSkills = window.CaspianSkills?.selectedNames(input.value) || [];
  const content = window.CaspianSkills?.messageText(input.value) || input.value.trim();
  if (!content || state.running || state.pendingInterrupt) return;
  state.activeSelectedSkills = selectedSkills;
  input.value = "";
  resizeComposer();
  try {
    await submitTask(content, selectedSkills);
  } catch (error) {
    handleError(error);
  } finally {
    if (!state.pendingInterrupt) {
      removeThinking();
      setBusy(false);
      setStatus("ready", "就绪");
      window.CaspianSkills?.clearSelection();
      state.activeSelectedSkills = [];
    }
  }
});

bindPrompts();
window.CaspianSkills?.attach({
  input: $("#message-input"),
  host: $("#skill-picker"),
  chips: $("#selected-skills"),
});

window.CaspianContextUi?.init({
  getThreadId: () => state.threadId,
  getThreads: () => state.threads,
  getCurrentThread: currentThread,
  addThread: (thread) => {
    state.threads.unshift(thread);
    saveThreads();
  },
  selectThread,
});

// --- 决策等级表查看 ---
const _LEVEL_LABELS = { 3: "3 必须", 2: "2 可协商", 1: "1 可选" };

function renderDecisionTable(data) {
  const version = $("#decision-table-version");
  const body = $("#decision-table-body");
  if (!data.exists || !data.rows.length) {
    version.textContent = "";
    body.innerHTML = '<p class="decision-table-empty">暂无等级表</p>';
    return;
  }
  version.textContent = `版本 ${data.version}`;
  const table = document.createElement("table");
  table.className = "decision-table";
  table.innerHTML = "<thead><tr><th>要求</th><th>决策</th><th>等级</th></tr></thead>";
  const tbody = document.createElement("tbody");
  for (const row of data.rows) {
    const tr = document.createElement("tr");
    const cells = [row.requirement, row.decision, _LEVEL_LABELS[row.priority] ?? String(row.priority)];
    for (const [i, text] of cells.entries()) {
      const td = document.createElement("td");
      td.textContent = text;
      if (i === 1) td.className = row.decision === "丢弃" ? "decision-dropped" : "decision-kept";
      if (i === 2) td.className = "decision-level";
      tr.append(td);
    }
    tbody.append(tr);
  }
  table.append(tbody);
  body.replaceChildren(table);
}

async function loadDecisionTable() {
  const version = $("#decision-table-version");
  const body = $("#decision-table-body");
  if (!state.threadId) {
    version.textContent = "";
    body.innerHTML = '<p class="decision-table-empty">请先发送一条消息创建会话</p>';
    return;
  }
  body.innerHTML = '<p class="decision-table-empty">加载中…</p>';
  try {
    const response = await fetch(
      `/api/threads/${encodeURIComponent(state.threadId)}/decision-table`,
      { credentials: "same-origin" }
    );
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    renderDecisionTable(await response.json());
  } catch (error) {
    version.textContent = "";
    body.innerHTML = '<p class="decision-table-empty">加载失败</p>';
  }
}

const decisionTablePanel = $("#decision-table-panel");
$("#decision-table-toggle")?.addEventListener("click", () => {
  const willOpen = decisionTablePanel.hidden;
  decisionTablePanel.hidden = !willOpen;
  $("#decision-table-toggle").setAttribute("aria-expanded", String(willOpen));
  if (willOpen) loadDecisionTable();
});
$("#decision-table-close")?.addEventListener("click", () => {
  decisionTablePanel.hidden = true;
  $("#decision-table-toggle").setAttribute("aria-expanded", "false");
});

restoreSession().catch(handleError);
