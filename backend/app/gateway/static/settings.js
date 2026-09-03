/*
本文件对外提供 Caspian 网关前端的设置导航模块。

输入为左下角账号行与设置菜单的用户操作，以及文档级 ui:archived-changed 事件。
输出为设置菜单的开合、中央「设置窗口」的打开/关闭、左侧「数据」分类下右侧内容区的
「已归档的会话」子入口导航，以及「已归档的会话」页的内联归档列表渲染。

具体工作流为：点击账号行开合「设置」菜单；点击菜单中的「设置」打开中央「设置窗口」；
窗口左侧为「数据」分类，右侧内容区在「数据」页列出子入口「已归档的会话」，点击该子入口
进入「已归档的会话」页，复用 app.js 的 loadArchived/renderArchivedList 内联列出归档会话，
并提供「← 数据」返回；「打开配置文件」尝试在同源读取 config.yaml 并展示。

示例：settings.js 在 app.js 之后加载，故可直接调用全局的 loadArchived / renderArchivedList / restoreThread。
*/

(function settingsModule() {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

  const menuButton = $("#user-menu-button");
  const menu = $("#settings-menu");
  const windowEl = $("#settings-window");
  const dataNav = $('.settings-nav-item[data-settings-view="data"]', windowEl || document);
  const configPre = $("#settings-config");

  // 配置编辑器运行时引用（由 buildConfigEditor 创建）
  let configEditor = null;
  let configStatus = null;

  // 菜单开合
  function setMenu(open) {
    if (!menu) return;
    menu.hidden = !open;
    menuButton?.setAttribute("aria-expanded", String(open));
  }

  function closeMenu() {
    setMenu(false);
  }

  function toggleMenu(event) {
    event.stopPropagation();
    setMenu(menu.hidden);
  }

  // 设置窗口开合
  function openSettingsWindow(event) {
    event?.stopPropagation();
    if (!windowEl) return;
    closeMenu();
    windowEl.hidden = false;
    document.dispatchEvent(new CustomEvent("ui:surface-open", {
      detail: {
        surface: windowEl,
        trigger: menuButton || document.activeElement,
        modal: false,
        label: "设置",
      },
    }));
    showData();
  }

  function closeSettingsWindow() {
    if (!windowEl || windowEl.hidden) return;
    windowEl.hidden = true;
    document.dispatchEvent(new CustomEvent("ui:surface-close", {
      detail: { surface: windowEl, label: "设置" },
    }));
  }

  // 分页切换（左侧始终高亮「数据」分类）
  function showPage(name) {
    dataNav?.classList.add("is-active");
    $$(".settings-page", windowEl).forEach((page) => {
      page.hidden = page.dataset.settingsPage !== name;
    });
  }

  // 落在「数据」页（列出子入口）
  function showData() {
    showPage("data");
  }

  // 落在「已归档的会话」页（列出归档列表）
  function showArchived() {
    showPage("archived");
    refreshArchived();
  }

  async function refreshArchived() {
    const container = $("#settings-archived-list");
    if (!container) return;
    try {
      const items = await loadArchived();
      renderArchivedList(items, container);
    } catch (error) {
      container.innerHTML = '<p class="archived-empty">加载失败</p>';
    }
  }

  async function openConfigFile() {
    if (!configPre) return;
    configPre.hidden = false;
    // 构建可编辑视图：textarea + 保存/取消
    if (!configPre.dataset.editorBuilt) {
      buildConfigEditor();
    }
    configEditor.value = "正在读取 config.yaml…";
    try {
      const response = await fetch("/api/config", {
        headers: { Accept: "text/plain" },
        cache: "no-store",
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      configEditor.value = await response.text();
    } catch (error) {
      configEditor.value = "";
      configStatus.textContent = "无法读取配置文件：请确认已登录且后端 /api/config 可用。";
    }
  }

  function buildConfigEditor() {
    // 单次构建：textarea + 状态行 + 保存/取消按钮
    configPre.classList.add("config-editor");
    configPre.textContent = "";

    configEditor = document.createElement("textarea");
    configEditor.className = "config-editor-textarea";
    configEditor.setAttribute("aria-label", "config.yaml 编辑区");
    configEditor.spellcheck = false;
    configPre.appendChild(configEditor);

    configStatus = document.createElement("p");
    configStatus.className = "config-editor-status";
    configPre.appendChild(configStatus);

    const actions = document.createElement("div");
    actions.className = "config-editor-actions";

    const saveBtn = document.createElement("button");
    saveBtn.type = "button";
    saveBtn.className = "button button-primary";
    saveBtn.textContent = "保存";
    saveBtn.addEventListener("click", saveConfig);

    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.className = "button button-quiet";
    cancelBtn.textContent = "取消";
    cancelBtn.addEventListener("click", cancelConfigEdit);

    actions.appendChild(saveBtn);
    actions.appendChild(cancelBtn);
    configPre.appendChild(actions);

    configPre.dataset.editorBuilt = "1";
  }

  async function saveConfig() {
    if (!configEditor) return;
    configStatus.textContent = "正在保存…";
    try {
      const response = await fetch("/api/config", {
        method: "PUT",
        headers: {
          "Content-Type": "text/plain",
          "X-CSRF-Token": csrfToken(),
        },
        body: configEditor.value,
        cache: "no-store",
      });
      if (response.status === 401) {
        configStatus.textContent = "保存失败：未登录，请重新登录。";
        return;
      }
      if (response.status === 403) {
        configStatus.textContent = "保存失败：CSRF 校验未通过，请刷新页面重试。";
        return;
      }
      if (!response.ok) {
        let detail = `HTTP ${response.status}`;
        try {
          const body = await response.json();
          if (body && body.detail) detail = body.detail;
        } catch (_) { /* 非 JSON，保留状态码 */ }
        configStatus.textContent = `保存失败：${detail}`;
        return;
      }
      const data = await response.json();
      configStatus.textContent = data.detail || "配置已保存，将在下一次 run 生效。";
    } catch (error) {
      configStatus.textContent = "保存失败：网络异常或端点不可用。";
    }
  }

  function cancelConfigEdit() {
    if (!configEditor || !configStatus) return;
    configEditor.value = "";
    configStatus.textContent = "已取消编辑，未保存的改动已丢弃。";
    openConfigFile();
  }

  // 事件绑定
  menuButton?.addEventListener("click", toggleMenu);
  $("#open-settings")?.addEventListener("click", openSettingsWindow);
  $("#settings-close")?.addEventListener("click", closeSettingsWindow);
  $("#open-config-file")?.addEventListener("click", openConfigFile);

  dataNav?.addEventListener("click", showData);
  $("#open-archived")?.addEventListener("click", showArchived);
  $("#back-to-data")?.addEventListener("click", showData);

  // 点击菜单外收起菜单；点击设置窗口遮罩/外部关闭窗口
  document.addEventListener("click", (event) => {
    if (menu && !menu.hidden && !event.target.closest?.(".user-row")) {
      closeMenu();
    }
    if (windowEl && !windowEl.hidden && (event.target === windowEl || !windowEl.contains(event.target))) {
      closeSettingsWindow();
    }
  });

  // 归档会话列表变化时（如恢复后）刷新内联列表
  document.addEventListener("ui:archived-changed", () => {
    if (windowEl && !windowEl.hidden) refreshArchived();
  });
})();
