/* ════════════════════════════════════════════════════════════════════
   TRON-X — Wake Word module  (Phase 38)
   "Hey Tron" / "Tron" / "Jarvis" / "Friday" → hands-free activation.

   Uses the browser's continuous SpeechRecognition (no model download,
   no backend load). Self-contained & additive:
   - injects a topbar toggle (●⃝ WAKE) and persists the preference
   - pauses itself while TTS is speaking (no self-triggering)
   - on wake: chime + glow + triggers the existing PTT mic flow
   ════════════════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const WAKE_WORDS = ["hey tron", "okay tron", "tron", "jarvis", "friday"];
  const COOLDOWN_MS = 4000;

  let rec = null, enabled = false, lastFire = 0, restarting = false;

  /* ── Topbar toggle ────────────────────────────────────────────────── */
  const btn = document.createElement("button");
  btn.className = "oracle-btn wake-btn";
  btn.title = "Wake word: say “Hey Tron” (hands-free)";
  btn.innerHTML = `<span class="pulse" style="display:none"></span>WAKE`;
  const mount = document.querySelector(".topbar-right");
  if (mount) mount.insertBefore(btn, mount.firstChild);

  function setUI(on, listening) {
    btn.style.opacity = on ? "1" : "0.45";
    const p = btn.querySelector(".pulse");
    if (p) p.style.display = on && listening ? "inline-block" : "none";
  }

  /* ── Wake handling ────────────────────────────────────────────────── */

  function chime() {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.frequency.value = 880; o.type = "sine";
      g.gain.setValueAtTime(0.0001, ctx.currentTime);
      g.gain.exponentialRampToValueAtTime(0.12, ctx.currentTime + 0.04);
      g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.5);
      o.connect(g); g.connect(ctx.destination);
      o.start(); o.stop(ctx.currentTime + 0.55);
    } catch (e) { /* chime optional */ }
  }

  function onWake(word) {
    const now = Date.now();
    if (now - lastFire < COOLDOWN_MS) return;
    lastFire = now;
    chime();
    document.body.classList.add("wake-flash");
    setTimeout(() => document.body.classList.remove("wake-flash"), 1200);

    // If Friday/Jarvis was called by name, switch persona to match
    if (word === "friday" || word === "jarvis") {
      const sel = document.getElementById("chat-persona");
      if (sel && sel.value !== word) {
        sel.value = word;
        sel.dispatchEvent(new Event("change", { bubbles: true }));
      }
    }

    // Trigger the existing push-to-talk mic flow
    const ptt = document.getElementById("chat-ptt");
    if (ptt) { ptt.click(); }
    else { document.getElementById("chat-input")?.focus(); }
  }

  /* ── Recognition loop ─────────────────────────────────────────────── */

  function speaking() {
    // pause while TTS audio is playing so it can't hear itself
    const s = document.getElementById("speaking-status");
    return s && s.style.display !== "none" && s.style.display !== "";
  }

  function start() {
    if (!SR || rec) return;
    rec = new SR();
    rec.continuous = true;
    rec.interimResults = true;
    rec.lang = "en-IN";          // good with Indian-accent English + names
    rec.onresult = (e) => {
      if (speaking()) return;
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const txt = (e.results[i][0].transcript || "").toLowerCase().trim();
        for (const w of WAKE_WORDS) {
          if (txt.includes(w)) { onWake(w === "hey tron" || w === "okay tron" ? "tron" : w); return; }
        }
      }
    };
    rec.onend = () => {           // Chrome stops recognition periodically
      rec = null;
      if (enabled && !restarting) {
        restarting = true;
        setTimeout(() => { restarting = false; if (enabled) start(); }, 400);
      }
      setUI(enabled, false);
    };
    rec.onerror = (e) => {
      if (e.error === "not-allowed" || e.error === "service-not-allowed") {
        enabled = false;
        localStorage.setItem("tronx_wake", "false");
      }
    };
    try { rec.start(); setUI(true, true); } catch (e) { rec = null; }
  }

  function stop() {
    if (rec) { try { rec.onend = null; rec.stop(); } catch (e) {} rec = null; }
    setUI(enabled, false);
  }

  function toggle(force) {
    enabled = force !== undefined ? force : !enabled;
    localStorage.setItem("tronx_wake", String(enabled));
    if (enabled) start(); else stop();
    setUI(enabled, enabled && !!rec);
  }

  btn.addEventListener("click", () => toggle());

  if (!SR) {
    btn.title = "Wake word needs Chrome/Edge (SpeechRecognition API)";
    btn.style.opacity = "0.25";
    btn.disabled = true;
  } else if (localStorage.getItem("tronx_wake") === "true") {
    toggle(true);
  } else {
    setUI(false, false);
  }

  window.TronWake = { toggle };
})();
