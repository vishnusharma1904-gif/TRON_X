/* ════════════════════════════════════════════════════════════════════
   TRON-X — Proactive HUD module  (Phase 37)
   Self-contained, additive: injects its own DOM, never touches the
   existing hud.js / panels.js contracts.

   - ORACLE button in the topbar → on-demand briefing
   - Floating Oracle dock: briefing card + live neural feed (SSE)
   - Proactive alert toasts (sentinel events), spoken-friendly
   ════════════════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  const API = "";   // same-origin
  const MAX_FEED = 30;

  /* ── DOM scaffolding ──────────────────────────────────────────────── */

  function el(tag, cls, html) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html !== undefined) n.innerHTML = html;
    return n;
  }

  const dock = el("div", "oracle-dock");
  dock.innerHTML = `
    <div class="oracle-head">
      <span class="title">◈ ORACLE</span>
      <span class="sub" id="oracle-status">LINK…</span>
      <button class="oracle-close" title="Close">✕</button>
    </div>
    <div class="oracle-body">
      <div id="briefing-slot"></div>
      <div class="feed-label">NEURAL FEED — LIVE</div>
      <div id="oracle-feed"><div class="feed-empty">Awaiting activity…</div></div>
    </div>`;
  document.body.appendChild(dock);

  const alertStack = el("div", "alert-stack");
  document.body.appendChild(alertStack);

  const oracleBtn = el("button", "oracle-btn",
    `<span class="pulse"></span>ORACLE`);
  oracleBtn.title = "Briefing & live activity (Ctrl+O)";

  // Mount next to the persona badge if possible, else topbar-right.
  const mount = document.querySelector(".topbar-right");
  if (mount) mount.insertBefore(oracleBtn, mount.firstChild);

  const feedBox   = dock.querySelector("#oracle-feed");
  const briefSlot = dock.querySelector("#briefing-slot");
  const statusEl  = dock.querySelector("#oracle-status");

  /* ── Dock open/close ──────────────────────────────────────────────── */

  let briefedOnce = false;
  function toggleDock(force) {
    const open = force !== undefined ? force : !dock.classList.contains("open");
    dock.classList.toggle("open", open);
    if (open && !briefedOnce) { briefedOnce = true; requestBriefing("adhoc"); }
  }
  oracleBtn.addEventListener("click", () => toggleDock());
  dock.querySelector(".oracle-close").addEventListener("click", () => toggleDock(false));
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey && (e.key === "o" || e.key === "O")) {
      e.preventDefault(); toggleDock();
    }
  });

  /* ── Briefing ─────────────────────────────────────────────────────── */

  function kindForNow() {
    const h = new Date().getHours();
    if (h < 12) return "morning";
    if (h >= 18) return "evening";
    return "adhoc";
  }

  async function requestBriefing(kind) {
    kind = kind || kindForNow();
    briefSlot.innerHTML = "";
    const card = el("div", "briefing-card loading", `
      <div class="kicker">✦ BRIEFING<span class="src">composing…</span></div>
      <div class="text">Gathering calendar, mail, weather, memory…</div>`);
    briefSlot.appendChild(card);
    try {
      const persona = (document.body.dataset.persona || "jarvis");
      const r = await fetch(
        `${API}/api/proactive/briefing?kind=${kind}&persona=${persona}`);
      const data = await r.json();
      card.classList.remove("loading");
      card.querySelector(".text").textContent = data.text || "—";
      card.querySelector(".src").textContent =
        (data.sources || []).join(" · ") || "no live sources";
    } catch (err) {
      card.classList.remove("loading");
      card.querySelector(".text").textContent =
        "Briefing unavailable — backend unreachable.";
    }
  }

  /* ── Live feed (SSE with auto-reconnect) ──────────────────────────── */

  function feedClass(type) {
    if (type.startsWith("proactive.trigger")) return "t-proactive";
    if (type.startsWith("proactive.briefing")) return "t-briefing";
    if (type.startsWith("memory.")) return "t-memory";
    return "";
  }

  function fmtTime(ts) {
    try {
      return new Date(ts * 1000).toLocaleTimeString([], {
        hour: "2-digit", minute: "2-digit", second: "2-digit" });
    } catch { return ""; }
  }

  function feedBody(evt) {
    const d = evt.data || {};
    if (evt.type === "proactive.trigger") return `${d.title || ""} — ${d.body || ""}`;
    if (evt.type === "proactive.briefing") return (d.text || "").slice(0, 160);
    if (evt.type === "agent.result")
      return `${d.intent || "?"} → ${d.model || "?"} (${d.latency_ms ?? "?"}ms) ${d.preview || ""}`;
    if (evt.type === "memory.consolidated")
      return `promoted ${d.promoted ?? 0} topics, pruned ${d.pruned ?? 0} episodes`;
    return JSON.stringify(d).slice(0, 140);
  }

  function pushFeed(evt) {
    const empty = feedBox.querySelector(".feed-empty");
    if (empty) empty.remove();
    const item = el("div", `feed-item ${feedClass(evt.type)}`, `
      <span class="dot"></span>
      <div class="meta">
        <div class="head">
          <span class="type"></span><span class="ts">${fmtTime(evt.ts)}</span>
        </div>
        <div class="body"></div>
      </div>`);
    item.querySelector(".type").textContent = evt.type;
    item.querySelector(".body").textContent = feedBody(evt);
    feedBox.prepend(item);
    while (feedBox.children.length > MAX_FEED) feedBox.lastChild.remove();
  }

  /* ── Proactive alert toasts ───────────────────────────────────────── */

  const GLYPHS = { calendar: "🗓", email: "✉", system: "⚙" };

  function toast(evt) {
    const d = evt.data || {};
    const t = el("div",
      `lux-toast urgency-${d.urgency || "info"}`, `
      <span class="glyph">${GLYPHS[d.category] || "✦"}</span>
      <div>
        <div class="t-title"></div>
        <div class="t-body"></div>
      </div>
      <button class="t-x" title="Dismiss">✕</button>`);
    t.querySelector(".t-title").textContent = d.title || "Notice";
    t.querySelector(".t-body").textContent = d.body || "";
    const kill = () => { t.classList.add("leaving");
      setTimeout(() => t.remove(), 400); };
    t.querySelector(".t-x").addEventListener("click", kill);
    alertStack.appendChild(t);
    setTimeout(kill, 14000);

    // Speak it if the HUD's voice layer is present & voice mode is on
    try {
      if (typeof Voice !== "undefined" && !Voice.isMuted()) {
        Voice.speak(`${d.title}. ${d.body || ""}`);
      }
    } catch { /* voice optional */ }
  }

  /* ── SSE connection ───────────────────────────────────────────────── */

  let es = null, retryMs = 2000;

  function connect() {
    if (es) es.close();
    let _uid = "", _adminKey = "";
    try {
      _uid = localStorage.getItem("tronx_user_id") || "";
      _adminKey = localStorage.getItem("tronx_admin_key") || "";
    } catch (e) {}
    let _qs = "";
    if (_uid) _qs += `&user_id=${encodeURIComponent(_uid)}`;
    if (_adminKey) _qs += `&api_key=${encodeURIComponent(_adminKey)}`;
    es = new EventSource(`${API}/api/proactive/stream?backfill=10${_qs}`);
    es.onopen = () => {
      retryMs = 2000;
      statusEl.textContent = "LIVE";
    };
    es.onmessage = (m) => {
      let evt;
      try { evt = JSON.parse(m.data); } catch { return; }
      pushFeed(evt);
      if (evt.type === "proactive.trigger") toast(evt);
      if (evt.type === "proactive.briefing" && dock.classList.contains("open")) {
        // refresh the briefing card when a scheduled one lands
        const card = briefSlot.querySelector(".briefing-card .text");
        if (card && evt.data && evt.data.text) card.textContent = evt.data.text;
      }
    };
    es.onerror = () => {
      statusEl.textContent = "RETRY…";
      es.close();
      setTimeout(connect, retryMs);
      retryMs = Math.min(retryMs * 2, 30000);
    };
  }

  connect();
})();
