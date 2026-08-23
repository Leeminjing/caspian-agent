(function () {
  let catalog = null;
  let loading = null;
  let selected = [];
  let chipHost = null;

  const COMMIT_ENTRY = {
    name: "commit",
    description: "启动九阶段承诺流程",
    command: true,
    token: "commit",
  };

  const PLAN_ENTRY = {
    name: "plan",
    description: "进入计划模式（先规划后执行，经 exit_plan_mode 评审）",
    command: true,
    token: "plan",
  };

  function commitVisible(query) {
    return "commit".startsWith(String(query || "").toLowerCase());
  }

  function planVisible(query) {
    return "plan".startsWith(String(query || "").toLowerCase());
  }

  function normalizeSkill(raw) {
    return {
      name: String(raw?.name || ""),
      description: String(raw?.description || ""),
    };
  }

  async function loadSkills(force = false) {
    if (catalog && !force) return catalog;
    if (!loading || force) {
      loading = fetch("/api/skills", { credentials: "same-origin" })
        .then((response) => {
          if (!response.ok) throw new Error(`Skill catalog failed (${response.status})`);
          return response.json();
        })
        .then((data) => (data.skills || []).map(normalizeSkill));
    }
    catalog = await loading;
    return catalog;
  }

  function clearCache() {
    catalog = null;
    loading = null;
  }

  function clearSelection() {
    selected = [];
    renderChips();
  }

  function triggerInfo(value, caret = value.length) {
    const before = value.slice(0, caret);
    const match = before.match(/(^|\s)(\S*)$/);
    if (!match) return null;
    const token = match[2];
    const start = before.length - token.length;
    if (!token.startsWith("/")) return null;

    const prefix = value.slice(0, start).trim();
    if (prefix && !prefix.split(/\s+/).every((item) => /^\/[^\s/]+$/.test(item))) {
      return null;
    }

    const after = value.slice(caret).match(/^\S*/)?.[0] || "";
    return {
      start,
      end: caret + after.length,
      query: token.slice(1),
    };
  }

  function selectedNames(value, skills = catalog || []) {
    const byLower = new Map(skills.map((skill) => [skill.name.toLowerCase(), skill.name]));
    const result = selected.slice();
    const seen = new Set(result);
    for (const token of value.trimStart().split(/\s+/)) {
      if (!token.startsWith("/") || token === "/") break;
      const name = byLower.get(token.slice(1).toLowerCase());
      if (!name) break;
      if (!seen.has(name)) {
        seen.add(name);
        result.push(name);
      }
    }
    return result;
  }

  function messageText(value) {
    const text = value.trim();
    if (/^\/commit(?=\s|$)/.test(text) || /^\/plan(?=\s|$)/.test(text)) return text;
    return [selected.map((name) => `/${name}`).join(" "), text].filter(Boolean).join(" ");
  }

  function filterSkills(skills, query) {
    const q = String(query || "").toLowerCase();
    if (!q) return skills.slice();
    return skills.filter((skill) =>
      skill.name.toLowerCase().includes(q)
      || skill.description.toLowerCase().includes(q)
    );
  }

  function replaceToken(value, info, name) {
    return `${value.slice(0, info.start)}/${name} ${value.slice(info.end).replace(/^\s*/, "")}`;
  }

  function removeToken(value, info) {
    return `${value.slice(0, info.start)}${value.slice(info.end).replace(/^\s*/, "")}`;
  }

  function renderChips() {
    if (!chipHost) return;
    chipHost.replaceChildren();
    selected.forEach((name) => {
      const chip = document.createElement("span");
      chip.className = "skill-chip";
      chip.textContent = `/${name}`;
      const button = document.createElement("button");
      button.type = "button";
      button.setAttribute("aria-label", `Remove ${name}`);
      button.textContent = "×";
      button.addEventListener("click", () => {
        selected = selected.filter((item) => item !== name);
        renderChips();
      });
      chip.append(button);
      chipHost.append(chip);
    });
    chipHost.hidden = selected.length === 0;
  }

  function attach({ input, host, chips }) {
    chipHost = chips || null;
    let open = false;
    let info = null;
    let skills = [];
    let matches = [];
    let active = 0;
    let status = "";

    host.setAttribute("role", "listbox");

    function close() {
      open = false;
      host.hidden = true;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-controls");
      input.removeAttribute("aria-activedescendant");
    }

    function render() {
      host.replaceChildren();
      if (!open) return close();

      const rows = status ? [] : matches;
      if (status) {
        const item = document.createElement("div");
        item.className = "skill-picker-status";
        item.textContent = status;
        host.append(item);
      }
      rows.forEach((skill, index) => {
        const item = document.createElement("button");
        item.type = "button";
        item.id = `skill-option-${index}`;
        item.className = `skill-picker-option${index === active ? " active" : ""}${skill.command ? " skill-picker-command" : ""}`;
        item.setAttribute("role", "option");
        item.setAttribute("aria-selected", index === active ? "true" : "false");
        item.innerHTML = '<span class="skill-picker-icon" aria-hidden="true"></span><strong></strong><em></em>';
        item.querySelector("strong").textContent = skill.command ? `/${skill.name}` : skill.name;
        item.querySelector("em").textContent = skill.description;
        item.addEventListener("mousedown", (event) => {
          event.preventDefault();
          choose(index);
        });
        host.append(item);
      });
      if (!status && rows.length === 0) {
        const item = document.createElement("div");
        item.className = "skill-picker-status";
        item.textContent = "No matching Skills";
        host.append(item);
      }
      host.hidden = false;
      input.setAttribute("aria-expanded", "true");
      input.setAttribute("aria-controls", host.id);
      if (rows[active]) input.setAttribute("aria-activedescendant", `skill-option-${active}`);
    }

    async function update() {
      info = triggerInfo(input.value, input.selectionStart || 0);
      if (!info) return close();
      open = true;
      status = "Loading Skills...";
      render();
      try {
        skills = await loadSkills();
        status = skills.length || commitVisible(info.query) || planVisible(info.query)
          ? ""
          : "No enabled Skills";
        matches = filterSkills(skills, info.query);
        if (planVisible(info.query)) matches = [PLAN_ENTRY, ...matches];
        if (commitVisible(info.query)) matches = [COMMIT_ENTRY, ...matches];
        active = Math.min(active, Math.max(0, matches.length - 1));
      } catch {
        status = "Could not load Skills";
        matches = [];
      }
      render();
    }

    function choose(index = active) {
      if (!info || !matches[index]) return;
      const row = matches[index];
      let caret = info.start;
      if (row.command) {
        const token = row.token || "commit";
        input.value = replaceToken(input.value, info, token);
        caret += `/${token} `.length;
      } else {
        const name = row.name;
        if (!selected.includes(name)) selected.push(name);
        input.value = removeToken(input.value, info);
      }
      input.dispatchEvent(new Event("input", { bubbles: true }));
      renderChips();
      close();
      input.focus();
      input.setSelectionRange(caret, caret);
    }

    input.addEventListener("input", update);
    input.addEventListener("click", update);
    input.addEventListener("keyup", (event) => {
      if (["ArrowUp", "ArrowDown", "Enter", "Tab", "Escape"].includes(event.key)) return;
      update();
    });
    input.addEventListener("keydown", (event) => {
      if (!open) return;
      if (event.key === "Escape") {
        event.preventDefault();
        close();
      } else if (event.key === "ArrowDown") {
        event.preventDefault();
        active = Math.min(active + 1, Math.max(0, matches.length - 1));
        render();
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        active = Math.max(0, active - 1);
        render();
      } else if ((event.key === "Enter" || event.key === "Tab") && matches.length) {
        event.preventDefault();
        choose();
      }
    }, true);
    document.addEventListener("mousedown", (event) => {
      if (event.target !== input && !host.contains(event.target)) close();
    });
  }

  const api = {
    attach,
    clearCache,
    clearSelection,
    commitVisible,
    filterSkills,
    loadSkills,
    messageText,
    planVisible,
    removeToken,
    replaceToken,
    selectedNames,
    triggerInfo,
  };

  if (typeof window !== "undefined") window.CaspianSkills = api;
  if (typeof globalThis !== "undefined") globalThis.CaspianSkills = api;
  if (typeof module !== "undefined") module.exports = api;
}());
