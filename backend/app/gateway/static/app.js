/*
本文件对外提供网关前端主控制器：会话列表、聊天流式渲染、承诺层评审卡、决策等级表、
目标徽章与登录态管理，是 index.html 的主脚本。

输入为用户交互事件与 /api/threads/{id}/runs/stream 的 SSE 事件；输出为 DOM 渲染与对
后端 REST 接口的调用。具体工作流为：登录后 loadThreads() 以 GET /api/contexts/tree
为事实源装载会话全集，经 CaspianThreadList 合并本地未入库会话并按最近活跃倒序渲染；
提交任务后逐帧消费 SSE，按事件类型分派到正文、推理、工具卡与中断评审面板。

会话列表的事实源是服务端；localStorage["caspian.threads"] 仅作离线降级缓存（保存前
先排序再截断最近 20 条），localStorage["caspian.current_thread"] 记住当前选中会话。

示例：submitTask("帮我分析这份日志") 会创建 run 并把流式结果渲染到消息区。
*/
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
  threadId: localStorage.getItem("caspian.current_thread") || null,
  running: false,
  followMessages: true,
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
  models: [],
  modelName: null,
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
  // ponytail: 仅作离线降级缓存，先按最近活跃排序再截断，避免缓存到最旧的 20 条
  const recent = window.CaspianThreadList
    ? window.CaspianThreadList.sortThreads(state.threads).slice(0, 20)
    : state.threads.slice(0, 20);
  localStorage.setItem("caspian.threads", JSON.stringify(recent));
}

async function loadThreads() {
  try {
    const tree = await apiFetch("/api/contexts/tree");
    // 不变量：state.threads 始终按展示序排列（未入库置顶 + 最近活跃倒序），
    // 使 ensureThread / removeLocalThread 等按 [0] 取「列表首位」的调用方无需各自排序
    state.threads = window.CaspianThreadList.sortThreads(
      window.CaspianThreadList.mergeThreads(tree, state.threads),
    );
    saveThreads();
    renderThreads();
  } catch {
    // 服务端不可达：保留既有本地缓存列表，不清空，当前会话仍可用
  }
}

function saveCurrentThread() {
  if (state.threadId) localStorage.setItem("caspian.current_thread", state.threadId);
  else localStorage.removeItem("caspian.current_thread");
}

function currentThread() {
  return state.threads.find((item) => item.id === state.threadId);
}

function createThread() {
  const thread = { id: threadId(), title: "新会话", updatedAt: Date.now(), pending: true };
  state.threads.unshift(thread);
  state.threadId = thread.id;
  saveThreads();
  saveCurrentThread();
  renderThreads();
  window.CaspianContextUi?.onThreadSelected();
  return thread;
}

function ensureThread() {
  if (state.threadId) return;
  // 恢复记住的当前会话；记住的 id 失效时回退列表首位（最近会话）；列表为空才新建
  const remembered = state.threads.find((item) => item.id === state.threadId) || state.threads[0];
  if (remembered) {
    state.threadId = remembered.id;
    saveCurrentThread();
    return;
  }
  createThread();
}

function renderThreads() {
  const list = $("#thread-list");
  list.replaceChildren();
  const threads = window.CaspianThreadList
    ? window.CaspianThreadList.sortThreads(state.threads)
    : state.threads;
  threads.forEach((thread) => {
    const row = document.createElement("div");
    row.className = `thread-row${thread.id === state.threadId ? " active" : ""}`;
    row.dataset.threadId = thread.id;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "thread-item";
    button.innerHTML = `<strong></strong><span></span>`;
    $("strong", button).textContent = thread.title;
    $("span", button).textContent = thread.id.slice(0, 13);
    button.addEventListener("click", () => selectThread(thread.id));
    const rename = document.createElement("button");
    rename.type = "button";
    rename.className = "thread-rename";
    rename.textContent = "✎";
    rename.title = "重命名";
    rename.setAttribute("aria-label", "重命名");
    rename.addEventListener("click", (event) => {
      event.stopPropagation();
      const strong = $("strong", button);
      startInlineRename(strong, {
        getValue: () => thread.title,
        onCommit: (value) => renameThread(thread.id, value),
      });
    });
    const archive = document.createElement("button");
    archive.type = "button";
    archive.className = "thread-archive";
    archive.textContent = "⊘";
    archive.title = "归档";
    archive.setAttribute("aria-label", "归档");
    archive.addEventListener("click", (event) => {
      event.stopPropagation();
      archiveThread(thread.id);
    });
    const del = document.createElement("button");
    del.type = "button";
    del.className = "thread-delete";
    del.textContent = "✕";
    del.title = "删除";
    del.setAttribute("aria-label", "删除");
    del.addEventListener("click", (event) => {
      event.stopPropagation();
      deleteThread(thread.id);
    });
    row.append(button, rename, archive, del);
    list.append(row);
  });
  const thread = currentThread();
  $("#thread-title").textContent = thread?.title || "新会话";
  $("#thread-id").textContent = state.threadId || "";
}

function startInlineRename(textEl, { getValue, onCommit }) {
  const container = textEl.parentNode;
  const original = textEl.textContent;
  const input = document.createElement("input");
  input.type = "text";
  input.maxLength = 200;
  input.className = "inline-rename-input";
  input.value = getValue ? String(getValue() ?? "") : original;
  input.setAttribute("aria-label", "新名称");
  textEl.replaceWith(input);
  input.focus();
  input.select();

  let restored = false;
  const restore = () => {
    if (restored) return;
    restored = true;
    input.replaceWith(textEl);
  };
  const submit = () => {
    if (restored) return;
    const value = input.value.trim();
    if (!value) {
      input.focus();
      return;
    }
    input.disabled = true;
    onCommit(value)
      .then(() => { restored = true; })
      .catch((error) => {
        alert(`重命名失败：${error.message}`);
        input.disabled = false;
        restore();
      });
  };

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      event.stopPropagation();
      submit();
    } else if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      restore();
    }
  });
  input.addEventListener("blur", submit);
}

function renameThread(id, title) {
  const url = `/api/contexts/${encodeURIComponent(id)}`;
  return fetch(url, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
    body: JSON.stringify({ title }),
    credentials: "same-origin",
  })
    .then(async (response) => {
      if (!response.ok) {
        let detail;
        try { detail = (await response.json()).detail; } catch { detail = await response.text(); }
        throw new Error(typeof detail === "string" ? detail : (detail?.message || JSON.stringify(detail)));
      }
      return response.json();
    })
    .then((payload) => {
      const target = state.threads.find((item) => item.id === id);
      if (target) {
        target.title = payload.title || title;
        target.updatedAt = Date.now();
        saveThreads();
        renderThreads();
      }
      window.CaspianContextUi?.onRunEnded();
      return payload;
    });
}

async function apiFetch(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrfToken(),
      ...(options.headers || {}),
    },
  });
  if (!response.ok) {
    let detail;
    try { detail = (await response.json()).detail; } catch { detail = await response.text(); }
    throw new Error(typeof detail === "string" ? detail : (detail?.message || JSON.stringify(detail)));
  }
  return response.json();
}

function removeLocalThread(id) {
  const index = state.threads.findIndex((item) => item.id === id);
  if (index === -1) return;
  state.threads.splice(index, 1);
  saveThreads();
  if (state.threadId === id) {
    const next = state.threads[0];
    if (next) selectThread(next.id);
    else {
      state.threadId = null;
      saveCurrentThread();
      renderThreads();
    }
  } else {
    renderThreads();
  }
}

async function archiveThread(id) {
  try {
    await apiFetch(`/api/threads/${encodeURIComponent(id)}/archive`, { method: "POST" });
    removeLocalThread(id);
    window.CaspianContextUi?.onRunEnded();
  } catch (error) {
    handleError(error);
  }
}

function deleteThread(id) {
  const title = state.threads.find((item) => item.id === id)?.title || id;
  openConfirm(`「${title}」将被永久删除且不可恢复（连同其所有派生子会话）。确定删除吗？`, async () => {
    await apiFetch(`/api/threads/${encodeURIComponent(id)}`, { method: "DELETE" });
    removeLocalThread(id);
    window.CaspianContextUi?.onRunEnded();
  });
}

async function restoreThread(id) {
  try {
    await apiFetch(`/api/threads/${encodeURIComponent(id)}/restore`, { method: "POST" });
    const items = await loadArchived();
    const archived = items.find((item) => item.thread_id === id);
    if (archived && !state.threads.some((item) => item.id === id)) {
      state.threads.unshift({ id, title: archived.title || "新会话", updatedAt: Date.now() });
      saveThreads();
      renderThreads();
    }
    document.dispatchEvent(new CustomEvent("ui:archived-changed", { detail: { items } }));
    window.CaspianContextUi?.onRunEnded();
  } catch (error) {
    handleError(error);
  }
}

async function loadArchived() {
  try {
    const items = await apiFetch("/api/threads/archived");
    return Array.isArray(items) ? items : [];
  } catch (error) {
    return [];
  }
}

function renderArchivedList(items, container) {
  if (!container) return;
  if (!items.length) {
    container.innerHTML = '<p class="archived-empty">暂无已归档的会话</p>';
    return;
  }
  container.replaceChildren();
  items.forEach((item) => {
    const row = document.createElement("div");
    row.className = "archived-row";
    const name = document.createElement("span");
    name.className = "archived-name";
    name.textContent = item.title || item.thread_id;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "button button-quiet archived-restore";
    btn.textContent = "恢复";
    btn.addEventListener("click", () => restoreThread(item.thread_id));
    row.append(name, btn);
    container.append(row);
  });
}

let confirmCallback = null;
function openConfirm(text, onOk) {
  $("#confirm-text").textContent = text;
  confirmCallback = onOk;
  $("#confirm-panel").hidden = false;
}
function closeConfirm() {
  $("#confirm-panel").hidden = true;
  confirmCallback = null;
}

$("#confirm-cancel")?.addEventListener("click", closeConfirm);
$("#confirm-ok")?.addEventListener("click", async () => {
  const ok = confirmCallback;
  closeConfirm();
  if (ok) {
    try { await ok(); } catch (error) { handleError(error); }
  }
});

function selectThread(id) {
  if (state.running) return;
  finishTracePanel("已停止");
  state.threadId = id;
  state.followMessages = true;
  saveCurrentThread();
  state.pendingInterrupt = null;
  state.currentRunId = null;
  state.interruptedByUser = false;
  state.uploads = [];
  state.renderedMessageIds.clear();
  state.renderedToolIds.clear();
  state.commitmentMessageIds.clear();
  state.commitmentTraceItems.clear();
  // msgAcc 缓存了指向旧 DOM 节点的渲染句柄，切换会话清空消息区后必须一并清掉，
  // 否则回到旧会话时 renderAgentMessage 把内容写进已脱离文档的节点（历史消息丢失）
  state.msgAcc = Object.create(null);
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
  refreshGoalBadge(state.threadId);
  loadThreadHistory();
  window.CaspianContextUi?.onThreadSelected();
}

async function refreshGoalBadge(threadId) {
  if (!threadId) { renderGoalBadge(null); return; }
  try {
    const res = await fetch(`/api/threads/${encodeURIComponent(threadId)}/goal`, { credentials: "same-origin" });
    if (!res.ok) { renderGoalBadge(null); return; }
    const data = await res.json();
    renderGoalBadge(data?.goal);
  } catch {
    renderGoalBadge(null);
  }
}

let goalPollTimer = null;
async function startGoalPoll(threadId) {
  stopGoalPoll();
  if (!threadId) return;
  refreshGoalBadge(threadId);
  goalPollTimer = setInterval(() => { refreshGoalBadge(threadId); }, 1500);
}
function stopGoalPoll() {
  if (goalPollTimer !== null) { clearInterval(goalPollTimer); goalPollTimer = null; }
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
    // 决策等级表编辑事务消息（编辑意图 / 编辑结果）不作为对话渲染
    if (
      message.additional_kwargs?.decision_table_edit ||
      message.additional_kwargs?.decision_table_edit_ack
    ) {
      return;
    }
    const type = message.type || message.role;
    if (type === "tool") {
      renderToolResultItem(message);
      return;
    }
    if (type !== "human" && type !== "ai" && type !== "assistant") return;
    if (type === "human") {
      const text = contentText(message.content);
      if (text) addMessage("user", text, message.id);
      return;
    }
    renderAgentMessage(message);
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
  document.dispatchEvent(new CustomEvent("ui:status", {
    detail: { kind, message: label },
  }));
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

// 流式 Markdown 重渲染的最小间隔：同一窗口内多个正文分片只合并为一次渲染，
// 避免长文逐 token 全量 marked.parse 造成的 O(n²) 卡顿与 DOM 频繁重建。
const STREAM_MD_THROTTLE_MS = 60;

// 权威渲染入口：把 h.contentAcc（正文 Markdown 源码）经 renderMarkdown 渲染成 HTML 写入
// h.contentEl（缺失时创建 .message-content）。供流式（consumeTokenChunk）与整帧
// （renderAgentMessage）共用，保证两路径结果一致。
function renderContentNow(h) {
  if (h.contentEl === null) {
    const el = document.createElement("div");
    el.className = "message-content";
    h.body.append(el);
    h.contentEl = el;
  }
  const text = h.contentAcc || "";
  const html = renderMarkdown(text);
  h.contentEl.innerHTML = html !== null ? html : text.replace(/</g, "&lt;");
}

// 节流调度：脏标记 + setTimeout，把节流窗口内的多个正文分片合并为一次渲染；
// 定时器触发时读取最新 h.contentAcc，故最终值与整帧定型一致（幂等）。
function scheduleContentRender(h) {
  if (h.renderPending) return;
  h.renderPending = true;
  setTimeout(() => {
    h.renderPending = false;
    renderContentNow(h);
  }, STREAM_MD_THROTTLE_MS);
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
  if (id) message.dataset.messageId = id;
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

// 单一渲染入口：按消息 id 定位 agent 气泡，缺失则新建，正文/推理/工具调用都挂到该气泡。
// 使用 ensureAgentHandle 提供的统一交互对象（article + body + 各部件），
// 确保 messages 流式（consumeTokenChunk）与 values 整帧（renderAgentMessage）操作同一批元素，
// 根除"流式期间无 think-line、结束时一次性出现"的双路径冲突。
function renderAgentMessage(message) {
  const id = message?.id || "";
  if (!id) return;
  const h = ensureAgentHandle(id);
  const text = contentText(message.content);
  const reasoning = reasoningText(message);
  const toolCalls = Array.isArray(message.tool_calls) ? message.tool_calls : [];

  if (text) {
    h.contentAcc = text;
    renderContentNow(h);
  }

  if (reasoning) {
    const line = ensureThinkLine(h);
    line.pre.textContent = reasoning;
    line.excerpt.textContent = tailSummary(reasoning, 120);
  }
  toolCalls.forEach((call) => renderToolCallItem(call, id, h.body));
}

function ensureAgentHandle(id) {
  if (!state.msgAcc) state.msgAcc = Object.create(null);
  if (state.msgAcc[id]) return state.msgAcc[id];
  const article = agentArticle(id);
  const body = article.querySelector(".message-body") || agentBody(article);
  state.msgAcc[id] = {
    article,
    body,
    contentEl: null,
    thinkLine: null,
    contentAcc: "",
    renderPending: false,
  };
  return state.msgAcc[id];
}

function ensureThinkLine(h) {
  if (h.thinkLine) return h.thinkLine;
  let line = h.body.querySelector(".think-line");
  if (!line) {
    line = document.createElement("details");
    line.className = "think-line";
    // 默认收起（不自动弹出）；推理仍逐 token 追加到 <pre>，用户点开可见完整推理。
    line.innerHTML = "<summary><span class=\"think-badge\">Think</span><span class=\"think-excerpt\"></span></summary><pre></pre>";
    h.body.insertBefore(line, h.body.firstChild);
  }
  h.thinkLine = {
    el: line,
    pre: line.querySelector("pre"),
    excerpt: line.querySelector(".think-excerpt"),
  };
  return h.thinkLine;
}

function agentArticle(id) {
  if (id) {
    const el = document.querySelector(`#messages .message.message-agent[data-message-id="${CSS.escape(id)}"]`);
    if (el) return el;
  }
  const article = document.createElement("article");
  article.className = "message message-agent";
  if (id) article.dataset.messageId = id;
  $("#messages").append(article);
  scrollMessages();
  return article;
}

function agentBody(article) {
  const body = document.createElement("div");
  body.className = "message-body";
  article.append(body);
  return body;
}

// messages 模式逐 token chunk 的就地增量累计：按消息 id 操作统一交互对象，
// 把 reasoning/content 分片追加到 think-line 与正文，与 values 整帧渲染共享同一批元素。
function consumeTokenChunk(data) {
  const message = data?.message;
  if (!message || typeof message !== "object") return;
  const id = message.id || "";
  if (!id) return;
  // 流式 AIMessageChunk 的 type 可能是 "ai"/"assistant"/"AIMessageChunk"（大小写/形式不一），
  // 这里只保留含 ai/assistant 语义的类型，避免把推理流式消息误跳过。
  const tokenType = String(message.type || message.role || "");
  if (!/ai|assistant/i.test(tokenType)) return;

  const reasoningDelta = reasoningText(message);
  const contentDelta = contentText(message.content);
  if (!reasoningDelta && !contentDelta) return;

  removeEmptyState();
  const h = ensureAgentHandle(id);

  if (contentDelta) {
    h.contentAcc = (h.contentAcc || "") + contentDelta;
    scheduleContentRender(h);
  }

  if (reasoningDelta) {
    const line = ensureThinkLine(h);
    const next = (line.pre.textContent || "") + reasoningDelta;
    line.pre.textContent = next;
    line.excerpt.textContent = tailSummary(next, 120);
  }
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

function scrollMessages(force = false) {
  if (!force && !state.followMessages) return;
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

function reasoningText(message) {
  const raw = message?.additional_kwargs?.reasoning_content;
  if (typeof raw === "string") return raw;
  if (!Array.isArray(raw)) return "";
  return raw
    .map((item) => typeof item === "string" ? item : item?.text || "")
    .filter(Boolean)
    .join("\n");
}

// 折叠态摘要：显示推理文本的**最新末尾窗口**（tail），流式时随 token 滑动，
// 让用户看到"最新的 token 在实时出现"，而非固定开头。
function tailSummary(text, max) {
  const flat = String(text || "").replace(/\s+/g, " ").trim();
  if (!flat) return "";
  if (flat.length <= max) return flat;
  return "…" + flat.slice(flat.length - max);
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

function renderToolCallItem(call, messageId, article) {
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
  (article || $("#messages")).append(details);
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
  if (window.CaspianPlugins && window.CaspianPlugins.consume(data)) return;
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
    renderAgentMessage(message);
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

function showDecisionTableAdjudication(interrupt) {
  removeThinking();
  finishTracePanel("等待确认");
  state.pendingInterrupt = interrupt;
  if (state.tracePanel?.isConnected) state.tracePanel.open = true;
  const payload = interrupt.value || {};
  setBusy(false);
  setStatus("review", "等待确认");
  removeEmptyState();

  const fragment = $("#review-template").content.cloneNode(true);
  const panel = $(".review-panel", fragment);
  panel.dataset.stage = "0";
  $(".review-kicker", panel).textContent = "决策等级表";
  $("h3", panel).textContent = "存在冲突，需确认";
  const rows = [].concat(
    (payload.candidate || []).map((r) => `候选：${r.requirement}（${r.decision}，等级 ${r.priority}）`),
    (payload.existing || []).map((r) => `现有：${r.requirement}（${r.decision}，等级 ${r.priority}）`)
  );
  $(".review-draft", panel).textContent = [].concat(rows, payload.conflicts || []).join("\n");
  $(".review-error", panel).textContent = "";
  $(".approve-button", panel).textContent = "采纳新表";
  $(".approve-button", panel).dataset.decision = "adopt";
  $(".revise-toggle", panel).textContent = "保留旧表";
  $(".revise-toggle", panel).dataset.decision = "keep";
  $(".segmented", panel)?.remove();
  $(".revision-form", panel)?.remove();

  $(".approve-button", panel).addEventListener("click", () => {
    disableReview(panel);
    resumeRun({ decision: "adopt" });
  });
  $(".revise-toggle", panel).addEventListener("click", () => {
    disableReview(panel);
    resumeRun({ decision: "keep" });
  });

  $("#messages").append(panel);
  scrollMessages();
}

function showPlanReview(interrupt) {
  removeThinking();
  finishTracePanel("等待确认");
  state.pendingInterrupt = interrupt;
  if (state.tracePanel?.isConnected) state.tracePanel.open = true;
  const payload = interrupt.value || {};
  setBusy(false);
  setStatus("review", "等待确认");
  removeEmptyState();

  const fragment = $("#plan-review-template").content.cloneNode(true);
  const panel = $(".plan-review-panel", fragment);
  const body = $(".plan-review-body", panel);
  const plan = payload.plan || "";
  const html = renderMarkdown(plan);
  if (html) body.innerHTML = html;
  else body.textContent = plan;
  bindPlanReview(panel);
  $("#messages").append(panel);
  scrollMessages();
}

function bindPlanReview(panel) {
  const form = $(".revision-form", panel);
  const input = $(".plan-feedback-input", panel);
  const approveButton = $(".plan-approve-button", panel);
  const keepToggle = $(".plan-keep-toggle", panel);
  const discussButton = $(".plan-discuss-button", panel);

  approveButton.addEventListener("click", () => {
    disableReview(panel);
    resumeRun({ decision: "approve" });
  });
  keepToggle.addEventListener("click", () => {
    $(".review-actions", panel).hidden = true;
    form.hidden = false;
    input.focus();
  });
  discussButton.addEventListener("click", () => {
    disableReview(panel);
    resumeRun({ decision: "dismiss" });
  });
  $(".plan-cancel-keep", panel).addEventListener("click", () => {
    form.hidden = true;
    $(".review-actions", panel).hidden = false;
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const value = input.value.trim();
    disableReview(panel);
    resumeRun({ decision: "keep", feedback: value });
  });
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
  startGoalPoll(state.threadId);
  const response = await fetch(`/api/threads/${encodeURIComponent(state.threadId)}/runs/stream`, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrfToken(),
    },
    body: JSON.stringify({ ...body, stream_mode: ["messages", "values"] }),
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
  let frameCount = 0;
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const frames = buffer.split(/\r?\n\r?\n/);
    buffer = frames.pop() || "";
    for (const frame of frames) {
      handleSseFrame(frame, streamId);
      // 每处理一批 SSE 帧，主动让出主线程一次，让浏览器有机会在推理流式期间重绘
      // （逐 token 增长的 think-line 正文）。若无此让出，大批 stream 帧同一 tick 处理完，
      // 浏览器只在最后重绘一次 -> 表现为"处理中几秒后突然出现完整推理"。
      if ((++frameCount & 31) === 0) {
        await new Promise((resolve) => setTimeout(resolve, 0));
      }
    }
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
  if (event === "stream") consumeTokenChunk(data);
  if (event === "interrupt") {
    if (data?.value?.type === "plan_review") showPlanReview(data);
    else if (data?.value?.type === "decision_table_adjudication") showDecisionTableAdjudication(data);
    else showReview(data);
  }
  if (event === "goal_state") renderGoalBadge(data?.goal);
  // 仅当前流的结束帧操作全局面板；被打断的旧流结束帧不覆盖新状态
  if (event === "end" && streamId === state.activeStreamId) {
    stopGoalPoll();
    if (!state.pendingInterrupt && !state.interruptedByUser) finishTracePanel("已完成");
    window.CaspianContextUi?.onRunEnded();
    // usage 落库先于关流，收到 end 时 updated_at 已刷新，可直接重排会话列表
    loadThreads();
  }
  if (event === "error") {
    stopGoalPoll();
    finishTracePanel("执行失败");
    window.CaspianContextUi?.onRunEnded();
    throw new Error(data?.error || "运行失败");
  }
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function submitGoalCommand(command) {
  if (state.running || state.pendingInterrupt) return;
  submitTask(`/goal ${command}`);
}

function startGoalEdit() {
  const input = $("#message-input");
  if (!input) return;
  input.value = "/goal edit ";
  input.focus();
}

function renderGoalBadge(goal) {
  const el = $("#goal-bar");
  if (!el) return;
  if (!goal || goal.phase === "complete" || goal.phase === "none") {
    el.hidden = true;
    el.innerHTML = "";
    return;
  }
  const phaseWord = ({
    active: "进行中的目标",
    paused: "已暂停的目标",
    blocked: "受阻的目标",
  })[goal.phase] || goal.phase;
  const rounds = `${goal.rounds_started}/${goal.max_goal_rounds}`;
  const objective = (goal.objective || "").slice(0, 80);
  const title = goal.blocked_reason
    ? `${goal.blocked_reason.code}: ${goal.blocked_reason.message}（${rounds} 轮）`
    : `${objective}（${rounds} 轮）`;
  // 对齐 deepseek-harness GoalBar：active→暂停；paused→续跑；blocked→仅编辑+清除（无续跑）
  const showToggle = goal.phase === "active" || goal.phase === "paused";
  const toggleCmd = goal.phase === "paused" ? "resume" : "pause";
  const toggleLabel = goal.phase === "paused" ? "续跑" : "暂停";
  const toggleIcon = goal.phase === "paused" ? "▶" : "⏸";
  el.title = title;
  el.innerHTML =
    `<span class="goal-bar-icon" aria-hidden="true">🎯</span>` +
    `<span class="goal-bar-text">` +
      `<span class="goal-bar-phase">${escapeHtml(phaseWord)}</span>` +
      `<span class="goal-bar-obj">${escapeHtml(objective)}</span>` +
    `</span>` +
    `<span class="goal-bar-actions">` +
      (showToggle ? `<button type="button" class="goal-bar-action" data-cmd="${toggleCmd}" title="${toggleLabel}" aria-label="${toggleLabel}">${toggleIcon}</button>` : "") +
      `<button type="button" class="goal-bar-action" data-cmd="edit" title="编辑目标" aria-label="编辑目标">✎</button>` +
      `<button type="button" class="goal-bar-action" data-cmd="clear" title="清除目标" aria-label="清除目标">🗑</button>` +
    `</span>`;
  el.hidden = false;
  el.querySelectorAll(".goal-bar-action").forEach((btn) => {
    btn.addEventListener("click", () => {
      const cmd = btn.dataset.cmd;
      if (cmd === "edit") startGoalEdit();
      else submitGoalCommand(cmd);
    });
  });
}

async function submitTask(content, selectedSkills = []) {
  const thread = currentThread();
  if (thread && thread.title === "新会话") {
    const autoTitle = content.replace(/\s+/g, " ").slice(0, 32);
    thread.title = autoTitle;
    thread.updatedAt = Date.now();
    saveThreads();
    renderThreads();
    // 与手动重命名共用权威源 web_threads.title（服务端不存在时懒创建）；
    // 失败不阻断发送，本地标题已即时生效
    renameThread(thread.id, autoTitle).catch(() => {});
  }
  state.followMessages = true;
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
    context: state.modelName ? { model_name: state.modelName } : undefined,
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

async function loadModels() {
  try {
    const res = await fetch("/api/models", { credentials: "same-origin" });
    if (!res.ok) return;
    const data = await res.json();
    state.models = Array.isArray(data.models) ? data.models : [];
    state.modelName = state.modelName || (state.models[0]?.name || null);
    renderModelToggle();
  } catch {
    // 模型列表加载失败不影响主流程
  }
}

function renderModelToggle() {
  const selected = state.models.find((m) => m.name === state.modelName);
  const name = $("#model-name");
  if (name) name.textContent = selected?.display_name || selected?.name || "";
  renderModelPopover();
}

function renderModelPopover() {
  const pop = $("#model-popover");
  if (!pop) return;
  pop.replaceChildren();
  state.models.forEach((m) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `model-option${m.name === state.modelName ? " is-selected" : ""}`;
    btn.setAttribute("role", "option");
    btn.dataset.model = m.name;
    btn.innerHTML = `<span class="mo-name"></span><span class="mo-desc"></span>`;
    $(".mo-name", btn).textContent = m.display_name || m.name;
    $(".mo-desc", btn).textContent = m.name;
    btn.addEventListener("click", () => selectModel(m.name));
    pop.append(btn);
  });
}

function selectModel(name) {
  state.modelName = name;
  renderModelToggle();
  closeModelPopover();
}

function openModelPopover() {
  const pop = $("#model-popover");
  if (!pop || !state.models.length) return;
  renderModelPopover();
  pop.hidden = false;
  $("#model-toggle")?.setAttribute("aria-expanded", "true");
}

function closeModelPopover() {
  const pop = $("#model-popover");
  if (pop) pop.hidden = true;
  $("#model-toggle")?.setAttribute("aria-expanded", "false");
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

async function showApp(user) {
  if (state.user?.id !== user.id) window.CaspianSkills?.clearCache();
  state.user = user;
  $("#login-view").hidden = true;
  $("#app-view").hidden = false;
  const name = user.display_name || user.email;
  $("#user-name").textContent = name;
  $("#user-avatar").textContent = name.slice(0, 1).toUpperCase();
  // 先以服务端为事实源装载会话全集，再决定是否需要新建，避免本地缓存为空时误建空会话
  await loadThreads();
  ensureThread();
  selectThread(state.threadId);
  loadModels();
}

async function restoreSession() {
  const response = await fetch("/api/auth/me", { credentials: "same-origin" });
  if (!response.ok) {
    showLogin();
    return;
  }
  const result = await response.json();
  await showApp(result.user);
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
    await showApp(result.user);
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
  createThread();
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
$("#messages").addEventListener("scroll", (event) => {
  state.followMessages = isNearBottom(event.currentTarget);
}, { passive: true });
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

$("#model-toggle")?.addEventListener("click", () => {
  const pop = $("#model-popover");
  if (pop?.hidden) openModelPopover();
  else closeModelPopover();
});

document.addEventListener("click", (event) => {
  if (!event.target.closest?.(".model-popover, .model-toggle")) closeModelPopover();
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeModelPopover();
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
  renameThread: (id, title) => {
    const thread = state.threads.find((item) => item.id === id);
    if (thread) {
      thread.title = title;
      thread.updatedAt = Date.now();
      saveThreads();
      renderThreads();
    }
  },
  selectThread,
});

// --- 决策等级表查看 ---
const _LEVEL_LABELS = { 3: "3 必须", 2: "2 可协商", 1: "1 可选" };

function renderDecisionTable(data) {
  const version = $("#decision-table-version");
  const body = $("#decision-table-body");
  if (!data.exists || !data.rows.length) {
    // 空表/新建：仍渲染可编辑表格与新增/提交，便于创建首条条目
    version.textContent = data.exists ? `版本 ${data.version}` : "";
  } else {
    version.textContent = `版本 ${data.version}`;
  }
  const table = document.createElement("table");
  table.className = "decision-table decision-table-editable";
  table.innerHTML = "<thead><tr><th>要求</th><th>决策</th><th>等级</th><th>守卫</th><th></th></tr></thead>";
  const tbody = document.createElement("tbody");
  const appendEditRow = (row) => {
    const r = row || {};
    const tr = document.createElement("tr");
    tr.dataset.id = r.id || "";
    try { tr.dataset.guards = JSON.stringify(r.guards || []); } catch { tr.dataset.guards = "[]"; }
    const tdReq = document.createElement("td");
    const inputReq = document.createElement("input");
    inputReq.type = "text";
    inputReq.value = r.requirement || "";
    inputReq.dataset.field = "requirement";
    tdReq.append(inputReq);
    const tdDec = document.createElement("td");
    const selectDec = document.createElement("select");
    selectDec.dataset.field = "decision";
    for (const d of ["保留", "丢弃"]) {
      const opt = document.createElement("option");
      opt.value = d;
      opt.textContent = d;
      if (d === (r.decision || "保留")) opt.selected = true;
      selectDec.append(opt);
    }
    tdDec.append(selectDec);
    const tdPri = document.createElement("td");
    const selectPri = document.createElement("select");
    selectPri.dataset.field = "priority";
    for (const [v, label] of Object.entries(_LEVEL_LABELS)) {
      const opt = document.createElement("option");
      opt.value = v;
      opt.textContent = label;
      if (Number(v) === (r.priority || 3)) opt.selected = true;
      selectPri.append(opt);
    }
    tdPri.append(selectPri);
    const tdGuard = document.createElement("td");
    tdGuard.className = "guard-cell";
    tdGuard.textContent = (r.guards || []).map((g) => `${g.kind}:${g.target} ${g.operator} "${g.pattern}"`).join("; ");
    const tdDel = document.createElement("td");
    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "icon-button icon-button-muted";
    delBtn.textContent = "删";
    delBtn.title = "删除该条目";
    delBtn.addEventListener("click", () => tr.remove());
    tdDel.append(delBtn);
    tr.append(tdReq, tdDec, tdPri, tdGuard, tdDel);
    tbody.append(tr);
  };
  for (const row of data.rows) appendEditRow(row);
  table.append(tbody);
  body.replaceChildren(table);

  const actions = document.createElement("div");
  actions.className = "decision-table-actions";
  const addBtn = document.createElement("button");
  addBtn.type = "button";
  addBtn.className = "button button-quiet";
  addBtn.textContent = "新增条目";
  addBtn.addEventListener("click", () => appendEditRow({}));
  const saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.className = "button button-primary";
  saveBtn.textContent = "提交编辑";
  const msg = document.createElement("p");
  msg.className = "decision-table-msg";
  saveBtn.addEventListener("click", () => {
    collectAndSubmitDecisionTable(body, msg);
  });
  actions.append(addBtn, saveBtn);
  body.append(actions, msg);
}

function collectRows(body) {
  const rows = [];
  body.querySelectorAll("tbody tr").forEach((tr) => {
    const requirement = tr.querySelector('[data-field="requirement"]')?.value || "";
    const decision = tr.querySelector('[data-field="decision"]')?.value || "";
    const priority = Number(tr.querySelector('[data-field="priority"]')?.value || 0);
    if (!requirement || !decision || !priority) return;
    const row = { requirement, decision, priority };
    const id = tr.dataset.id || "";
    if (id) row.id = id;
    let guards = [];
    try { guards = JSON.parse(tr.dataset.guards || "[]"); } catch { guards = []; }
    if (Array.isArray(guards) && guards.length) row.guards = guards;
    rows.push(row);
  });
  return rows;
}

function collectAndSubmitDecisionTable(body, msg) {
  const rows = collectRows(body);
  if (!rows.length) {
    msg.textContent = "没有可提交的条目";
    return;
  }
  msg.textContent = "提交中…";
  sendDecisionTableEdit(rows, msg);
}

async function sendDecisionTableEdit(rows, msg) {
  const content = "用户手工编辑了决策等级表，请据此执行冲突检测。";
  await streamRun({
    input: {
      messages: [{
        role: "user",
        content,
        additional_kwargs: { decision_table_edit: { rows } },
      }],
    },
    selected_skills: [],
    context: state.modelName ? { model_name: state.modelName } : undefined,
  });
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
function setDecisionTableOpen(open) {
  const wasOpen = !decisionTablePanel.hidden;
  const trigger = document.activeElement;
  decisionTablePanel.hidden = !open;
  $("#decision-table-toggle").setAttribute("aria-expanded", String(open));
  if (open && !wasOpen) {
    document.dispatchEvent(new CustomEvent("ui:surface-open", {
      detail: {
        surface: decisionTablePanel,
        trigger,
        modal: false,
        label: "决策等级表",
      },
    }));
  } else if (!open && wasOpen) {
    document.dispatchEvent(new CustomEvent("ui:surface-close", {
      detail: { surface: decisionTablePanel, label: "决策等级表" },
    }));
  }
}

$("#decision-table-toggle")?.addEventListener("click", () => {
  const willOpen = decisionTablePanel.hidden;
  setDecisionTableOpen(willOpen);
  if (willOpen) loadDecisionTable();
});
$("#decision-table-close")?.addEventListener("click", () => {
  setDecisionTableOpen(false);
});

restoreSession().catch(handleError);
