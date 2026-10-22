// 微信风格聊天界面：拉历史、发消息、听 SSE 推送。

const listEl = document.getElementById("list");
const formEl = document.getElementById("form");
const inputEl = document.getElementById("input");
const sendEl = document.getElementById("send");
const titleEl = document.getElementById("title");
const subEl = document.getElementById("sub");

let herName = "她";
let lastTs = 0;
let lastRole = null;
let lastMsgTs = 0;
let typingRow = null;
let sending = false;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function fmtTime(ts) {
  const d = new Date(ts);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function addTime(ts) {
  const div = document.createElement("div");
  div.className = "time";
  div.textContent = fmtTime(ts);
  listEl.appendChild(div);
}

function makeRow(role, text, ts) {
  if (ts && ts - lastTs > 5 * 60 * 1000) {
    addTime(ts);
    lastTs = ts;
  } else if (!lastTs) {
    lastTs = ts || Date.now();
  }

  const row = document.createElement("div");
  row.className = "row" + (role === "me" ? " me" : "");

  // 连续同一人、间隔一分钟内，不重复显示头像
  const continued = role === lastRole && ts && ts - lastMsgTs < 60 * 1000;
  if (continued) row.classList.add("cont");

  const avatar = document.createElement("div");
  avatar.className = "avatar";
  avatar.textContent = role === "me" ? "我" : herName.slice(0, 1);

  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text;

  row.appendChild(avatar);
  row.appendChild(bubble);
  listEl.appendChild(row);

  lastRole = role;
  lastMsgTs = ts || Date.now();
}

function scrollToBottom() {
  listEl.scrollTop = listEl.scrollHeight;
}

function showTyping() {
  if (typingRow) return;
  typingRow = document.createElement("div");
  typingRow.className = "row";

  const avatar = document.createElement("div");
  avatar.className = "avatar";
  avatar.textContent = herName.slice(0, 1);

  const bubble = document.createElement("div");
  bubble.className = "typing-bubble";
  bubble.appendChild(document.createElement("i"));
  bubble.appendChild(document.createElement("i"));
  bubble.appendChild(document.createElement("i"));

  typingRow.appendChild(avatar);
  typingRow.appendChild(bubble);
  listEl.appendChild(typingRow);
  scrollToBottom();
}

function hideTyping() {
  if (!typingRow) return;
  typingRow.remove();
  typingRow = null;
}

/** 按服务端给的延迟逐条播放，模拟打字。 */
async function playBubbles(bubbles) {
  for (const item of bubbles) {
    showTyping();
    await sleep(Math.max(300, item.delay_ms || 800));
    hideTyping();
    makeRow("her", item.text, Date.now());
    scrollToBottom();
  }
}

async function loadHistory() {
  const res = await fetch("/api/history?limit=50");
  const data = await res.json();
  for (const m of data.messages) {
    const ts = Date.parse(m.created_at);
    if (m.role === "user") {
      makeRow("me", m.content, ts);
    } else if (m.role === "assistant") {
      // 存储时把一轮的多条气泡用换行拼成了一条，这里拆回去
      for (const part of m.content.split("\n")) {
        if (part.trim()) makeRow("her", part, ts);
      }
    }
  }
  scrollToBottom();
}

function connectEvents() {
  const es = new EventSource("/api/events");

  es.addEventListener("bubble", (e) => {
    const data = JSON.parse(e.data);
    playBubbles(data.bubbles || []);
  });

  es.onopen = () => {
    subEl.textContent = "在线";
    subEl.classList.remove("off");
  };
  es.onerror = () => {
    subEl.textContent = "重连中…";
    subEl.classList.add("off");
  };
}

async function boot() {
  try {
    const state = await fetch("/api/state").then((r) => r.json());
    herName = state.name || "她";
    titleEl.textContent = herName;
    document.title = herName;
  } catch (e) {
    titleEl.textContent = "连不上";
  }

  await loadHistory();
  connectEvents();
  inputEl.focus();
}

formEl.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = inputEl.value.trim();
  if (!text || sending) return;

  sending = true;
  sendEl.disabled = true;
  inputEl.value = "";
  makeRow("me", text, Date.now());
  scrollToBottom();
  showTyping();

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    }).then((r) => r.json());

    hideTyping();
    if (res.ok) {
      await playBubbles(res.bubbles || []);
    } else {
      makeRow("her", "……", Date.now());
    }
  } catch (err) {
    hideTyping();
    makeRow("her", "……", Date.now());
  } finally {
    sending = false;
    sendEl.disabled = false;
    inputEl.focus();
  }
});

boot();
