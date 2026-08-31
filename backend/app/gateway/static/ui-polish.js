/*
本文件对外提供 Caspian 网关前端的跨功能界面增强。

输入为既有界面派发的 ui:surface-open、ui:surface-close、ui:context-rail-change
与 ui:status 事件，以及窄屏导航按钮的用户操作。
输出为会话/Context 抽屉、面板焦点生命周期、背景隔离和专用状态播报。

具体工作流为：复用现有 DOM 切换窄屏抽屉；界面打开时保存触发控件并移动焦点；
模态 Context 编辑器打开时将主应用设为 inert；关闭时恢复背景与焦点。

示例：document 派发 ui:surface-open 后，本模块聚焦对应面板并保存触发按钮。
*/

(function uiPolishModule() {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const mobileQuery = window.matchMedia("(max-width: 720px)");
  const surfaceTriggers = new WeakMap();
  const state = {
    drawer: null,
    drawerTrigger: null,
    modalSurface: null,
    modalBackground: [],
  };

  function announce(message) {
    const region = $("#ui-status-announcer");
    if (!region || !message) return;
    region.textContent = "";
    requestAnimationFrame(() => {
      region.textContent = String(message);
    });
  }

  function focusElement(element) {
    if (!element?.isConnected || typeof element.focus !== "function") return;
    requestAnimationFrame(() => element.focus({ preventScroll: true }));
  }

  function drawerElement(kind) {
    return kind === "threads" ? $(".sidebar") : $("#context-rail");
  }

  function drawerToggle(kind) {
    return kind === "threads" ? $("#mobile-thread-toggle") : $("#mobile-context-toggle");
  }

  function drawerFocusTarget(kind) {
    const root = drawerElement(kind);
    if (!root) return null;
    if (kind === "threads") {
      return $(".thread-item.active", root) || $(".thread-item", root) || $("#new-thread", root);
    }
    return $(".context-rail-card.is-current", root)
      || $(".context-rail-card", root)
      || $(".context-rail-add", root);
  }

  function closeDrawer(restoreFocus = true) {
    if (!state.drawer) return;
    const trigger = state.drawerTrigger;
    document.body.classList.remove(
      "mobile-drawer-open",
      "mobile-threads-open",
      "mobile-context-open",
    );
    $("#mobile-drawer-backdrop").hidden = true;
    drawerToggle("threads")?.setAttribute("aria-expanded", "false");
    drawerToggle("context")?.setAttribute("aria-expanded", "false");
    state.drawer = null;
    state.drawerTrigger = null;
    if (restoreFocus) focusElement(trigger);
  }

  function openDrawer(kind, trigger) {
    if (!mobileQuery.matches || !drawerElement(kind)) return;
    closeDrawer(false);
    state.drawer = kind;
    state.drawerTrigger = trigger;
    document.body.classList.add("mobile-drawer-open", `mobile-${kind}-open`);
    $("#mobile-drawer-backdrop").hidden = false;
    drawerToggle(kind)?.setAttribute("aria-expanded", "true");
    focusElement(drawerFocusTarget(kind));
    announce(kind === "threads" ? "已打开会话列表" : "已打开 Context 列表");
  }

  function surfaceCloseControl(surface) {
    return $(
      "#decision-table-close, #plugin-close, #settings-close, [data-action=\"exit-context-editor\"]",
      surface,
    );
  }

  function closeOtherSurfaces(surface) {
    $$('[data-ui-surface]:not([hidden])').forEach((candidate) => {
      if (candidate === surface) return;
      surfaceCloseControl(candidate)?.click();
    });
  }

  function handleSurfaceOpen(event) {
    const detail = event.detail || {};
    const surface = detail.surface;
    if (!surface) return;
    closeDrawer(false);
    closeOtherSurfaces(surface);
    surfaceTriggers.set(surface, detail.trigger || document.activeElement);
    if (detail.modal) {
      state.modalSurface = surface;
      state.modalBackground = [
        $("#app-view"),
        $("#decision-table-panel"),
        $("#plugin-panel"),
        $(".context-block-banner"),
      ].filter((element) => element && element !== surface);
      state.modalBackground.forEach((element) => { element.inert = true; });
      document.body.classList.add("ui-modal-open");
    }
    focusElement(
      $("[autofocus]", surface)
      || $("input:not([disabled]), textarea:not([disabled]), button:not([disabled])", surface)
      || surface,
    );
    announce(`已打开${detail.label || "面板"}`);
  }

  function handleSurfaceClose(event) {
    const detail = event.detail || {};
    const surface = detail.surface;
    if (!surface) return;
    if (state.modalSurface === surface) {
      state.modalSurface = null;
      state.modalBackground.forEach((element) => { element.inert = false; });
      state.modalBackground = [];
      document.body.classList.remove("ui-modal-open");
    }
    const trigger = surfaceTriggers.get(surface);
    surfaceTriggers.delete(surface);
    focusElement(trigger);
    announce(`已关闭${detail.label || "面板"}`);
  }

  function handleContextRailChange(event) {
    const available = Boolean(event.detail?.available);
    const toggle = $("#mobile-context-toggle");
    if (toggle) toggle.hidden = !available;
    if (!available && state.drawer === "context") closeDrawer(false);
  }

  $("#mobile-thread-toggle")?.addEventListener("click", (event) => {
    if (state.drawer === "threads") closeDrawer();
    else openDrawer("threads", event.currentTarget);
  });

  $("#mobile-context-toggle")?.addEventListener("click", (event) => {
    if (state.drawer === "context") closeDrawer();
    else openDrawer("context", event.currentTarget);
  });

  $("#mobile-drawer-backdrop")?.addEventListener("click", () => closeDrawer());

  document.addEventListener("click", (event) => {
    if (!state.drawer) return;
    if (state.drawer === "threads" && event.target.closest?.(".thread-item, #new-thread")) {
      closeDrawer();
    }
    if (state.drawer === "context" && event.target.closest?.(".context-rail-card")) {
      closeDrawer();
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const surface = $$('[data-ui-surface]:not([hidden])').at(-1);
    if (surface) {
      const close = surfaceCloseControl(surface);
      if (close) {
        event.preventDefault();
        close.click();
      }
      return;
    }
    if (state.drawer) {
      event.preventDefault();
      closeDrawer();
    }
  });

  document.addEventListener("ui:surface-open", handleSurfaceOpen);
  document.addEventListener("ui:surface-close", handleSurfaceClose);
  document.addEventListener("ui:context-rail-change", handleContextRailChange);
  document.addEventListener("ui:status", (event) => announce(event.detail?.message));

  mobileQuery.addEventListener("change", (event) => {
    if (!event.matches) closeDrawer(false);
  });

  handleContextRailChange({ detail: { available: document.body.classList.contains("has-context-rail") } });
})();
