(function contextUiModule(global) {
  "use strict";

  const contextEditor = global.CaspianContextEditor;
  const PROJECTION_RUNNABLE = new Set(["root", "valid", "repaired", "approved"]);

  const state = {
    hooks: null,
    tree: [],
    draft: null,
    pointerDrag: null,
    undoTimer: null,
    uiSequence: 0,
    rail: null,
    banner: null,
    overlay: null,
  };

  function escapeHtml(value = "") {
    return String(value).replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  }

  function csrfToken() {
    const item = document.cookie
      .split("; ")
      .find((part) => part.startsWith("csrf_token="));
    return item ? decodeURIComponent(item.split("=").slice(1).join("=")) : "";
  }

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    const method = (options.method || "GET").toUpperCase();
    if (!["GET", "HEAD"].includes(method)) headers.set("X-CSRF-Token", csrfToken());
    if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
    const response = await fetch(path, { ...options, method, headers, credentials: "same-origin" });
    if (!response.ok) {
      let detail;
      try { detail = (await response.json()).detail; } catch { detail = await response.text(); }
      const error = new Error(typeof detail === "string" ? detail : (detail?.message || JSON.stringify(detail)));
      error.status = response.status;
      error.detail = detail;
      throw error;
    }
    if (response.status === 204) return null;
    return response.json();
  }

  function nextContextUiKey() {
    state.uiSequence += 1;
    return `context-ui-${state.uiSequence}`;
  }

  function threadTitle(contextId) {
    const thread = state.hooks.getThreads().find((item) => item.id === contextId);
    if (thread?.title && thread.title !== "新会话") return thread.title;
    const node = state.tree.find((item) => item.context_id === contextId);
    return node?.title || contextId.slice(0, 8);
  }

  // ---------------------------------------------------------------------------
  // init / rail / banner
  // ---------------------------------------------------------------------------

  function init(hooks) {
    state.hooks = hooks;
    state.rail = document.createElement("aside");
    state.rail.id = "context-rail";
    state.rail.className = "context-rail";
    state.rail.hidden = true;
    state.rail.innerHTML = `<header class="context-rail-heading"><strong>Contexts</strong><span>0</span></header>
      <nav class="context-rail-list" aria-label="当前聊天派生的 Context"></nav>`;
    const shell = document.querySelector("#app-view");
    if (shell) shell.append(state.rail);

    state.banner = document.createElement("section");
    state.banner.className = "context-block-banner";
    state.banner.hidden = true;
    document.body.append(state.banner);

    buildOverlay();
    attachListeners();
    refreshTree().catch(() => {});
  }

  function buildOverlay() {
    const overlay = document.createElement("section");
    overlay.id = "context-editor-overlay";
    overlay.className = "context-editor-view";
    overlay.dataset.uiSurface = "context-editor";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-label", "Context 编辑器");
    overlay.tabIndex = -1;
    overlay.hidden = true;
    overlay.innerHTML = `<aside class="context-source-panel"></aside>
      <section class="context-definition-panel"></section>`;
    document.body.append(overlay);
    state.overlay = overlay;
  }

  function currentTreeNode(contextId) {
    return state.tree.find((node) => node.context_id === contextId) || null;
  }

  function renderRail() {
    const rail = state.rail;
    const currentId = state.hooks.getThreadId();
    const tree = state.tree;
    if (!tree.length) {
      rail.hidden = true;
      document.body.classList.remove("has-context-rail");
      document.dispatchEvent(new CustomEvent("ui:context-rail-change", {
        detail: { available: false },
      }));
      return;
    }
    const family = currentId
      ? contextEditor.contextFamilyIds(tree, currentId)
      : new Set(tree.map((node) => node.context_id));
    const members = tree.filter((node) => family.has(node.context_id));
    const cards = members.map((node) => {
      const blocked = !PROJECTION_RUNNABLE.has(node.projection_status);
      const cacheRate = Number.isFinite(node.cache_hit_rate)
        ? `${Math.round(node.cache_hit_rate * 100)}%`
        : "—";
      const otherParents = (node.parents || []).slice(1)
        .map((parent) => threadTitle(parent.context_id))
        .join("、");
      return `<div class="context-rail-item${node.editable ? " is-editable" : ""}" style="--context-depth:${node.depth}">
        <button type="button" class="context-rail-card${node.context_id === currentId ? " is-current" : ""}${blocked ? " is-blocked" : ""}" data-action="context-rail-card" data-context-id="${escapeHtml(node.context_id)}" aria-current="${node.context_id === currentId ? "true" : "false"}">
          <span class="context-rail-title">${escapeHtml(threadTitle(node.context_id))}</span>
          <span class="context-rail-meta">${node.depth ? "派生 Context" : "根 Context"} · ${escapeHtml(node.context_id.slice(0, 8))}${blocked ? ` · ${escapeHtml(node.projection_status)}` : ""} · 缓存 ${cacheRate}</span>
          ${otherParents ? `<span class="context-rail-parents">另含：${escapeHtml(otherParents)}</span>` : ""}
        </button>
        ${node.editable ? `<button type="button" class="context-rail-edit" data-action="edit-context-definition" data-context-id="${escapeHtml(node.context_id)}">编辑</button>` : ""}
      </div>`;
    }).join("");
    rail.querySelector(".context-rail-heading span").textContent = String(members.length);
    rail.querySelector(".context-rail-list").innerHTML = `${cards}
      <button type="button" class="context-rail-add" data-action="derive-context">新增 Context</button>`;
    rail.hidden = false;
    document.body.classList.add("has-context-rail");
    document.dispatchEvent(new CustomEvent("ui:context-rail-change", {
      detail: { available: true },
    }));
  }

  function updateBanner() {
    const node = currentTreeNode(state.hooks.getThreadId());
    const blocked = node && !PROJECTION_RUNNABLE.has(node.projection_status);
    if (!blocked) {
      state.banner.hidden = true;
      return;
    }
    state.banner.innerHTML = `<strong>该 Context 尚未获得安全执行投影</strong><span>${escapeHtml(node.projection_status)}</span><button class="primary" data-action="resume-context-decision">查看并决断</button>`;
    state.banner.hidden = false;
  }

  async function refreshTree() {
    try {
      state.tree = await api("/api/contexts/tree");
    } catch (error) {
      state.tree = [];
    }
    renderRail();
    updateBanner();
  }

  function orderThreads(threads) {
    return contextEditor.orderTasksByTree(threads, state.tree);
  }

  function onThreadSelected() {
    refreshTree().catch(() => {});
  }

  function onRunEnded() {
    refreshTree().catch(() => {});
  }

  // ---------------------------------------------------------------------------
  // 派生编辑器
  // ---------------------------------------------------------------------------

  async function openDeriveEditor() {
    const contextId = state.hooks.getThreadId();
    if (!contextId) return showEditorError("请先选择或创建一个会话，再派生 Context");
    try {
      const snapshot = await api(`/api/contexts/${encodeURIComponent(contextId)}/snapshot`);
      if (!snapshot.checkpoint_id) throw new Error("该 Context 尚无可派生的已提交 checkpoint");
      const current = state.hooks.getCurrentThread();
      state.draft = {
        title: `${current?.title || "会话"} · 派生`,
        sources: [{ context_id: contextId, checkpoint_id: snapshot.checkpoint_id }],
        sourceSnapshots: [{ context_id: contextId, checkpoint_id: snapshot.checkpoint_id, messages: contextEditor.cloneMessages(snapshot.messages) }],
        activeSourceId: contextId,
        messages: [],
        uiKeys: [],
        expandedKey: null,
        undo: null,
        context: null,
      };
      showEditor();
    } catch (error) {
      showEditorError(error.message);
    }
  }

  async function reopenContextEditor(contextId) {
    try {
      const payload = await api(`/api/contexts/${encodeURIComponent(contextId)}`);
      if (!payload.sources || !payload.authored_messages) throw new Error("Context 数据不完整");
      state.draft = await draftFromContext(payload, payload.authored_messages);
      showEditor();
    } catch (error) {
      showEditorError(error.message);
    }
  }

  async function openDecisionFromBlock(detail) {
    // streamRun 收到 409 context_projection_blocked 时进入
    const context = detail?.context;
    if (!context || !context.authored_messages) return;
    try {
      state.draft = await draftFromContext(context, context.authored_messages);
      showEditor();
    } catch (error) {
      showEditorError(error.message);
    }
  }

  async function draftFromContext(context, messages) {
    const sources = contextEditor.cloneMessages(context.sources || []);
    const sourceSnapshots = [];
    for (const source of sources) {
      try {
        const checkpoint = encodeURIComponent(source.checkpoint_id);
        const snapshot = await api(`/api/contexts/${encodeURIComponent(source.context_id)}/snapshot?checkpoint_id=${checkpoint}`);
        sourceSnapshots.push({ ...source, messages: contextEditor.cloneMessages(snapshot.messages) });
      } catch (error) {
        sourceSnapshots.push({ ...source, messages: [] });
      }
    }
    const cloned = contextEditor.cloneMessages(messages);
    return {
      title: context.title || "Context",
      sources,
      sourceSnapshots,
      activeSourceId: sources[0]?.context_id || null,
      messages: cloned,
      uiKeys: contextEditor.createUiKeys(cloned, nextContextUiKey),
      expandedKey: null,
      undo: null,
      context,
    };
  }

  function showEditor() {
    const trigger = document.activeElement;
    const opening = state.overlay.hidden;
    state.overlay.hidden = false;
    document.body.style.overflow = "hidden";
    renderContextEditor();
    if (opening) {
      document.dispatchEvent(new CustomEvent("ui:surface-open", {
        detail: {
          surface: state.overlay,
          trigger,
          modal: true,
          label: "Context 编辑器",
        },
      }));
    }
  }

  function closeContextEditor() {
    const wasOpen = state.overlay && !state.overlay.hidden;
    cleanupContextPointerDrag();
    state.draft = null;
    state.overlay.hidden = true;
    document.body.style.overflow = "";
    if (wasOpen) {
      document.dispatchEvent(new CustomEvent("ui:surface-close", {
        detail: { surface: state.overlay, label: "Context 编辑器" },
      }));
    }
    refreshTree().catch(() => {});
  }

  function showEditorError(message) {
    const trigger = document.activeElement;
    const opening = state.overlay.hidden;
    state.overlay.hidden = false;
    document.body.style.overflow = "hidden";
    state.overlay.querySelector(".context-source-panel").innerHTML =
      `<p class="context-empty-source muted">${escapeHtml(message)}</p>`;
    state.overlay.querySelector(".context-definition-panel").innerHTML =
      `<header><div><h1>无法打开编辑器</h1></div><button class="text-button" data-action="exit-context-editor">关闭</button></header>`;
    if (opening) {
      document.dispatchEvent(new CustomEvent("ui:surface-open", {
        detail: {
          surface: state.overlay,
          trigger,
          modal: true,
          label: "Context 编辑器",
        },
      }));
    }
  }

  function syncContextDraft() {
    const draft = state.draft;
    if (!draft) return null;
    const panel = state.overlay.querySelector(".context-editor-view, .context-definition-panel");
    const root = state.overlay;
    if (!root) return draft;
    const titleInput = root.querySelector("[data-context-title]");
    if (titleInput) draft.title = titleInput.value.trim();
    draft.messages = contextEditor.readMessages(root, draft.messages);
    return draft;
  }

  function contextDraftLocked(draft = state.draft) {
    return Boolean(draft?.context && !draft.context.editable);
  }

  function ensureContextUiKeys(draft) {
    draft.uiKeys ||= [];
    while (draft.uiKeys.length < draft.messages.length) draft.uiKeys.push(nextContextUiKey());
    if (draft.uiKeys.length > draft.messages.length) draft.uiKeys.length = draft.messages.length;
    if (draft.expandedKey && !draft.uiKeys.includes(draft.expandedKey)) draft.expandedKey = null;
  }

  function contextSourcePanelMarkup(draft) {
    const created = Boolean(draft.context);
    const locked = contextDraftLocked(draft);
    const active = draft.sourceSnapshots?.find((source) => source.context_id === draft.activeSourceId) || draft.sourceSnapshots?.[0];
    const tabs = draft.sources.map((source, index) => {
      return `<span class="context-source-tab-wrap"><button type="button" class="context-source-tab${source.context_id === active?.context_id ? " is-active" : ""}" data-action="context-source-select" data-context-id="${escapeHtml(source.context_id)}">${escapeHtml(threadTitle(source.context_id))}</button>${!created && draft.sources.length > 1 ? `<button type="button" class="context-source-remove" data-action="remove-context-source" data-source-index="${index}" aria-label="移除此来源">×</button>` : ""}</span>`;
    }).join("");
    const sourceIds = new Set(draft.sources.map((source) => source.context_id));
    const candidates = state.tree.filter((item) => !sourceIds.has(item.context_id)).map((item) => {
      return `<button class="context-source-candidate" style="--context-depth:${item.depth}" data-action="add-context-source" data-context-id="${escapeHtml(item.context_id)}">${escapeHtml(threadTitle(item.context_id))}</button>`;
    }).join("") || '<span class="muted tiny">没有其他可加入的 Context</span>';
    return `<header class="context-source-heading"><div><h2>已有消息</h2></div><span class="muted tiny">拖到右侧</span></header>
      <div class="context-source-tabs" role="tablist" aria-label="来源 Context">${tabs}</div>
      <div class="context-source-message-list">${contextEditor.renderSourceMessages(active?.messages || [], active?.context_id || "", locked)}</div>
      <details class="context-source-add"${created ? " hidden" : ""}><summary>加入其他来源</summary><div class="context-source-tree">${candidates}</div></details>`;
  }

  function renderContextSourcePanel() {
    const panel = state.overlay.querySelector(".context-source-panel");
    if (panel && state.draft) panel.innerHTML = contextSourcePanelMarkup(state.draft);
  }

  function renderContextDecision(context) {
    if (!context) return "";
    const repairs = (context.repair_manifest || []).filter((item) => item.kind !== "regex_flag");
    const repairList = repairs.length
      ? `<details open><summary>Caspian 无损补齐了 ${repairs.length} 处协议结构</summary><pre>${escapeHtml(JSON.stringify(repairs, null, 2))}</pre></details>`
      : "";
    if (context.projection_status === "repaired" && !context.editable) {
      return `<section class="context-decision repaired"><h2>执行投影已安全生成</h2>${repairList}<button class="primary" data-action="open-derived-context">进入新 Context</button></section>`;
    }
    if (!["approval_required", "rejected", "initialization_failed"].includes(context.projection_status)) return repairList;
    const issues = (context.issues || []).map((issue) => `<article class="context-issue">
      <strong>${escapeHtml(issue.reason)}</strong>
      <div class="context-diff"><div><span>原始片段</span><pre>${escapeHtml(JSON.stringify(issue.original, null, 2))}</pre></div><div><span>拟议投影</span><pre>${escapeHtml(JSON.stringify(issue.proposed, null, 2))}</pre></div></div>
    </article>`).join("");
    const actions = context.projection_status === "approval_required"
      ? `<button class="primary" data-action="accept-context-projection">接受本次降级</button><button class="text-button" data-action="edit-context-projection">返回编辑</button><button class="text-button danger" data-action="cancel-context-projection">取消发送</button>`
      : `<button class="text-button" data-action="edit-context-projection">返回编辑</button><button class="text-button danger" data-action="exit-context-editor">关闭</button>`;
    return `<section class="context-decision blocked"><h2>需要你的决断</h2><p>Caspian 不会静默采用以下降级。</p>${repairList}${issues}<div class="dialog-actions">${actions}</div></section>`;
  }

  function renderContextEditor(scrollTop = null) {
    const draft = state.draft;
    if (!draft) return closeContextEditor();
    ensureContextUiKeys(draft);
    const created = Boolean(draft.context);
    const locked = contextDraftLocked(draft);
    state.overlay.querySelector(".context-source-panel").innerHTML = contextSourcePanelMarkup(draft);
    const panel = state.overlay.querySelector(".context-definition-panel");
    panel.innerHTML = `<header><div><h1>自由组装</h1></div><button class="text-button" data-action="exit-context-editor">关闭</button></header>
      <label>Context 标题<input data-context-title value="${escapeHtml(draft.title)}" ${created ? "disabled" : ""}></label>
      <div class="context-compose-heading"><div><strong>新 Context</strong><span>${draft.messages.length} 条消息</span></div><div><button class="text-button" data-action="context-message-add" ${locked ? "disabled" : ""}>新增消息</button><button class="text-button danger" data-action="context-message-clear" ${locked ? "disabled" : ""}>清空</button></div></div>
      <div class="context-message-list" data-context-drop-zone>${contextEditor.renderMessages(draft.messages, draft.uiKeys, draft.expandedKey, locked)}</div>
      ${renderContextDecision(draft.context)}
      <div class="context-undo-toast" hidden><span>已删除消息</span><button type="button" class="text-button" data-action="context-message-undo">撤销</button></div>
      ${locked ? "" : `<footer><span class="muted tiny">只有右侧内容会成为新 Context；来源始终保持不变。</span><button class="primary" data-action="submit-context">${draft.context ? "重新编译" : "创建 Context"}</button></footer>`}`;
    if (Number.isFinite(scrollTop)) panel.scrollTop = scrollTop;
  }

  // ---------------------------------------------------------------------------
  // 消息列表操作（局部 DOM 更新）
  // ---------------------------------------------------------------------------

  function contextMessageElement(message, index, uiKey, expanded = false) {
    const template = document.createElement("template");
    template.innerHTML = contextEditor.renderMessages([message], [uiKey], expanded ? uiKey : null, contextDraftLocked()).trim();
    const element = template.content.firstElementChild;
    element.dataset.contextMessageIndex = String(index);
    element.querySelector("[data-context-message-json]")?.setAttribute("data-context-message-index", String(index));
    return element;
  }

  function contextListPositions() {
    return new Map([...state.overlay.querySelectorAll(".context-message-editor")].map((row) => [row.dataset.contextUiKey, row.getBoundingClientRect().top]));
  }

  function animateContextReflow(previous) {
    if (!previous || window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
    state.overlay.querySelectorAll(".context-message-editor").forEach((row) => {
      const before = previous.get(row.dataset.contextUiKey);
      if (before == null) return;
      const delta = before - row.getBoundingClientRect().top;
      if (Math.abs(delta) > 1) row.animate([{ transform: `translateY(${delta}px)` }, { transform: "translateY(0)" }], { duration: 190, easing: "cubic-bezier(.2,.8,.2,1)" });
    });
  }

  function updateContextMessageIndices() {
    const draft = state.draft;
    const list = state.overlay.querySelector(".context-message-list");
    if (!draft || !list) return;
    const rows = [...list.querySelectorAll(".context-message-editor")];
    rows.forEach((row, index) => {
      row.dataset.contextMessageIndex = String(index);
      const role = draft.messages[index]?.role || "未指定角色";
      row.querySelector("header strong").textContent = `${String(index + 1).padStart(2, "0")} · ${role}`;
      const input = row.querySelector("[data-context-message-json]");
      if (input) {
        input.dataset.contextMessageIndex = String(index);
        input.setAttribute("aria-label", `消息 ${index + 1} JSON`);
      }
    });
    const count = state.overlay.querySelector(".context-compose-heading > div:first-child span");
    if (count) count.textContent = `${draft.messages.length} 条消息`;
    if (!rows.length && !list.querySelector(".context-empty-drop")) list.innerHTML = contextEditor.renderMessages([], []);
  }

  function insertContextMessage(message, index, options = {}) {
    const draft = syncContextDraft();
    ensureContextUiKeys(draft);
    const list = state.overlay.querySelector(".context-message-list");
    const previous = contextListPositions();
    const safeIndex = Math.max(0, Math.min(Number(index), draft.messages.length));
    const key = options.key || nextContextUiKey();
    draft.messages.splice(safeIndex, 0, contextEditor.cloneMessages([message])[0]);
    draft.uiKeys.splice(safeIndex, 0, key);
    if (options.expand) draft.expandedKey = key;
    list.querySelector(".context-empty-drop")?.remove();
    const rows = [...list.querySelectorAll(".context-message-editor")];
    list.insertBefore(contextMessageElement(draft.messages[safeIndex], safeIndex, key, Boolean(options.expand)), rows[safeIndex] || null);
    updateContextMessageIndices();
    animateContextReflow(previous);
    if (options.focus) requestAnimationFrame(() => list.querySelector(`[data-context-ui-key="${key}"] [data-context-message-json]`)?.focus());
    return key;
  }

  function moveContextMessage(from, to, focus = false) {
    const draft = syncContextDraft();
    if (from < 0 || from >= draft.messages.length || to < 0 || to >= draft.messages.length || from === to) return;
    const previous = contextListPositions();
    const key = draft.uiKeys[from];
    contextEditor.move(draft.messages, from, to);
    contextEditor.move(draft.uiKeys, from, to);
    const rows = new Map([...state.overlay.querySelectorAll(".context-message-editor")].map((row) => [row.dataset.contextUiKey, row]));
    const list = state.overlay.querySelector(".context-message-list");
    draft.uiKeys.forEach((uiKey) => list.append(rows.get(uiKey)));
    updateContextMessageIndices();
    animateContextReflow(previous);
    if (focus) state.overlay.querySelector(`[data-context-ui-key="${key}"] [data-context-pointer-handle]`)?.focus();
  }

  function toggleContextMessage(uiKey) {
    const draft = syncContextDraft();
    const previousExpanded = draft.expandedKey;
    draft.expandedKey = previousExpanded === uiKey ? null : uiKey;
    const replace = (key) => {
      if (!key) return;
      const index = draft.uiKeys.indexOf(key);
      const row = state.overlay.querySelector(`[data-context-ui-key="${key}"]`);
      if (row && index >= 0) row.replaceWith(contextMessageElement(draft.messages[index], index, key, draft.expandedKey === key));
    };
    replace(previousExpanded);
    if (uiKey !== previousExpanded) replace(uiKey);
    updateContextMessageIndices();
    if (draft.expandedKey) requestAnimationFrame(() => state.overlay.querySelector(`[data-context-ui-key="${draft.expandedKey}"] [data-context-message-json]`)?.focus());
  }

  function showContextUndo() {
    clearTimeout(state.undoTimer);
    const toast = state.overlay.querySelector(".context-undo-toast");
    if (toast) toast.hidden = false;
    state.undoTimer = setTimeout(() => {
      if (state.draft) state.draft.undo = null;
      const current = state.overlay.querySelector(".context-undo-toast");
      if (current) current.hidden = true;
    }, 4500);
  }

  async function deleteContextMessage(index) {
    const draft = syncContextDraft();
    const row = state.overlay.querySelector(`.context-message-editor[data-context-message-index="${index}"]`);
    if (!row) return;
    const panel = state.overlay.querySelector(".context-definition-panel");
    const scrollTop = panel?.scrollTop || 0;
    const previous = contextListPositions();
    const [message] = draft.messages.splice(index, 1);
    const [key] = draft.uiKeys.splice(index, 1);
    if (draft.expandedKey === key) draft.expandedKey = null;
    draft.undo = { message, key, index };
    if (!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) {
      const animation = row.animate([{ opacity: 1, transform: "scale(1)", maxHeight: `${row.offsetHeight}px` }, { opacity: 0, transform: "scale(.98)", maxHeight: "0px" }], { duration: 150, easing: "ease-in", fill: "forwards" });
      await Promise.race([animation.finished.catch(() => {}), new Promise((resolve) => setTimeout(resolve, 180))]);
    }
    row.remove();
    updateContextMessageIndices();
    if (panel) panel.scrollTop = Math.min(scrollTop, Math.max(0, panel.scrollHeight - panel.clientHeight));
    animateContextReflow(previous);
    showContextUndo();
  }

  function undoContextMessageDelete() {
    const draft = state.draft;
    if (!draft?.undo) return;
    const undo = draft.undo;
    draft.undo = null;
    clearTimeout(state.undoTimer);
    state.overlay.querySelector(".context-undo-toast")?.setAttribute("hidden", "");
    insertContextMessage(undo.message, undo.index, { key: undo.key });
  }

  function clearContextMessages() {
    const draft = syncContextDraft();
    draft.messages = [];
    draft.uiKeys = [];
    draft.expandedKey = null;
    state.overlay.querySelector(".context-message-list").innerHTML = contextEditor.renderMessages([], []);
    state.overlay.querySelector(".context-compose-heading > div:first-child span").textContent = "0 条消息";
  }

  async function addContextSource(contextId) {
    const draft = syncContextDraft();
    if (!draft || draft.sources.some((source) => source.context_id === contextId)) return;
    try {
      const snapshot = await api(`/api/contexts/${encodeURIComponent(contextId)}/snapshot`);
      if (!snapshot.checkpoint_id) throw new Error("来源 Context 尚无已提交 checkpoint");
      draft.sources.push({ context_id: contextId, checkpoint_id: snapshot.checkpoint_id });
      draft.sourceSnapshots.push({ context_id: contextId, checkpoint_id: snapshot.checkpoint_id, messages: contextEditor.cloneMessages(snapshot.messages) });
      draft.activeSourceId = contextId;
      renderContextSourcePanel();
    } catch (error) {
      showEditorError(error.message);
    }
  }

  async function submitContext() {
    let draft;
    try { draft = syncContextDraft(); }
    catch (error) { return showEditorError(error.message); }
    if (!draft.title) return showEditorError("Context 标题不能为空");
    try {
      const context = draft.context
        ? await api(`/api/contexts/${encodeURIComponent(draft.context.context_id)}/definition`, { method: "PUT", body: JSON.stringify({ messages: draft.messages }) })
        : await api("/api/contexts/derive", { method: "POST", body: JSON.stringify({ title: draft.title, sources: draft.sources, messages: draft.messages }) });
      draft.context = context;
      await refreshTree();
      if (context.projection_status === "valid") return openDerivedContext();
      renderContextEditor();
    } catch (error) {
      showEditorError(error.message);
    }
  }

  async function decideContextProjection(decision) {
    const context = state.draft?.context;
    if (!context) return;
    try {
      const updated = await api(`/api/contexts/${encodeURIComponent(context.context_id)}/projection/decision`, {
        method: "POST",
        body: JSON.stringify({ decision, definition_hash: context.definition_hash, projection_hash: context.projection_hash }),
      });
      state.draft.context = updated;
      if (decision === "accept") return openDerivedContext();
      renderContextEditor();
    } catch (error) {
      showEditorError(error.message);
    }
  }

  async function openDerivedContext() {
    const context = state.draft?.context;
    if (!context) return;
    const contextId = context.context_id;
    state.draft = null;
    state.overlay.hidden = true;
    document.body.style.overflow = "";
    state.hooks.addThread({ id: contextId, title: context.title || "新 Context", updatedAt: Date.now() });
    state.hooks.selectThread(contextId);
    await refreshTree();
  }

  // ---------------------------------------------------------------------------
  // Pointer Events 拖拽（无依赖，浏览器原生）
  // ---------------------------------------------------------------------------

  function beginContextPointerDrag(drag, event) {
    try { syncContextDraft(); }
    catch (error) { return cancelContextPointerDrag(); }
    const rect = drag.card.getBoundingClientRect();
    drag.started = true;
    drag.offsetX = Math.max(18, Math.min(event.clientX - rect.left, rect.width - 18));
    drag.offsetY = Math.max(18, Math.min(event.clientY - rect.top, rect.height - 18));
    drag.preview = drag.card.cloneNode(true);
    drag.preview.className = "context-drag-preview";
    drag.preview.removeAttribute("data-context-message-index");
    drag.preview.querySelectorAll("button, textarea, details").forEach((node) => { node.tabIndex = -1; });
    drag.preview.style.width = `${rect.width}px`;
    drag.placeholder = document.createElement("div");
    drag.placeholder.className = "context-drop-placeholder";
    drag.placeholderHeight = Math.min(rect.height, 180);
    drag.placeholder.style.height = `${drag.placeholderHeight}px`;
    drag.card.classList.add("is-lifted");
    document.body.append(drag.preview);
    state.overlay.querySelector(".context-message-list")?.classList.add("is-drag-active");
    positionContextDragPreview(event.clientX, event.clientY);
    drag.autoFrame = requestAnimationFrame(runContextAutoScroll);
  }

  function positionContextDragPreview(clientX, clientY) {
    if (!state.pointerDrag?.preview) return;
    state.pointerDrag.clientX = clientX;
    state.pointerDrag.clientY = clientY;
    state.pointerDrag.preview.style.transform = `translate3d(${clientX - state.pointerDrag.offsetX}px, ${clientY - state.pointerDrag.offsetY}px, 0) scale(1.015)`;
  }

  function updateContextDropTarget(clientX, clientY) {
    const drag = state.pointerDrag;
    const list = state.overlay.querySelector(".context-message-list");
    if (!drag?.started || !list) return;
    const rect = list.getBoundingClientRect();
    const inside = clientX >= rect.left && clientX <= rect.right && clientY >= rect.top && clientY <= rect.bottom;
    if (!inside) {
      drag.dropIndex = null;
      drag.placeholder?.remove();
      return;
    }
    // 占位条绝对定位悬浮在落点间隙：不占布局空间 → 卡片不被推移，
    // 落点计算基于稳定布局，无震荡也无闪烁（此前占位条占位导致卡片跳动）
    const rows = [...list.querySelectorAll(".context-message-editor")].filter((row) => row !== drag.card);
    let index = rows.findIndex((row) => clientY < row.getBoundingClientRect().top + row.getBoundingClientRect().height / 2);
    if (index < 0) index = rows.length;
    drag.dropIndex = index;
    let topPx;
    if (index < rows.length) {
      const box = rows[index].getBoundingClientRect();
      if (index > 0) {
        const before = rows[index - 1].getBoundingClientRect();
        topPx = (before.bottom + box.top) / 2 - rect.top - drag.placeholderHeight / 2;
      } else {
        topPx = box.top - rect.top - drag.placeholderHeight / 2 - 4;
      }
    } else if (rows.length) {
      topPx = rows[rows.length - 1].getBoundingClientRect().bottom - rect.top + 4;
    } else {
      topPx = 0;
    }
    drag.placeholder.style.top = `${topPx}px`;
    if (!drag.placeholder.isConnected) list.append(drag.placeholder);
  }

  function scrollContextPanelNearPointer(clientY) {
    const panel = state.overlay.querySelector(".context-definition-panel");
    const rect = panel?.getBoundingClientRect();
    if (panel && rect) {
      const edge = 58;
      const delta = clientY < rect.top + edge ? -12 : clientY > rect.bottom - edge ? 12 : 0;
      if (delta) {
        panel.scrollTop += delta;
        return true;
      }
    }
    return false;
  }

  function runContextAutoScroll() {
    const drag = state.pointerDrag;
    if (!drag?.started) return;
    if (scrollContextPanelNearPointer(drag.clientY)) updateContextDropTarget(drag.clientX, drag.clientY);
    drag.autoFrame = requestAnimationFrame(runContextAutoScroll);
  }

  function cleanupContextPointerDrag(keepPreview = false) {
    const drag = state.pointerDrag;
    if (!drag) return null;
    state.pointerDrag = null;
    cancelAnimationFrame(drag.autoFrame);
    drag.placeholder?.remove();
    drag.card?.classList.remove("is-lifted");
    state.overlay.querySelector(".context-message-list")?.classList.remove("is-drag-active");
    if (!keepPreview) drag.preview?.remove();
    try {
      if (drag.handle.hasPointerCapture?.(drag.pointerId)) drag.handle.releasePointerCapture(drag.pointerId);
    } catch { /* 捕获已释放时忽略 */ }
    return drag;
  }

  function cancelContextPointerDrag() {
    const drag = cleanupContextPointerDrag(true);
    if (!drag?.preview) return;
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return drag.preview.remove();
    const remove = () => drag.preview.remove();
    drag.preview.animate([{ opacity: 1 }, { opacity: 0, transform: `${drag.preview.style.transform} scale(.96)` }], { duration: 120, easing: "ease-out" }).finished.finally(remove);
    setTimeout(remove, 150);
  }

  function settleContextPointerPreview(drag, uiKey) {
    if (!drag.preview) return;
    const target = state.overlay.querySelector(`[data-context-ui-key="${uiKey}"]`);
    if (!target || window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return drag.preview.remove();
    const rect = target.getBoundingClientRect();
    const remove = () => drag.preview.remove();
    drag.preview.animate([
      { transform: drag.preview.style.transform, opacity: 1 },
      { transform: `translate3d(${rect.left}px, ${rect.top}px, 0) scale(1)`, opacity: 0.2 },
    ], { duration: 170, easing: "cubic-bezier(.2,.8,.2,1)" }).finished.finally(remove);
    setTimeout(remove, 200);
  }

  // ---------------------------------------------------------------------------
  // 事件委托
  // ---------------------------------------------------------------------------

  function handleAction(action, element) {
    switch (action) {
      case "context-rail-card":
        state.hooks.selectThread(element.dataset.contextId);
        break;
      case "derive-context":
        return openDeriveEditor();
      case "edit-context-definition":
        return reopenContextEditor(element.dataset.contextId);
      case "resume-context-decision":
        return reopenContextEditor(state.hooks.getThreadId());
      case "exit-context-editor":
        return closeContextEditor();
      case "context-source-select": {
        const draft = syncContextDraft();
        draft.activeSourceId = element.dataset.contextId;
        renderContextSourcePanel();
        break;
      }
      case "remove-context-source": {
        const draft = syncContextDraft();
        const index = Number(element.dataset.sourceIndex);
        const [removed] = draft.sources.splice(index, 1);
        draft.sourceSnapshots = draft.sourceSnapshots.filter((source) => source.context_id !== removed.context_id);
        if (draft.activeSourceId === removed.context_id) draft.activeSourceId = draft.sources[0]?.context_id || null;
        renderContextSourcePanel();
        break;
      }
      case "add-context-source":
        return addContextSource(element.dataset.contextId);
      case "context-source-copy": {
        const sourceCard = element.closest(".context-source-message");
        const sourceContextId = sourceCard?.dataset.sourceContextId;
        const sourceIndex = Number(sourceCard?.dataset.contextSourceIndex);
        const source = state.draft.sourceSnapshots.find((item) => item.context_id === sourceContextId);
        if (source?.messages[sourceIndex]) insertContextMessage(source.messages[sourceIndex], state.draft.messages.length);
        break;
      }
      case "context-message-add":
        insertContextMessage({ role: "human", content: "" }, state.draft.messages.length, { expand: true, focus: true });
        break;
      case "context-message-copy": {
        const index = Number(element.closest(".context-message-editor")?.dataset.contextMessageIndex);
        insertContextMessage(state.draft.messages[index], index + 1);
        break;
      }
      case "context-message-delete":
        return deleteContextMessage(Number(element.closest(".context-message-editor")?.dataset.contextMessageIndex));
      case "context-message-undo":
        return undoContextMessageDelete();
      case "context-message-clear":
        return clearContextMessages();
      case "context-message-toggle": {
        const uiKey = element.closest(".context-message-editor")?.dataset.contextUiKey;
        if (uiKey) toggleContextMessage(uiKey);
        break;
      }
      case "submit-context":
        return submitContext();
      case "accept-context-projection":
        return decideContextProjection("accept");
      case "cancel-context-projection":
        return decideContextProjection("reject");
      case "edit-context-projection":
        // 保留 context（重新编译走 PUT definition），只重新渲染编辑器
        return renderContextEditor();
      case "open-derived-context":
        return openDerivedContext();
      default:
        break;
    }
    return undefined;
  }

  function attachListeners() {
    document.addEventListener("click", (event) => {
      const actionElement = event.target.closest?.("[data-action]");
      if (!actionElement) return;
      const action = actionElement.dataset.action;
      if (!action || !action.startsWith("context") && !["derive-context", "edit-context-definition", "resume-context-decision", "exit-context-editor", "submit-context", "accept-context-projection", "cancel-context-projection", "edit-context-projection", "open-derived-context", "add-context-source", "remove-context-source"].includes(action)) return;
      event.preventDefault();
      handleAction(action, actionElement);
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && state.pointerDrag) {
        event.preventDefault();
        return cancelContextPointerDrag();
      }
      if (event.key === "Escape" && state.draft && state.overlay && !state.overlay.hidden && !state.pointerDrag) {
        return closeContextEditor();
      }
      const contextHandle = event.target.closest?.('[data-context-pointer-handle][data-context-drag-origin="draft"]');
      if (contextHandle && event.altKey && ["ArrowUp", "ArrowDown"].includes(event.key)) {
        event.preventDefault();
        const index = Number(contextHandle.closest("[data-context-message-index]").dataset.contextMessageIndex);
        const target = index + (event.key === "ArrowUp" ? -1 : 1);
        try {
          if (target >= 0 && target < state.draft.messages.length) moveContextMessage(index, target, true);
        } catch (error) {
          showEditorError(error.message);
        }
      }
    });

    document.addEventListener("pointerdown", (event) => {
      if (!state.draft || state.overlay.hidden) return;
      const handle = event.target.closest?.("[data-context-pointer-handle]");
      if (!handle || handle.disabled || event.button !== 0) return;
      const origin = handle.dataset.contextDragOrigin;
      const card = origin === "source" ? handle.closest(".context-source-message") : handle.closest(".context-message-editor");
      if (!card) return;
      event.preventDefault();
      state.pointerDrag = {
        pointerId: event.pointerId,
        handle,
        card,
        origin,
        uiKey: card.dataset.contextUiKey,
        sourceContextId: card.dataset.sourceContextId,
        sourceIndex: Number(card.dataset.contextSourceIndex),
        startX: event.clientX,
        startY: event.clientY,
        clientX: event.clientX,
        clientY: event.clientY,
        started: false,
        dropIndex: null,
      };
      try { handle.setPointerCapture(event.pointerId); } catch { /* 忽略 */ }
    });

    document.addEventListener("pointermove", (event) => {
      const drag = state.pointerDrag;
      if (!drag || event.pointerId !== drag.pointerId) return;
      if (!drag.started && Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) < 5) return;
      event.preventDefault();
      if (!drag.started) beginContextPointerDrag(drag, event);
      if (!state.pointerDrag) return;
      positionContextDragPreview(event.clientX, event.clientY);
      updateContextDropTarget(event.clientX, event.clientY);
      if (scrollContextPanelNearPointer(event.clientY)) updateContextDropTarget(event.clientX, event.clientY);
    });

    document.addEventListener("pointerup", (event) => {
      const current = state.pointerDrag;
      if (!current || event.pointerId !== current.pointerId) return;
      if (!current.started || current.dropIndex == null) return cancelContextPointerDrag();
      const dropIndex = current.dropIndex;
      const drag = cleanupContextPointerDrag(true);
      try {
        let uiKey;
        if (drag.origin === "source") {
          const source = state.draft.sourceSnapshots.find((item) => item.context_id === drag.sourceContextId);
          uiKey = insertContextMessage(source.messages[drag.sourceIndex], dropIndex);
        } else {
          const from = state.draft.uiKeys.indexOf(drag.uiKey);
          const to = Math.max(0, Math.min(dropIndex, state.draft.messages.length - 1));
          moveContextMessage(from, to);
          uiKey = state.draft.uiKeys[to];
        }
        settleContextPointerPreview(drag, uiKey);
      } catch (error) {
        showEditorError(error.message);
      }
    });

    document.addEventListener("pointercancel", () => cancelContextPointerDrag());
  }

  global.CaspianContextUi = {
    init,
    onThreadSelected,
    onRunEnded,
    openDecisionFromBlock,
    orderThreads,
  };
})(window);
