/* Zax UI — vanilla JS, no build step (Odysseus-style). */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

const api = {
  async get(path) {
    const r = await fetch(`/api${path}`);
    if (!r.ok) throw new Error(`${r.status}`);
    return r.json();
  },
  async post(path, body) {
    const r = await fetch(`/api${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    if (!r.ok) throw new Error(`${r.status}`);
    return r.json();
  },
  async del(path) {
    await fetch(`/api${path}`, { method: "DELETE" });
  },
};

function esc(raw) {
  return raw
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/* ============================== markdown + code highlighting */

function highlightCode(code, lang) {
  const keywords = {
    python: /\b(def|class|if|elif|else|for|while|return|import|from|as|try|except|finally|with|yield|lambda|async|await|raise|pass|break|continue|and|or|not|in|is|None|True|False|self|print)\b/g,
    javascript: /\b(const|let|var|function|return|if|else|for|while|class|import|export|from|async|await|try|catch|finally|throw|new|this|true|false|null|undefined)\b/g,
    typescript: /\b(const|let|var|function|return|if|else|for|while|class|interface|type|import|export|from|async|await|try|catch|finally|throw|new|this|true|false|null|undefined)\b/g,
    json: /".*?":|true|false|null|\b\d+\b/g,
    html: /&lt;\/?[\w-]+|&lt;![\w\s]*&gt;/g,
    css: /[.#][\w-]+\s*\{|\b(display|color|background|margin|padding|border|width|height|font|position|flex|grid|content|var)\b/g,
    bash: /\b(echo|cd|ls|cat|grep|sed|awk|curl|python|node|npm|git|docker|sudo|export|source|if|then|else|fi|for|do|done)\b/g,
  };
  const k = lang && keywords[lang.toLowerCase()] ? lang.toLowerCase() : "javascript";
  const pat = keywords[k] || keywords.javascript;
  const comm = k === "python" ? /#.*/g : k === "html" ? /&lt;!--.*?--&gt;/g : /(\/\/.*|\/\*[\s\S]*?\*\/)/g;
  const str = /("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`)/g;
  const num = /\b\d+(?:\.\d+)?\b/g;
  let out = esc(code);
  const spans = [];
  function mark(re, cls) {
    out = out.replace(re, (m) => { spans.push([m, cls]); return `\x00_SPAN_${spans.length - 1}_\x00`; });
  }
  mark(str, "hl-str");
  mark(comm, "hl-comm");
  mark(pat, "hl-kw");
  mark(num, "hl-num");
  out = out.replace(/\x00_SPAN_(\d+)_\x00/g, (_, i) => {
    const [text, cls] = spans[+i];
    return `<span class="${cls}">${esc(text)}</span>`;
  });
  return out;
}

function renderInline(text) {
  return text
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_]+)__/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
    .replace(/_([^_]+)_/g, "<em>$1</em>")
    .replace(/~~([^~]+)~~/g, "<del>$1</del>")
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
}

function renderMarkdown(text) {
  // fenced code blocks (handle first, globally)
  const codeBlocks = [];
  text = text.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    codeBlocks.push(`<pre class="code-block"><code>${highlightCode(code.replace(/\n$/, ""), lang)}</code></pre>`);
    return `\x00CODE${codeBlocks.length - 1}\x00`;
  });

  const blocks = text.split(/\n\n+/);
  const html = blocks.map((block) => {
    block = block.trim();
    if (!block) return "";
    // code placeholder
    if (/^\x00CODE\d+\x00$/.test(block)) {
      return codeBlocks[+block.slice(5, -1)];
    }
    // headers
    const h = block.match(/^(#{1,4})\s+(.*)$/);
    if (h) return `<h${h[1].length}>${renderInline(esc(h[2]))}</h${h[1].length}>`;
    // blockquote
    if (/^>\s?/.test(block)) {
      return `<blockquote>${renderInline(esc(block.replace(/^>\s?/gm, "")))}</blockquote>`;
    }
    // unordered list
    if (/^[\-*]\s+/.test(block)) {
      const items = block.split("\n").filter((l) => l.trim()).map((l) => `<li>${renderInline(esc(l.replace(/^[\-*]\s+/, "")))}</li>`).join("");
      return `<ul>${items}</ul>`;
    }
    // ordered list
    if (/^\d+\.\s+/.test(block)) {
      const items = block.split("\n").filter((l) => l.trim()).map((l) => `<li>${renderInline(esc(l.replace(/^\d+\.\s+/, "")))}</li>`).join("");
      return `<ol>${items}</ol>`;
    }
    // paragraph (preserve single line breaks)
    const inner = esc(block).replace(/\n/g, "<br>");
    return `<p>${renderInline(inner)}</p>`;
  });

  return html.join("");
}

/* ============================== voice: the Zax deep voice */

const ZaxVoice = {
  enabled: false,   // text by default — Zax only speaks when asked
  speaking: false,
  current: null,    // the currently-playing <audio>, so we never overlap
  _token: 0,

  // Stop any in-progress speech. Called before every new utterance so two
  // messages can never play at the same time.
  stop() {
    this._token++;
    if (this.current) {
      try { this.current.pause(); this.current.src = ""; } catch {}
      this.current = null;
    }
    if (window.speechSynthesis) speechSynthesis.cancel();
    this.speaking = false;
    $("#orb")?.classList.remove("speaking");
  },

  // force=true speaks even when auto-voice is off (the per-message 🔊 button,
  // and replies to a spoken question).
  async speak(text, { force = false } = {}) {
    if ((!this.enabled && !force) || !text) return;
    this.stop();
    const token = this._token;
    const clean = text.replace(/[*_`#>]/g, "").replace(/[✓✦◈⧉⊚⛬⌬]/g, "").slice(0, 1200);
    $("#orb")?.classList.add("speaking");
    this.speaking = true;
    try {
      await this.serverSpeak(clean, token);
    } catch {
      if (token === this._token) await this.browserSpeak(clean, token);
    } finally {
      if (token === this._token) {
        this.speaking = false;
        $("#orb")?.classList.remove("speaking");
      }
    }
  },

  serverSpeak(text, token) {
    return new Promise(async (resolve, reject) => {
      try {
        const r = await fetch("/api/voice/speak", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text }),
        });
        if (!r.ok) return reject(new Error("server tts unavailable"));
        if (token !== this._token) return resolve();   // superseded while fetching
        const blob = await r.blob();
        const audio = new Audio(URL.createObjectURL(blob));
        this.current = audio;
        audio.onended = () => resolve();
        audio.onerror = () => reject(new Error("playback failed"));
        await audio.play();
      } catch (e) {
        reject(e);
      }
    });
  },

  browserSpeak(text, token) {
    return new Promise((resolve) => {
      if (!window.speechSynthesis || token !== this._token) return resolve();
      const u = new SpeechSynthesisUtterance(text);
      u.pitch = 0.9;   // near-natural; heavy pitch-down sounds robotic
      u.rate = 1.0;
      const voices = speechSynthesis.getVoices();
      u.voice =
        voices.find((v) => /Daniel|Google UK English Male|Arthur|Oliver/i.test(v.name)) ||
        voices.find((v) => v.lang.startsWith("en-GB")) ||
        voices.find((v) => v.lang.startsWith("en")) ||
        null;
      u.onend = () => resolve();
      u.onerror = () => resolve();
      speechSynthesis.speak(u);
    });
  },

  setEnabled(on) {
    this.enabled = on;
    if (!on) this.stop();
    const btn = $("#voice-toggle");
    if (btn) { btn.textContent = on ? "🔊" : "🔇"; btn.classList.toggle("on", on); }
    const cb = $("#voice-enabled");
    if (cb) cb.checked = on;
  },
};

/* ============================== speech-to-text (talk to Zax) */

function setupMic() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const btn = $("#mic-btn");
  if (!SR) {
    btn.style.display = "none";
    return;
  }
  let rec = null;
  btn.addEventListener("click", () => {
    if (rec) {
      rec.stop();
      return;
    }
    rec = new SR();
    rec.lang = "en-US";
    rec.interimResults = false;
    btn.classList.add("live");
    $("#orb").classList.add("listening");
    rec.onresult = (e) => {
      const text = e.results[0][0].transcript;
      $("#chat-input").value = text;
      pendingVoiceReply = true;  // you spoke → Zax answers aloud
      sendChat();
    };
    rec.onend = () => {
      btn.classList.remove("live");
      $("#orb").classList.remove("listening");
      rec = null;
    };
    rec.start();
  });
}

/* ============================== bridge chat */

function addMsg(role, content, ts) {
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  const who = document.createElement("span");
  who.className = "who";
  who.textContent = role === "zax" ? "ZAX · CEO" : "FOUNDER";
  const time = document.createElement("span");
  time.className = "msg-time";
  time.textContent = ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
  const body = document.createElement("span");
  body.className = "msg-body";
  body.innerHTML = renderMarkdown(content);
  const meta = document.createElement("div");
  meta.className = "msg-meta";
  meta.append(who, time);
  div.append(meta, body);
  // Per-message speaker — play THIS reply aloud on demand (works even with
  // auto-voice off). This is the "speak only when asked" path.
  if (role === "zax") {
    const play = document.createElement("button");
    play.className = "msg-speak";
    play.textContent = "🔊";
    play.title = "Play aloud";
    play.onclick = () => ZaxVoice.speak(content, { force: true });
    div.appendChild(play);
  }
  const indicator = $("#typing-indicator");
  if (indicator) {
    $("#chat-log").insertBefore(div, indicator);
  } else {
    $("#chat-log").appendChild(div);
  }
  $("#chat-log").scrollTop = $("#chat-log").scrollHeight;
}

/* ============================== chat sessions (multiple conversations) */

let activeSession = "main";

// Render a delegated task as a LIVE card in the chat log and poll it to completion,
// so the Founder watches the work happen where they delegated it (not on another tab).
function trackTaskInChat(taskId, title) {
  const card = document.createElement("div");
  card.className = "chat-task";
  card.innerHTML = `
    <div class="ct-head"><span class="ct-icon">⚙</span><span class="ct-title">${esc(title)}</span></div>
    <div class="ct-status">queued…</div>
    <div class="ct-bar"><div class="ct-fill" style="width:0%"></div></div>`;
  const log = $("#chat-log");
  const ti = $("#typing-indicator");
  ti ? log.insertBefore(card, ti) : log.appendChild(card);
  log.scrollTop = log.scrollHeight;

  const fill = card.querySelector(".ct-fill");
  const statusEl = card.querySelector(".ct-status");
  const icon = card.querySelector(".ct-icon");
  let ticks = 0;
  const poll = setInterval(async () => {
    ticks++;
    let t;
    try { t = await api.get(`/tasks/${taskId}`); } catch { return; }
    fill.style.width = (t.progress || 0) + "%";
    const who = t.agent_name ? ` · ${t.agent_name}` : "";
    if (t.status === "assigned") statusEl.textContent = `assigned${who} — starting…`;
    else if (t.status === "in_progress") statusEl.textContent = `working${who}… ${t.progress}%`;
    else if (t.status === "done" || t.status === "failed") {
      clearInterval(poll);
      card.classList.add(t.status === "done" ? "ct-done" : "ct-failed");
      icon.textContent = t.status === "done" ? "✓" : "✕";
      const sc = t.score != null ? ` · scored ${t.score}/100` : "";
      statusEl.innerHTML = `${t.status === "done" ? "delivered" : "failed"}${who}${sc} ` +
        `<button class="ct-view">view result</button>`;
      fill.style.width = "100%";
      const view = card.querySelector(".ct-view");
      const body = document.createElement("div");
      body.className = "ct-result";
      body.textContent = t.result || "(no result)";
      body.style.display = "none";
      card.appendChild(body);
      view.onclick = () => {
        body.style.display = body.style.display === "none" ? "block" : "none";
        log.scrollTop = log.scrollHeight;
      };
    }
    if (ticks > 90) clearInterval(poll);  // safety stop (~3 min)
  }, 2000);
}

let pendingVoiceReply = false;  // set when you SPOKE to Zax → he replies aloud once

async function sendToZax(text) {
  text = text.trim();
  if (!text) return;
  const spokeToHim = pendingVoiceReply;
  pendingVoiceReply = false;
  addMsg("founder", text);
  $("#zax-state").textContent = "ZAX · THINKING…";
  const ti = $("#typing-indicator");
  if (ti) ti.classList.remove("hidden");
  try {
    const res = await api.post("/chat", { message: text, session_id: activeSession });
    if (ti) ti.classList.add("hidden");
    addMsg("zax", res.reply);
    if (res.graph_context_used) $("#ticker").textContent = "↺ answered from memory graph — full history not replayed";
    // Speak only if auto-voice is on, or you just spoke to him (voice→voice).
    ZaxVoice.speak(res.reply, { force: spokeToHim });
    refreshAll();
    renderSessions();  // title/order may have changed
    if (res.actions && res.actions.length) {
      burstRefreshTasks();  // Zax kicked off work — watch it run
      // Show each delegated task working LIVE, right here in the chat.
      res.actions.filter((a) => a.type === "create_task" && a.task_id)
        .forEach((a) => trackTaskInChat(a.task_id, a.title));
    }
  } catch (e) {
    if (ti) ti.classList.add("hidden");
    addMsg("zax", `(comms error: ${e.message})`);
  } finally {
    $("#zax-state").textContent = "ZAX · ONLINE";
  }
}

async function renderSessions() {
  const sessions = await api.get("/sessions");
  const list = $("#sessions-list");
  list.innerHTML = "";
  sessions.forEach((s) => {
    const row = document.createElement("div");
    row.className = `session-row ${s.id === activeSession ? "active" : ""}`;
    const title = document.createElement("span");
    title.className = "session-title";
    title.textContent = s.title || "New chat";
    title.title = s.title;
    title.onclick = () => switchSession(s.id);
    row.appendChild(title);
    if (s.id !== "main") {
      const del = document.createElement("button");
      del.className = "session-del";
      del.textContent = "✕";
      del.title = "delete chat";
      del.onclick = async (e) => {
        e.stopPropagation();
        if (!confirm(`Delete chat "${s.title}"?`)) return;
        await api.del(`/sessions/${s.id}`);
        if (activeSession === s.id) await switchSession("main");
        else renderSessions();
      };
      row.appendChild(del);
    }
    list.appendChild(row);
  });
  const active = sessions.find((s) => s.id === activeSession);
  $("#active-session-name").textContent = active ? active.title : "";
}

async function switchSession(id) {
  activeSession = id;
  await loadHistory(id);
  renderSessions();
  // jump to the Bridge so the conversation is visible
  document.querySelector('.nav-btn[data-panel="bridge"]').click();
}

async function newChat() {
  const s = await api.post("/sessions", {});
  activeSession = s.id;
  await loadHistory(s.id);
  renderSessions();
  document.querySelector('.nav-btn[data-panel="bridge"]').click();
  $("#chat-input").focus();
}

// Work now executes immediately server-side; refresh the board a few times over
// the next ~15s so the founder watches it move assigned → in progress → done.
let _burstTimers = [];
function burstRefreshTasks() {
  _burstTimers.forEach(clearTimeout);
  _burstTimers = [1000, 2500, 5000, 8000, 12000, 16000].map((ms) =>
    setTimeout(() => {
      // A drain can hire/fire/throttle, so refresh whichever org-ish panel is open.
      if ($("#panel-tasks").classList.contains("active")) renderTasks();
      if ($("#panel-org").classList.contains("active")) renderOrg();
      if ($("#panel-ops").classList.contains("active")) renderOps();
    }, ms)
  );
}

function sendChat() {
  const input = $("#chat-input");
  const text = input.value;
  input.value = "";
  sendToZax(text);
}

async function loadHistory(session = activeSession) {
  const msgs = await api.get(`/messages?session=${encodeURIComponent(session)}`);
  // Preserve typing indicator element while clearing messages
  const log = $("#chat-log");
  const ti = $("#typing-indicator");
  log.innerHTML = "";
  if (ti) log.appendChild(ti);  // re-add typing indicator
  msgs.forEach((m) => addMsg(m.role, m.content, m.ts));
  if (!msgs.length) {
    const hint = document.createElement("div");
    hint.className = "chat-empty";
    hint.textContent = "New conversation — speak to Zax, Founder.";
    log.insertBefore(hint, ti || null);
  }
}

/* ============================== org chart */

function perfClass(p) {
  return p >= 70 ? "" : p >= 50 ? "warn" : "bad";
}

async function renderOrg() {
  const agents = await api.get("/agents");
  const active = agents.filter((a) => a.status !== "fired");
  const fired = agents.filter((a) => a.status === "fired");

  $("#agents-grid").innerHTML = "";
  active.forEach((a) => {
    const card = document.createElement("div");
    card.className = `agent-card ${a.status}`;
    card.innerHTML = `
      <div class="agent-name">${esc(a.name)}</div>
      <div class="agent-title">${esc(a.title)}${a.status === "throttled" ? " · ⚠ BUDGET THROTTLED" : ""}</div>
      <div class="agent-meta">${esc(a.role)}</div>
      <div class="perf-bar"><div class="perf-fill ${perfClass(a.performance)}" style="width:${a.performance}%"></div></div>
      <div class="agent-meta">
        perf ${a.performance.toFixed(0)}% · done ${a.tasks_done} · failed ${a.tasks_failed}<br>
        tokens ${a.tokens_used.toLocaleString()} / ${a.token_budget.toLocaleString()}
      </div>`;
    const fireBtn = document.createElement("button");
    fireBtn.className = "fire-btn";
    fireBtn.textContent = "FIRE";
    fireBtn.onclick = async () => {
      if (!confirm(`Terminate ${a.name}?`)) return;
      await api.post(`/agents/${a.id}/fire`, { reason: "Founder's directive" });
      refreshAll();
    };
    card.appendChild(fireBtn);
    $("#agents-grid").appendChild(card);
  });

  $("#fired-grid").innerHTML = "";
  fired.forEach((a) => {
    const card = document.createElement("div");
    card.className = "agent-card fired";
    card.innerHTML = `
      <div class="agent-name">${esc(a.name)}</div>
      <div class="agent-title">${esc(a.title)}</div>
      <div class="fired-reason">✝ ${esc(a.fired_reason || "terminated")}</div>`;
    $("#fired-grid").appendChild(card);
  });
}

/* ============================== tasks */

const COLS = [
  ["inbox", "INBOX"],
  ["assigned", "ASSIGNED / IN PROGRESS"],
  ["done", "DONE"],
  ["failed", "FAILED"],
];

async function renderTasks() {
  const [tasks, agents] = await Promise.all([api.get("/tasks"), api.get("/agents")]);
  const names = Object.fromEntries(agents.map((a) => [a.id, a.name]));
  const board = $("#task-board");
  board.innerHTML = "";
  COLS.forEach(([key, label]) => {
    const col = document.createElement("div");
    col.className = "task-col";
    col.innerHTML = `<h3>${label}</h3>`;
    const items = tasks.filter((t) =>
      key === "assigned" ? ["assigned", "in_progress"].includes(t.status) : t.status === key
    );
    items.forEach((t) => {
      const card = document.createElement("div");
      card.className = `task-card p${t.priority} ${t.status === "done" ? "done-card" : ""} ${t.status === "failed" ? "failed-card" : ""}`;
      const scoreCls = t.score == null ? "" : t.score >= 70 ? "" : t.score >= 50 ? "low" : "bad";
      const pct = t.progress ?? 0;
      const running = t.status === "in_progress";
      const active = t.status === "assigned" || running;
      const pctCls = t.status === "failed" ? "bad" : t.status === "done" ? "done" : "";
      card.innerHTML = `
        <div class="t-title">${esc(t.title)}</div>
        <div class="t-meta">
          ${t.agent_id ? "→ " + esc(names[t.agent_id] || "?") : "unassigned"}
          ${running ? ' · <span class="t-running">working…</span>' : ""}
          ${t.score != null ? ` · <span class="score-chip ${scoreCls}">${t.score}/100</span>` : ""}
        </div>
        <div class="t-progress">
          <div class="t-prog-bar"><div class="t-prog-fill ${pctCls} ${active ? "live" : ""}" style="width:${pct}%"></div></div>
          <span class="t-prog-pct">${pct}%</span>
        </div>
        ${t.result ? `<div class="task-result">${esc(t.feedback ? "ZAX: " + t.feedback + "\n\n" : "")}${esc(t.result)}</div>` : ""}`;
      card.onclick = () => card.classList.toggle("open");
      col.appendChild(card);
    });
    board.appendChild(col);
  });
}

/* ============================== ops */

async function renderOps() {
  const status = await api.get("/status");
  const t = status.tasks || {};
  const stats = [
    [status.headcount, "AGENTS ON STAFF"],
    [`${status.avg_performance}%`, "ORG PERFORMANCE"],
    [(t.inbox || 0) + (t.assigned || 0) + (t.in_progress || 0), "TASKS IN FLIGHT"],
    [t.done || 0, "TASKS DELIVERED"],
    [status.tokens_spent_month.toLocaleString(), "TOKENS THIS MONTH"],
  ];
  $("#stat-cards").innerHTML = stats
    .map(([v, l]) => `<div class="stat-card"><div class="stat-val">${v}</div><div class="stat-label">${l}</div></div>`)
    .join("");

  const routines = await api.get("/routines");
  $("#routines-list").innerHTML = "";
  routines.forEach((r) => {
    const row = document.createElement("div");
    row.className = "routine-row";
    row.innerHTML = `<span>${esc(r.name)}<br><span class="r-meta">every ${r.interval_minutes} min</span></span>`;
    const del = document.createElement("button");
    del.textContent = "✕";
    del.onclick = async () => {
      await api.del(`/routines/${r.id}`);
      renderOps();
    };
    row.appendChild(del);
    $("#routines-list").appendChild(row);
  });

  const events = await api.get("/feed?after=0");
  $("#audit-log").innerHTML = events
    .map(
      (e) => `<div class="audit-row">
        <span class="a-ts">${new Date(e.ts * 1000).toLocaleTimeString()}</span>
        <span class="a-kind k-${esc(e.kind)}">${esc(e.kind.toUpperCase())}</span>
        ${esc(e.message)}</div>`
    )
    .join("");
}

/* ============================== settings */

async function renderSettings() {
  const s = await api.get("/status");
  $("#settings-info").innerHTML = `
    <div class="kv">founder: <b>${esc(s.founder)}</b></div>
    <div class="kv">intelligence core: <b>${esc(s.provider)}</b> · model <b>${esc(s.model || "default")}</b></div>
    <div class="kv">server tts (zax voice): <b>${s.voice_server_tts ? "online" : "offline — using browser fallback"}</b></div>
    <div class="kv">heartbeat: <b>every ${s.heartbeat_seconds}s</b></div>`;
  renderProviders();
  renderTelegram();
  renderVoiceSettings();
}

async function renderVoiceSettings() {
  let c;
  try { c = await api.get("/voice/config"); } catch { return; }
  const prov = $("#voice-provider");
  prov.value = c.provider;
  $("#voice-edge-row").classList.toggle("hidden", c.provider !== "edge");
  $("#voice-eleven-rows").classList.toggle("hidden", c.provider !== "elevenlabs");
  $("#voice-edge-select").innerHTML = c.edge_voices
    .map((v) => `<option value="${esc(v.id)}" ${v.id === c.edge_voice ? "selected" : ""}>${esc(v.label)}</option>`).join("");
  $("#voice-eleven-select").innerHTML = c.eleven_presets
    .map((v) => `<option value="${esc(v.id)}" ${v.id === c.eleven_voice ? "selected" : ""}>${esc(v.label)}</option>`).join("");
  $("#voice-status").textContent = c.provider === "elevenlabs"
    ? (c.eleven_connected ? "✓ ElevenLabs connected" : "⚠ add an API key to use ElevenLabs")
    : (c.edge_available ? "edge-tts ready" : "edge-tts unavailable — browser fallback");
}

async function saveVoice(patch) {
  try {
    await api.post("/voice/config", patch);
    renderVoiceSettings();
  } catch (e) {
    $("#voice-status").textContent = `✗ ${e.message}`;
  }
}

async function renderTelegram() {
  const t = await api.get("/telegram");
  const status = $("#telegram-status");
  if (t.connected && t.chat_id) {
    status.innerHTML = `✅ connected as <b>@${esc(t.username || "bot")}</b> — linked to your chat`;
  } else if (t.has_token) {
    status.innerHTML = `🟡 bot <b>@${esc(t.username || "")}</b> connected — now open it in Telegram and send <code>/start</code>`;
  } else {
    status.innerHTML = "⚪ not connected";
  }
  $("#telegram-notify").checked = t.notify;
  $("#telegram-disconnect").style.display = t.has_token ? "" : "none";
}

/* ============================== cortex: memory + self-learning */

const KIND_LABELS = { skill: "SKILL", lesson: "LESSON", report: "REPORT", note: "NOTE", org: "ORG" };

async function renderCortex() {
  const s = await api.get("/learning/status");
  const c = s.counts || {};
  const last = s.last_reflection
    ? new Date(s.last_reflection * 1000).toLocaleString()
    : "never";
  const stats = [
    [Object.values(c).reduce((a, b) => a + b, 0), "MEMORIES"],
    [c.skill || 0, "SKILLS"],
    [c.lesson || 0, "LESSONS"],
    [c.report || 0, "DAILY REPORTS"],
    [last, "LAST REFLECTION"],
  ];
  $("#cortex-stats").innerHTML = stats
    .map(([v, l]) => `<div class="stat-card"><div class="stat-val ${String(v).length > 8 ? "stat-small" : ""}">${esc(v)}</div><div class="stat-label">${l}</div></div>`)
    .join("");
  renderMemories();
}

async function renderMemories() {
  const q = $("#mem-q").value.trim();
  const kind = $("#mem-kind").value;
  const params = new URLSearchParams();
  if (q) params.set("q", q);
  if (kind) params.set("kind", kind);
  const mems = await api.get(`/memory?${params}`);
  const list = $("#cortex-list");
  list.innerHTML = mems.length ? "" : "<p class='hint'>memory bank is empty — it fills itself as the org works</p>";
  mems.forEach((m) => {
    const row = document.createElement("div");
    row.className = "mem-card";
    row.innerHTML = `
      <span class="mem-kind k-${esc(m.kind)}">${esc(KIND_LABELS[m.kind] || m.kind.toUpperCase())}</span>
      ${m.agent ? `<span class="mem-agent">${esc(m.agent)}</span>` : ""}
      <span class="mem-body">${esc(m.content)}</span>
      <span class="mem-meta">imp ${Number(m.importance).toFixed(1)} · used ${m.uses}×</span>`;
    const del = document.createElement("button");
    del.className = "mem-del";
    del.textContent = "✕";
    del.onclick = async () => {
      await api.del(`/memory/${m.id}`);
      renderCortex();
    };
    row.appendChild(del);
    list.appendChild(row);
  });
}

async function runReflection() {
  const btn = $("#reflect-btn");
  btn.disabled = true;
  btn.textContent = "⟳ REFLECTING…";
  try {
    const r = await api.post("/learning/reflect", {});
    const box = $("#reflect-report");
    box.classList.remove("hidden");
    if (r.ok) {
      box.innerHTML = `<b>ZAX'S REFLECTION</b><br>${esc(r.report)}<br>` +
        (r.lessons || []).map((l) => `<span class="hint">→ lesson: ${esc(l)}</span>`).join("<br>");
    } else {
      box.innerHTML = `<span class="hint">${esc(r.error || "reflection failed")}</span>`;
    }
    renderCortex();
  } finally {
    btn.disabled = false;
    btn.textContent = "⟳ REFLECT NOW";
  }
}

function providerStatus(p) {
  if (p.active) return '<span class="chip chip-active">ACTIVE</span>';
  if (p.id === "claude-cli" && !p.cli_found)
    return '<span class="chip chip-warn">claude CLI not found</span>';
  return p.configured
    ? '<span class="chip chip-ok">ready</span>'
    : '<span class="chip chip-warn">needs setup</span>';
}

async function renderProviders() {
  const provs = await api.get("/providers");
  const list = $("#providers-list");
  list.innerHTML = "";
  provs.forEach((p) => {
    const row = document.createElement("div");
    row.className = `prov-row ${p.active ? "active" : ""}`;

    const head = document.createElement("div");
    head.className = "prov-head";
    head.innerHTML = `
      <label class="prov-pick">
        <input type="radio" name="prov" ${p.active ? "checked" : ""} />
        <span class="prov-label">${esc(p.label)}</span>
      </label>
      ${providerStatus(p)}`;
    head.querySelector("input").addEventListener("change", async () => {
      await api.post("/providers/select", { provider: p.id });
      renderSettings();
    });

    const desc = document.createElement("div");
    desc.className = "prov-desc";
    desc.textContent = p.desc;

    const form = document.createElement("div");
    form.className = "prov-form";

    let keyInput = null, modelInput = null, baseInput = null;
    if (!["claude-cli", "ollama", "mock"].includes(p.id)) {
      keyInput = document.createElement("input");
      keyInput.type = "password";
      keyInput.placeholder = p.key_hint ? `API key (${p.key_hint})` : "API key";
      form.appendChild(keyInput);
    }
    if (p.id === "custom") {
      baseInput = document.createElement("input");
      baseInput.placeholder = p.base_url || "base URL, e.g. http://localhost:1234/v1";
      form.appendChild(baseInput);
    }
    if (p.id !== "mock") {
      modelInput = document.createElement("input");
      modelInput.placeholder = `model (${p.model || p.default_model || "required"})`;
      form.appendChild(modelInput);
    }

    const save = document.createElement("button");
    save.textContent = "SAVE";
    save.onclick = async () => {
      const body = { provider: p.id };
      if (keyInput && keyInput.value.trim()) body.api_key = keyInput.value.trim();
      if (modelInput && modelInput.value.trim()) body.model = modelInput.value.trim();
      if (baseInput && baseInput.value.trim()) body.base_url = baseInput.value.trim();
      await api.post("/providers/configure", body);
      renderSettings();
    };
    const test = document.createElement("button");
    test.textContent = "TEST";
    const result = document.createElement("span");
    result.className = "prov-result hint";
    test.onclick = async () => {
      result.textContent = "testing…";
      const r = await api.post("/providers/test", { provider: p.id });
      result.textContent = r.ok
        ? `✓ ${r.seconds}s — ${r.reply}`
        : `✗ ${r.error}`;
      result.style.color = r.ok ? "var(--green)" : "var(--red)";
    };
    if (p.id !== "mock") form.append(save, test);
    form.appendChild(result);

    row.append(head, desc, form);
    list.appendChild(row);
  });
}

/* ============================== memory graph (graphify) */

const GRAPH_COLORS = {
  person: "#ff8fb3", project: "#19e3ff", preference: "#ffb938", fact: "#3dff9e",
  tool: "#b78fff", concept: "#7fd4e6", agent: "#19e3ff", task: "#5d7f93",
};

const GraphView = {
  nodes: [], links: [], byId: {}, adj: {},
  raf: null, drag: null, selected: null, dpr: 1,
  // Simulated-annealing state: alpha fades the forces to zero so the layout
  // settles and the render loop stops (no perpetual jitter, no wasted CPU).
  alpha: 0, alphaMin: 0.005, alphaDecay: 0.04,

  async activate() {
    const canvas = $("#graph-canvas");
    if (!this._bound) this.bind(canvas);
    await this.load();
    this.resize();
    this.reheat(1);
  },

  deactivate() {
    if (this.raf) cancelAnimationFrame(this.raf);
    this.raf = null;
  },

  // Restart the cooling loop (single instance) and re-warm the simulation.
  reheat(a = 0.6) {
    this.alpha = Math.max(this.alpha, a);
    if (this.raf == null && $("#panel-graph").classList.contains("active")) {
      this.raf = requestAnimationFrame(() => this.loop());
    }
  },

  async load() {
    const data = await api.get("/graph");
    const nodes = data.nodes || [];
    const links = data.links || [];
    // preserve positions of nodes that still exist across reloads
    const prev = this.byId;
    const W = $("#graph-canvas-wrap").clientWidth || 600;
    const H = $("#graph-canvas-wrap").clientHeight || 400;
    this.nodes = nodes.map((n, i) => {
      const old = prev[n.id];
      const ang = (i / Math.max(nodes.length, 1)) * Math.PI * 2;
      return {
        id: n.id, label: n.label || n.id, kind: n.kind || "concept",
        summary: n.summary || "",
        x: old ? old.x : W / 2 + Math.cos(ang) * 120 + (Math.random() - 0.5) * 40,
        y: old ? old.y : H / 2 + Math.sin(ang) * 120 + (Math.random() - 0.5) * 40,
        vx: 0, vy: 0, deg: 0,
      };
    });
    this.byId = {};
    this.nodes.forEach((n) => (this.byId[n.id] = n));
    this.adj = {};
    this.links = links
      .map((l) => {
        const s = typeof l.source === "object" ? l.source.id : l.source;
        const t = typeof l.target === "object" ? l.target.id : l.target;
        if (!this.byId[s] || !this.byId[t]) return null;
        this.byId[s].deg++; this.byId[t].deg++;
        (this.adj[s] = this.adj[s] || []).push({ to: t, rel: l.relation, conf: l.confidence, dir: "out" });
        (this.adj[t] = this.adj[t] || []).push({ to: s, rel: l.relation, conf: l.confidence, dir: "in" });
        return { s, t, relation: l.relation || "", confidence: l.confidence || "INFERRED" };
      })
      .filter(Boolean);
    $("#graph-empty").classList.toggle("hidden", this.nodes.length > 0);
    this.renderLegend();
    this.reheat(0.8);  // re-settle the new/changed layout
  },

  renderLegend() {
    const kinds = [...new Set(this.nodes.map((n) => n.kind))];
    $("#graph-legend").innerHTML = kinds
      .map((k) => `<span class="lg"><span class="dot" style="background:${GRAPH_COLORS[k] || "#7fd4e6"}"></span>${esc(k)}</span>`)
      .join("");
  },

  resize() {
    const wrap = $("#graph-canvas-wrap");
    const canvas = $("#graph-canvas");
    this.dpr = window.devicePixelRatio || 1;
    canvas.width = wrap.clientWidth * this.dpr;
    canvas.height = wrap.clientHeight * this.dpr;
  },

  tick() {
    const W = $("#graph-canvas-wrap").clientWidth, H = $("#graph-canvas-wrap").clientHeight;
    const N = this.nodes;
    const a = this.alpha;  // forces fade with alpha so the layout converges
    // repulsion (O(n^2) — fine for a personal graph)
    for (let i = 0; i < N.length; i++) {
      for (let j = i + 1; j < N.length; j++) {
        let dx = N[i].x - N[j].x, dy = N[i].y - N[j].y;
        let d2 = dx * dx + dy * dy || 0.01;
        const f = (1600 / d2) * a;
        const d = Math.sqrt(d2);
        const fx = (dx / d) * f, fy = (dy / d) * f;
        N[i].vx += fx; N[i].vy += fy; N[j].vx -= fx; N[j].vy -= fy;
      }
    }
    // springs along links
    for (const l of this.links) {
      const na = this.byId[l.s], nb = this.byId[l.t];
      let dx = nb.x - na.x, dy = nb.y - na.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const f = (d - 80) * 0.02 * a;
      const fx = (dx / d) * f, fy = (dy / d) * f;
      na.vx += fx; na.vy += fy; nb.vx -= fx; nb.vy -= fy;
    }
    // gravity to center + integrate
    for (const n of N) {
      n.vx += (W / 2 - n.x) * 0.004 * a;
      n.vy += (H / 2 - n.y) * 0.004 * a;
      n.vx *= 0.82; n.vy *= 0.82;
      if (n !== this.drag) { n.x += n.vx; n.y += n.vy; }
      n.x = Math.max(20, Math.min(W - 20, n.x));
      n.y = Math.max(20, Math.min(H - 20, n.y));
    }
  },

  draw() {
    const canvas = $("#graph-canvas");
    const ctx = canvas.getContext("2d");
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    // edges
    for (const l of this.links) {
      const a = this.byId[l.s], b = this.byId[l.t];
      const hot = this.selected && (l.s === this.selected || l.t === this.selected);
      ctx.beginPath();
      ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y);
      ctx.strokeStyle = hot ? "rgba(25,227,255,0.7)" : "rgba(20,48,74,0.8)";
      ctx.lineWidth = hot ? 1.6 : 0.8;
      ctx.setLineDash(l.confidence === "INFERRED" ? [3, 3] : []);
      ctx.stroke();
    }
    ctx.setLineDash([]);
    // nodes
    for (const n of this.nodes) {
      const r = 4 + Math.min(n.deg, 10) * 1.3;
      const sel = n.id === this.selected;
      ctx.beginPath();
      ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
      ctx.fillStyle = GRAPH_COLORS[n.kind] || "#7fd4e6";
      ctx.globalAlpha = this.selected && !sel && !(this.adj[this.selected] || []).some((e) => e.to === n.id) ? 0.35 : 1;
      ctx.fill();
      if (sel) { ctx.strokeStyle = "#fff"; ctx.lineWidth = 1.5; ctx.stroke(); }
      ctx.globalAlpha = 1;
      // Thin labels on big graphs so they don't pile up; always show hubs/selection.
      const labelMin = this.nodes.length > 60 ? 4 : this.nodes.length > 30 ? 2 : 0;
      const near = this.selected && (this.adj[this.selected] || []).some((e) => e.to === n.id);
      if (sel || near || n.deg >= labelMin) {
        ctx.fillStyle = "rgba(201,230,242,0.85)";
        ctx.font = "10px JetBrains Mono, monospace";
        ctx.fillText(n.label.slice(0, 22), n.x + r + 3, n.y + 3);
      }
    }
  },

  loop() {
    if (!$("#panel-graph").classList.contains("active")) { this.raf = null; return; }
    if (this.alpha > this.alphaMin || this.drag) {
      this.tick();
      if (!this.drag) this.alpha *= 1 - this.alphaDecay;
      this.draw();
      this.raf = requestAnimationFrame(() => this.loop());
    } else {
      // settled — draw the final frame once, then stop (no more redraws = no flicker)
      this.alpha = 0;
      this.draw();
      this.raf = null;
    }
  },

  _pick(mx, my) {
    let best = null, bd = 16;
    for (const n of this.nodes) {
      const d = Math.hypot(n.x - mx, n.y - my);
      if (d < bd) { bd = d; best = n; }
    }
    return best;
  },

  bind(canvas) {
    this._bound = true;
    const pos = (e) => {
      const r = canvas.getBoundingClientRect();
      return { x: e.clientX - r.left, y: e.clientY - r.top };
    };
    canvas.addEventListener("mousedown", (e) => {
      const p = pos(e);
      const n = this._pick(p.x, p.y);
      if (n) { this.drag = n; this._moved = false; this.reheat(0.1); }
    });
    window.addEventListener("mousemove", (e) => {
      if (!this.drag) return;
      const p = pos(e);
      this.drag.x = p.x; this.drag.y = p.y; this.drag.vx = this.drag.vy = 0;
      this._moved = true;
      this.reheat(0.3);  // let neighbours follow the dragged node
    });
    window.addEventListener("mouseup", () => {
      const dragged = this.drag;
      this.drag = null;
      if (dragged && !this._moved) this.inspect(dragged.id);
      else this.reheat(0.15);  // gently re-settle after a drag
    });
    window.addEventListener("resize", () => {
      if ($("#panel-graph").classList.contains("active")) { this.resize(); this.reheat(0.3); }
    });
  },

  inspect(id) {
    const n = this.byId[id];
    if (!n) return;
    this.selected = id;
    const rels = (this.adj[id] || []).map((e) => {
      const other = this.byId[e.to];
      const arrow = e.dir === "out" ? "→" : "←";
      return `<div class="gi-rel">${esc(e.rel || "related")} <span class="gi-arrow">${arrow}</span> ${esc(other ? other.label : e.to)}</div>`;
    }).join("") || "<div class='gi-rel hint'>no links yet</div>";
    const box = $("#graph-inspector");
    box.innerHTML = `
      <span class="gi-close">✕</span>
      <div class="gi-label">${esc(n.label)}</div>
      <div class="gi-kind">${esc(n.kind)}${n.summary ? " · " + esc(n.summary) : ""}</div>
      ${rels}
      <button class="gi-del">FORGET THIS NODE</button>`;
    box.classList.remove("hidden");
    this.reheat(0);  // force one redraw to show the highlight even when settled
    box.querySelector(".gi-close").onclick = () => { box.classList.add("hidden"); this.selected = null; this.reheat(0); };
    box.querySelector(".gi-del").onclick = async () => {
      await api.del(`/graph/node/${encodeURIComponent(id)}`);
      box.classList.add("hidden"); this.selected = null;
      await this.load();
    };
  },

  async refreshStats() {
    const s = await api.get("/graph/stats");
    const cards = [
      [s.nodes, "ENTITIES"],
      [s.edges, "RELATIONSHIPS"],
      [(s.god_nodes[0] && s.god_nodes[0].label) || "—", "TOP HUB"],
      [Object.keys(s.by_kind || {}).length, "ENTITY TYPES"],
    ];
    $("#graph-stats").innerHTML = cards
      .map(([v, l]) => `<div class="stat-card"><div class="stat-val ${String(v).length > 8 ? "stat-small" : ""}">${esc(v)}</div><div class="stat-label">${l}</div></div>`)
      .join("");
  },
};

async function runGraphQuery() {
  const q = $("#graph-q").value.trim();
  if (!q) return;
  const box = $("#graph-query-result");
  box.classList.remove("hidden");
  box.textContent = "querying the graph…";
  const r = await api.get(`/graph/query?q=${encodeURIComponent(q)}`);
  box.textContent = r.ok ? (r.text || "No matching nodes.") : `error: ${r.error}`;
}

async function rebuildGraph() {
  const btn = $("#graph-rebuild");
  btn.disabled = true; btn.textContent = "⟳ REBUILDING…";
  try {
    await api.post("/graph/rebuild", {});
    await GraphView.load();
    await GraphView.refreshStats();
  } finally {
    btn.disabled = false; btn.textContent = "⟳ REBUILD";
  }
}

/* ============================== live feed ticker */

let lastEventId = 0;
let feedPrimed = false; // don't voice-announce stale events on page load
async function pollFeed() {
  try {
    const events = await api.get(`/feed?after=${lastEventId}`);
    if (events.length) {
      lastEventId = Math.max(...events.map((e) => e.id));
      $("#ticker").textContent = events[0].message;  // ticker only — no auto-speak
    }
    feedPrimed = true;
  } catch {}
}

/* ============================== helpers + wiring */

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function refreshAll() {
  renderOrg();
  renderTasks();
  renderOps();
  renderCortex();
  renderSessions();
  GraphView.refreshStats();
  renderSettings();
}

function setupNav() {
  $$(".nav-btn").forEach((btn) =>
    btn.addEventListener("click", () => {
      $$(".nav-btn").forEach((b) => b.classList.remove("active"));
      $$(".panel").forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      $(`#panel-${btn.dataset.panel}`).classList.add("active");
      refreshAll();
      if (btn.dataset.panel === "skills") renderSkills();
      if (btn.dataset.panel === "graph") GraphView.activate();
      else GraphView.deactivate();
    })
  );
}

/* ============================== skills catalog */

async function renderSkills() {
  const cats = await api.get("/skills");
  const wrap = $("#skills-catalog");
  wrap.innerHTML = "";
  Object.entries(cats).forEach(([cat, packs]) => {
    const group = document.createElement("div");
    group.className = "skill-group";
    group.innerHTML = `<h3 class="skill-cat">${esc(cat)}</h3>`;
    const grid = document.createElement("div");
    grid.className = "skill-grid";
    packs.forEach((p) => {
      const card = document.createElement("div");
      card.className = `skill-card ${p.hired ? "hired" : ""}`;
      card.innerHTML = `
        <div class="skill-top"><span class="skill-emoji">${esc(p.emoji)}</span>
          <div><div class="skill-name">${esc(p.name)}</div>
          <div class="skill-title">${esc(p.title)}</div></div></div>
        <div class="skill-role">${esc(p.role)}</div>`;
      const btn = document.createElement("button");
      btn.className = "skill-hire";
      btn.textContent = p.hired ? "✓ ON STAFF" : "+ HIRE";
      btn.disabled = p.hired;
      btn.onclick = async () => {
        btn.disabled = true; btn.textContent = "hiring…";
        await api.post("/skills/hire", { skill: p.key });
        renderSkills();
        renderOrg();
      };
      card.appendChild(btn);
      grid.appendChild(card);
    });
    group.appendChild(grid);
    wrap.appendChild(group);
  });
}

// One place that paints the provider badge + surfaces any core error reason.
function updateProviderBadge(status) {
  const badge = $("#provider-badge");
  const activeProvider = status.provider;
  const effectiveProvider = status._effective_provider || activeProvider;
  let text = `core: ${activeProvider}${status.model ? " · " + status.model : ""}`;
  if (effectiveProvider !== activeProvider) text += ` → ${effectiveProvider} (fallback)`;
  const err = status.circuit_breaker?.last_error;
  if (status.circuit_breaker?.backoff_active) {
    text += ` ⚠ RECONNECTING (${status.circuit_breaker.retry_in || 0}s)`;
    badge.style.color = "var(--amber)";
    badge.style.borderColor = "rgba(255,185,56,0.3)";
  } else if (err) {
    text += " ⚠ CORE ERROR";
    badge.style.color = "var(--red)";
    badge.style.borderColor = "rgba(255,77,94,0.3)";
  } else if (!status.provider_online) {
    text += " ⚠ DISCONNECTED";
    badge.style.color = "var(--red)";
    badge.style.borderColor = "rgba(255,77,94,0.3)";
  } else {
    badge.style.color = "";
    badge.style.borderColor = "";
  }
  badge.textContent = text;
  // Full reason on hover + a visible line so the founder knows WHY work stalled.
  badge.title = err || "";
  const line = $("#core-error-line");
  if (line) {
    line.textContent = err ? `⚠ ${err}` : "";
    line.classList.toggle("hidden", !err);
  }
}

async function boot() {
  $("#boot").classList.add("fade");
  setTimeout(() => $("#boot").remove(), 1000);
  $("#app").classList.remove("hidden");

  const status = await api.get("/status");
  updateProviderBadge(status);
  $("#org-founder").textContent = status.founder.toUpperCase();

  await renderSessions();
  await loadHistory(activeSession);
  refreshAll();

  // Zax welcomes the Founder (text always; spoken only if voice is on).
  const g = await api.get("/greeting");
  addMsg("zax", g.text);
  ZaxVoice.speak(g.text);  // no-op unless the founder enabled voice

  setInterval(pollFeed, 4000);
  setInterval(async () => {
    try {
      updateProviderBadge(await api.get("/status"));
    } catch {}
    if ($("#panel-tasks").classList.contains("active")) renderTasks();
    if ($("#panel-org").classList.contains("active")) renderOrg();
    if ($("#panel-ops").classList.contains("active")) renderOps();
  }, 4000);
}

document.addEventListener("DOMContentLoaded", () => {
  if (window.speechSynthesis) speechSynthesis.getVoices(); // warm the voice list

  $("#boot-btn").addEventListener("click", boot);

  $("#chat-form").addEventListener("submit", (e) => {
    e.preventDefault();
    sendChat();
  });

  $("#new-chat-btn").addEventListener("click", newChat);

  $("#graph-query-form").addEventListener("submit", (e) => {
    e.preventDefault();
    runGraphQuery();
  });
  $("#graph-rebuild").addEventListener("click", rebuildGraph);

  $("#hire-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const role = $("#hire-role").value.trim();
    if (!role) return;
    $("#hire-role").value = "";
    await api.post("/hire", { role });
    renderOrg();
  });

  $("#task-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const title = $("#task-title").value.trim();
    if (!title) return;
    await api.post("/tasks", {
      title,
      description: $("#task-desc").value.trim(),
      priority: parseInt($("#task-priority").value, 10),
    });
    $("#task-title").value = "";
    $("#task-desc").value = "";
    renderTasks();
    burstRefreshTasks();  // executes now, not on the next heartbeat
  });

  $("#run-now-btn").addEventListener("click", async () => {
    const btn = $("#run-now-btn");
    const status = $("#run-now-status");
    btn.disabled = true;
    btn.textContent = "▶ RUNNING…";
    status.textContent = "Zax is draining the queue…";
    burstRefreshTasks();
    try {
      const r = await api.post("/run", {});
      if (!r.ok) {
        // RUN NOW forces past the circuit breaker, so a failure means the core
        // itself errored on the retry.
        status.textContent = `core error: ${r.error || "unknown"}`;
      } else if (r.executed === 0 && r.reviewed === 0) {
        status.textContent = r.pending_before
          ? `nothing ran — ${r.pending_before} task(s) pending but no agent could take them`
          : "nothing pending — queue is clear";
      } else {
        status.textContent = `✓ ran ${r.executed} task(s), reviewed ${r.reviewed}`;
      }
    } catch (e) {
      status.textContent = `error: ${e.message}`;
    } finally {
      btn.disabled = false;
      btn.textContent = "▶ RUN NOW";
      refreshAll();  // a drain can hire/fire/throttle — refresh org too
    }
  });

  $("#routine-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = $("#routine-name").value.trim();
    if (!name) return;
    await api.post("/routines", {
      name,
      description: $("#routine-desc").value.trim(),
      interval_minutes: parseInt($("#routine-interval").value, 10) || 1440,
    });
    $("#routine-name").value = "";
    $("#routine-desc").value = "";
    renderOps();
  });

  $("#mem-search-form").addEventListener("submit", (e) => {
    e.preventDefault();
    renderMemories();
  });
  $("#mem-kind").addEventListener("change", renderMemories);
  $("#reflect-btn").addEventListener("click", runReflection);

  $("#voice-enabled").addEventListener("change", (e) => ZaxVoice.setEnabled(e.target.checked));
  $("#voice-toggle").addEventListener("click", () => ZaxVoice.setEnabled(!ZaxVoice.enabled));
  $("#voice-test").addEventListener("click", () =>
    ZaxVoice.speak("Zax here. If you can hear this, my voice is working — try not to get used to the silence.", { force: true })
  );
  $("#voice-provider").addEventListener("change", (e) => saveVoice({ provider: e.target.value }));
  $("#voice-edge-select").addEventListener("change", (e) => saveVoice({ edge_voice: e.target.value }));
  $("#voice-eleven-select").addEventListener("change", (e) => saveVoice({ eleven_voice: e.target.value }));
  $("#voice-eleven-connect").addEventListener("click", async () => {
    const key = $("#voice-eleven-key").value.trim();
    if (!key) return;
    $("#voice-status").textContent = "verifying…";
    await saveVoice({ eleven_key: key, provider: "elevenlabs" });
    $("#voice-eleven-key").value = "";
  });

  $("#telegram-connect").addEventListener("click", async () => {
    const tok = $("#telegram-token").value.trim();
    if (!tok) return;
    const btn = $("#telegram-connect");
    btn.disabled = true; btn.textContent = "…";
    try {
      await api.post("/telegram/connect", { token: tok });
      $("#telegram-token").value = "";
    } catch (e) {
      $("#telegram-status").textContent = `✗ ${e.message}`;
    } finally {
      btn.disabled = false; btn.textContent = "CONNECT";
      renderTelegram();
    }
  });
  $("#telegram-disconnect").addEventListener("click", async () => {
    await api.post("/telegram/disconnect", {});
    renderTelegram();
  });
  $("#telegram-notify").addEventListener("change", async (e) => {
    await api.post("/telegram/notify", { enabled: e.target.checked });
  });

  setupNav();
  setupMic();
});
