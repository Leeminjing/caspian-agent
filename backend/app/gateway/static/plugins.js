/* 插件调试视图：状态卡（Injected/Dependencies/Status）+ 执行参与链（plugin_trace 实时渲染）。 */
window.CaspianPlugins = (() => {
  const state = {
    traces: [],        // 最近执行参与记录（容量上限 200）
    open: false,
    loaded: false,
  };

  const $ = (sel, root = document) => root.querySelector(sel);

  /* ---------- SSE 事件消费 ---------- */

  function extractTrace(value) {
    if (!value || typeof value !== "object") return null;
    if (value.type === "plugin_trace") return value;
    if (Array.isArray(value) && value.length === 2
        && value[0] === "custom" && value[1] && value[1].type === "plugin_trace") {
      return value[1];
    }
    for (const item of Object.values(value)) {
      const hit = extractTrace(item);
      if (hit) return hit;
    }
    return null;
  }

  // 由 app.js 的 consumeGraphEvent 调用；返回 true 表示已消费该事件
  function consume(data) {
    const trace = extractTrace(data);
    if (!trace) return false;
    state.traces.push(trace);
    if (state.traces.length > 200) state.traces.splice(0, state.traces.length - 200);
    renderChain();
    return true;
  }

  /* ---------- 渲染 ---------- */

  const STATUS_LABEL = { ok: "成功", failed: "失败", timeout: "超时", skipped: "跳过" };
  const STATE_LABEL = { active: "Active", unavailable: "Unavailable" };

  function renderStatuses(plugins) {
    const body = $("#plugin-status-list");
    if (!body) return;
    if (!plugins.length) {
      body.innerHTML = '<p class="plugin-empty">未启用任何插件。在 extensions_config.json 的 plugins 段启用后，插件实现将注入系统。</p>';
      return;
    }
    body.innerHTML = "";
    for (const p of plugins) {
      const card = document.createElement("article");
      card.className = "plugin-card";
      const injected = (p.injected || []).map((i) => `<span class="chip">${escapeHtml(i)}</span>`).join("")
        || '<span class="plugin-muted">无注入</span>';
      const deps = (p.requires || []).length
        ? (p.requires || []).map((r) => `<span class="chip chip-dep">${escapeHtml(r)}</span>`).join("")
        : '<span class="plugin-muted">无依赖</span>';
      const issues = (p.issues || []).map((i) => `<li>${escapeHtml(i)}</li>`).join("");
      const missing = (p.missing_dependencies || []).map((m) => `<li>Missing dependency: ${escapeHtml(m)}</li>`).join("");
      card.innerHTML = `
        <header>
          <strong>${escapeHtml(p.display_name || p.name)}</strong>
          <span class="plugin-scope">${p.scope === "custom" ? "custom" : "public"}</span>
          <span class="plugin-state is-${p.state}">${STATE_LABEL[p.state] || p.state}</span>
        </header>
        <div class="plugin-rows">
          <div class="plugin-row"><span class="plugin-label">Injected</span>${injected}</div>
          <div class="plugin-row"><span class="plugin-label">Dependencies</span>${deps}</div>
          ${p.reason ? `<div class="plugin-row plugin-reason">${escapeHtml(p.reason)}</div>` : ""}
          ${issues ? `<ul class="plugin-issues">${issues}</ul>` : ""}
          ${missing ? `<ul class="plugin-issues">${missing}</ul>` : ""}
        </div>`;
      body.appendChild(card);
    }
  }

  function renderChain() {
    const list = $("#plugin-trace-list");
    if (!list) return;
    const traces = state.traces.slice(-50).reverse();
    if (!traces.length) {
      list.innerHTML = '<p class="plugin-empty">暂无执行记录。插件实现参与接口执行时实时显示。</p>';
      return;
    }
    list.innerHTML = "";
    for (const t of traces) {
      const li = document.createElement("li");
      li.className = `plugin-trace-item is-${t.status}`;
      const changed = t.changed ? " · 已变更" : "";
      li.innerHTML = `
        <span class="trace-iface">${escapeHtml(t.interface)}</span>
        <span class="trace-arrow">→</span>
        <strong>${escapeHtml(t.plugin)}</strong>
        <span class="trace-status">${STATUS_LABEL[t.status] || t.status}${changed}</span>
        <span class="trace-meta">${Math.round(t.latency_ms || 0)}ms</span>
        ${t.snapshot ? `<pre class="trace-snapshot">${escapeHtml(t.snapshot)}</pre>` : ""}
        ${t.detail ? `<p class="trace-detail">${escapeHtml(t.detail)}</p>` : ""}`;
      list.appendChild(li);
    }
  }

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  /* ---------- 面板开关与刷新 ---------- */

  function open() {
    const panel = $("#plugin-panel");
    const opening = !state.open;
    const trigger = document.activeElement;
    state.open = true;
    const toggle = $("#plugin-toggle");
    if (panel) panel.hidden = false;
    if (toggle) toggle.setAttribute("aria-expanded", "true");
    if (panel && opening) {
      document.dispatchEvent(new CustomEvent("ui:surface-open", {
        detail: { surface: panel, trigger, modal: false, label: "插件调试视图" },
      }));
    }
    refresh();
  }

  function close() {
    const panel = $("#plugin-panel");
    const wasOpen = state.open;
    state.open = false;
    const toggle = $("#plugin-toggle");
    if (panel) panel.hidden = true;
    if (toggle) toggle.setAttribute("aria-expanded", "false");
    if (panel && wasOpen) {
      document.dispatchEvent(new CustomEvent("ui:surface-close", {
        detail: { surface: panel, label: "插件调试视图" },
      }));
    }
  }

  function toggle() {
    state.open ? close() : open();
  }

  async function refresh() {
    try {
      const response = await fetch("/api/plugins", { credentials: "same-origin" });
      if (!response.ok) return;
      const data = await response.json();
      renderStatuses(Array.isArray(data.plugins) ? data.plugins : []);
      state.loaded = true;
    } catch (error) {
      console.warn("插件状态加载失败:", error);
    }
  }

  function init() {
    const toggleButton = $("#plugin-toggle");
    const closeBtn = $("#plugin-close");
    if (toggleButton) toggleButton.addEventListener("click", toggle);
    if (closeBtn) closeBtn.addEventListener("click", close);
    renderChain();
  }

  init();

  return { consume, refresh, open, close, toggle };
})();
