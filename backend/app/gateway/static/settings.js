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
    configPre.textContent = "正在读取 config.yaml…";
    try {
      const response = await fetch("/config.yaml", {
        headers: { Accept: "text/plain" },
        cache: "no-store",
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      configPre.textContent = await response.text();
    } catch (error) {
      configPre.textContent = "无法读取配置文件：config.yaml 未在同源提供。";
    }
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
