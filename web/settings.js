// 设置抽屉：从侧边列表进入，改角色卡 / 用户档案 / 说话风格。
// 依赖 index.html 里的 #menuBtn / #scrim / #drawer 等节点。

(function () {
  const menuBtn = document.getElementById("menuBtn");
  const scrimEl = document.getElementById("scrim");
  const drawerEl = document.getElementById("drawer");
  const drawerBody = document.getElementById("drawerBody");
  const drawerTitle = document.getElementById("drawerTitle");
  const drawerBack = document.getElementById("drawerBack");
  const drawerClose = document.getElementById("drawerClose");
  const titleEl = document.getElementById("title");

  if (!menuBtn || !drawerEl || !drawerBody) return;

  const SHORT_FIELDS = [
    ["name", "名字"],
    ["user_nickname", "她叫你"],
    ["stage", "关系阶段"],
    ["known_since", "认识那天（YYYY-MM-DD）"],
  ];

  const TEXT_FIELDS = [
    ["description", "她是谁（年龄、外貌、住哪、做什么）", 5],
    ["personality", "性格与说话方式", 5],
    ["scenario", "你们怎么认识的、现在什么关系", 3],
    ["system_prompt", "角色设定（system prompt）", 8],
    ["post_history_instructions", "每轮提醒（最后再说一遍的话）", 3],
    ["first_mes", "她说的第一句", 2],
    ["mes_example", "示例对话（<user> / <char>）", 6],
    ["creator_notes", "备注（不进 prompt，给自己看）", 2],
  ];

  // ---------------------------------------------------------------- 小工具

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else node.setAttribute(key, value);
    }
    for (const child of [].concat(children || [])) {
      if (child) node.appendChild(child);
    }
    return node;
  }

  function openDrawer() {
    drawerEl.classList.add("open");
    scrimEl.classList.add("open");
    drawerEl.setAttribute("aria-hidden", "false");
  }

  function closeDrawer() {
    drawerEl.classList.remove("open");
    scrimEl.classList.remove("open");
    drawerEl.setAttribute("aria-hidden", "true");
  }

  const statusEl = el("div", { class: "form-status" });

  function setStatus(text, isError) {
    statusEl.textContent = text;
    statusEl.className = "form-status" + (isError ? " err" : "");
  }

  // ---------------------------------------------------------------- 菜单

  function menuItem(title, hint, onClick) {
    const btn = el("button", { class: "menu-item", type: "button" });
    btn.appendChild(el("span", { text: title }));
    btn.appendChild(el("span", { class: "hint", text: hint }));
    btn.addEventListener("click", onClick);
    return btn;
  }

  async function showMenu() {
    drawerTitle.textContent = "设置";
    drawerBack.hidden = true;

    const moodLine = el("div", { class: "mood-line", text: "她此刻：…" });
    drawerBody.replaceChildren(
      moodLine,
      menuItem("角色卡", "她的名字、性格、说话方式、lorebook", showPersonaForm),
      menuItem("我的档案", "她本来就知道的、关于你的事", showProfileForm),
      menuItem("说话风格", "标准 / 更像人 / 更黏", showPacksForm)
    );

    try {
      const mood = await fetch("/api/mood").then((r) => r.json());
      moodLine.textContent = mood.level
        ? `她此刻：${MoodLabel(mood.level)}（${mood.note || "没特别的事"}）`
        : "她此刻：平常";
    } catch (err) {
      moodLine.textContent = "她此刻：读不到";
    }
  }

  function MoodLabel(level) {
    return { 2: "很亲近", 1: "心情不错", "-1": "有点不高兴", "-2": "在生气" }[level] || "平常";
  }

  // ---------------------------------------------------------------- 角色卡

  async function showPersonaForm() {
    drawerTitle.textContent = "角色卡";
    drawerBack.hidden = false;
    drawerBody.replaceChildren(el("div", { class: "form-status", text: "加载中…" }));

    let persona;
    try {
      persona = (await fetch("/api/persona").then((r) => r.json())).persona;
    } catch (err) {
      drawerBody.replaceChildren(el("div", { class: "form-status err", text: "读不到角色卡" }));
      return;
    }

    const inputs = {};
    const form = el("div");

    for (const [key, label] of SHORT_FIELDS) {
      const input = el("input", { type: "text", value: persona[key] || "" });
      inputs[key] = input;
      form.appendChild(el("div", { class: "form-group" }, [el("label", { text: label }), input]));
    }

    for (const [key, label, rows] of TEXT_FIELDS) {
      const area = el("textarea", { rows: String(rows) });
      area.value = persona[key] || "";
      inputs[key] = area;
      form.appendChild(el("div", { class: "form-group" }, [el("label", { text: label }), area]));
    }

    const greetings = el("textarea", { rows: "3" });
    greetings.value = (persona.alternate_greetings || []).join("\n");
    form.appendChild(
      el("div", { class: "form-group" }, [
        el("label", { text: "备选开场白（一行一条）" }),
        greetings,
      ])
    );

    // lorebook：关键词 → 补充设定
    const entries = (persona.lorebook || []).map((entry) => ({
      keys: (entry.keys || []).slice(),
      content: entry.content || "",
      constant: !!entry.constant,
    }));
    const loreWrap = el("div");

    function renderLore() {
      loreWrap.replaceChildren();
      entries.forEach((entry, index) => {
        const keysInput = el("input", {
          type: "text",
          placeholder: "关键词，用逗号分隔",
          value: entry.keys.join(", "),
        });
        keysInput.addEventListener("input", () => {
          entry.keys = keysInput.value
            .split(/[,，]/)
            .map((part) => part.trim())
            .filter(Boolean);
        });

        const contentArea = el("textarea", { rows: "2", placeholder: "触发时补进 prompt 的设定" });
        contentArea.value = entry.content;
        contentArea.addEventListener("input", () => {
          entry.content = contentArea.value;
        });

        const constantBox = el("input", { type: "checkbox" });
        constantBox.checked = entry.constant;
        constantBox.addEventListener("change", () => {
          entry.constant = constantBox.checked;
        });

        const removeBtn = el("button", { class: "btn btn-danger", type: "button", text: "删除" });
        removeBtn.addEventListener("click", () => {
          entries.splice(index, 1);
          renderLore();
        });

        const constantLabel = el("label");
        constantLabel.appendChild(constantBox);
        constantLabel.appendChild(document.createTextNode(" 常驻（每轮都注入）"));

        loreWrap.appendChild(
          el("div", { class: "lore-card" }, [
            el("div", { class: "lore-head" }, [keysInput]),
            contentArea,
            el("div", { class: "lore-foot" }, [constantLabel, removeBtn]),
          ])
        );
      });

      const addBtn = el("button", { class: "btn btn-ghost", type: "button", text: "＋ 添加词条" });
      addBtn.addEventListener("click", () => {
        entries.push({ keys: [], content: "", constant: false });
        renderLore();
      });
      loreWrap.appendChild(addBtn);
    }

    renderLore();
    form.appendChild(
      el("div", { class: "form-group" }, [el("label", { text: "lorebook（关键词触发）" }), loreWrap])
    );

    const saveBtn = el("button", { class: "btn btn-primary", type: "button", text: "保存" });
    saveBtn.addEventListener("click", async () => {
      const payload = { lorebook: entries };
      for (const [key] of SHORT_FIELDS.concat(TEXT_FIELDS)) payload[key] = inputs[key].value;
      payload.alternate_greetings = greetings.value
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean);

      saveBtn.disabled = true;
      setStatus("保存中…", false);
      try {
        const res = await fetch("/api/persona", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        }).then((r) => r.json());

        if (res.ok) {
          setStatus("已保存，下一句生效", false);
          if (res.name) {
            titleEl.textContent = res.name;
            document.title = res.name;
            window.dispatchEvent(
              new CustomEvent("persona-changed", { detail: { name: res.name } })
            );
          }
        } else {
          setStatus((res.errors || ["保存失败"]).join("；"), true);
        }
      } catch (err) {
        setStatus("网络错误，没保存上", true);
      } finally {
        saveBtn.disabled = false;
      }
    });

    drawerBody.replaceChildren(form, saveBtn, statusEl);
    setStatus("改了直接保存，不用重启", false);
  }

  // ---------------------------------------------------------------- 用户档案

  async function showProfileForm() {
    drawerTitle.textContent = "我的档案";
    drawerBack.hidden = false;
    drawerBody.replaceChildren(el("div", { class: "form-status", text: "加载中…" }));

    let data;
    try {
      data = await fetch("/api/profile").then((r) => r.json());
    } catch (err) {
      drawerBody.replaceChildren(el("div", { class: "form-status err", text: "读不到档案" }));
      return;
    }

    const area = el("textarea", { rows: "16" });
    area.value = (data.facts || []).join("\n");

    const saveBtn = el("button", { class: "btn btn-primary", type: "button", text: "保存" });
    saveBtn.addEventListener("click", async () => {
      const facts = area.value
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean);
      saveBtn.disabled = true;
      setStatus("保存中…", false);
      try {
        const res = await fetch("/api/profile", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ facts }),
        }).then((r) => r.json());
        if (res.ok) setStatus(`已保存 ${res.count} 条`, false);
        else setStatus((res.errors || ["保存失败"]).join("；"), true);
      } catch (err) {
        setStatus("网络错误，没保存上", true);
      } finally {
        saveBtn.disabled = false;
      }
    });

    drawerBody.replaceChildren(
      el("div", { class: "form-group" }, [
        el("label", { text: "一行一条。她默认就知道这些。" }),
        area,
      ]),
      saveBtn,
      statusEl
    );
    setStatus("", false);
  }

  // ---------------------------------------------------------------- 说话风格

  async function showPacksForm() {
    drawerTitle.textContent = "说话风格";
    drawerBack.hidden = false;
    drawerBody.replaceChildren(el("div", { class: "form-status", text: "加载中…" }));

    let data;
    try {
      data = await fetch("/api/prompts").then((r) => r.json());
    } catch (err) {
      drawerBody.replaceChildren(el("div", { class: "form-status err", text: "读不到风格包" }));
      return;
    }

    let selected = data.active;

    const form = el("div");
    for (const pack of data.packs) {
      const radio = el("input", { type: "radio", name: "pack", value: pack.id });
      radio.checked = pack.id === data.active;
      radio.addEventListener("change", () => {
        selected = pack.id;
      });

      const card = el("label", { class: "pack-card" }, [
        el("div", { class: "pack-head" }, [radio, el("span", { class: "pack-title", text: pack.label })]),
        el("div", { class: "pack-desc", text: pack.description }),
      ]);
      for (const block of pack.blocks) {
        card.appendChild(el("div", { class: "pack-block", text: `· ${block.text}` }));
      }
      form.appendChild(card);
    }

    const saveBtn = el("button", { class: "btn btn-primary", type: "button", text: "用这个风格" });
    saveBtn.addEventListener("click", async () => {
      saveBtn.disabled = true;
      setStatus("保存中…", false);
      try {
        const res = await fetch("/api/prompts", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ active: selected }),
        }).then((r) => r.json());
        if (res.ok) setStatus("换好了，下一句生效", false);
        else setStatus((res.errors || ["保存失败"]).join("；"), true);
      } catch (err) {
        setStatus("网络错误，没保存上", true);
      } finally {
        saveBtn.disabled = false;
      }
    });

    drawerBody.replaceChildren(form, saveBtn, statusEl);
    setStatus("风格只影响语气，不改人设", false);
  }

  // ---------------------------------------------------------------- 绑定

  menuBtn.addEventListener("click", () => {
    showMenu();
    openDrawer();
  });
  drawerClose.addEventListener("click", closeDrawer);
  scrimEl.addEventListener("click", closeDrawer);
  drawerBack.addEventListener("click", showMenu);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeDrawer();
  });
})();
