/* =========================================================================
 * TRON-X // JARVIS A.V.E.N.G.E.R.S PROTOCOL — command center frontend
 * =========================================================================
 * - VIEWPORT 1: 21-node circular boardroom (JARVIS core + 20 orbital nodes)
 * - VIEWPORT 2: streaming command console fed by /ws/avengers
 * - Voice: browser VAD-gated MediaRecorder -> base64 frames over WebSocket;
 *          server confirms wake word on the transcript; Kokoro audio frames
 *          stream back and play instantly while text streams into the console.
 * ========================================================================= */
"use strict";

/* ------------------------------------------------------------------ state */
const S = {
  ws: null,
  wsReady: false,
  reconnectDelay: 1000,
  sessionId: null,
  roster: [],
  nodes: {},            // persona id -> { g, beam, cfg }
  activeAgent: null,
  streamEl: null,       // current streaming console line
  audioQueue: [],
  audioPlaying: false,
  // voice
  micOn: false,
  wakeMode: false,
  pttHeld: false,
  mediaStream: null,
  recorder: null,
  chunks: [],
  audioCtx: null,
  analyser: null,
  vadSpeaking: false,
  vadSilenceMs: 0,
  vadLastTick: 0,
  // output / agent toggles
  voiceOut: true,
  agentMode: false,
};

const VAD = { threshold: 0.022, hangoverMs: 750, minSpeechMs: 280 };
let speechStartedAt = 0;

const $ = (id) => document.getElementById(id);
const consoleEl = $("console");

/* ============================================================ console === */
function line(cls, text, { stream = false } = {}) {
  const div = document.createElement("div");
  div.className = `ln ${cls}` + (stream ? " cursor" : "");
  div.textContent = text;
  consoleEl.appendChild(div);
  while (consoleEl.children.length > 600) consoleEl.removeChild(consoleEl.firstChild);
  consoleEl.scrollTop = consoleEl.scrollHeight;
  return div;
}

function logLine(raw) {
  let cls = "ln-log";
  if (raw.includes("ERROR")) cls = "ln-err";
  else if (raw.includes("WARNING")) cls = "ln-warn";
  line(cls, raw);
}

/* ========================================================== boardroom === */
function buildBoardroom() {
  const host = $("boardroom");
  host.innerHTML = "";
  const W = 900, H = 760, cx = W / 2, cy = H / 2, R = 285;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");

  const defs = document.createElementNS(svg.namespaceURI, "defs");
  defs.innerHTML = `
    <filter id="glow" x="-80%" y="-80%" width="260%" height="260%">
      <feGaussianBlur stdDeviation="3.2" result="b"/>
      <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
    <radialGradient id="coreGrad">
      <stop offset="0%"  stop-color="#0ff" stop-opacity="0.55"/>
      <stop offset="55%" stop-color="#089" stop-opacity="0.18"/>
      <stop offset="100%" stop-color="#000" stop-opacity="0"/>
    </radialGradient>`;
  svg.appendChild(defs);

  // faint orbit ring
  const orbit = document.createElementNS(svg.namespaceURI, "circle");
  orbit.setAttribute("cx", cx); orbit.setAttribute("cy", cy); orbit.setAttribute("r", R);
  orbit.setAttribute("fill", "none");
  orbit.setAttribute("stroke", "rgba(0,229,255,0.14)");
  orbit.setAttribute("stroke-dasharray", "2 9");
  svg.appendChild(orbit);

  const beams = document.createElementNS(svg.namespaceURI, "g");
  svg.appendChild(beams);

  const jarvis = S.roster.find((r) => r.id === "jarvis");
  const orbitals = S.roster.filter((r) => r.id !== "jarvis");

  /* ---- core (JARVIS) ---- */
  const core = document.createElementNS(svg.namespaceURI, "g");
  core.setAttribute("id", "core-group");
  core.classList.add("node");
  core.dataset.pid = "jarvis";
  core.innerHTML = `
    <circle cx="${cx}" cy="${cy}" r="96" fill="url(#coreGrad)"/>
    <g class="ring-a">
      <circle cx="${cx}" cy="${cy}" r="72" fill="none" stroke="#00e5ff" stroke-width="1.4"
              stroke-dasharray="40 14 6 14" filter="url(#glow)"/>
    </g>
    <g class="ring-b">
      <circle cx="${cx}" cy="${cy}" r="58" fill="none" stroke="#00e5ff" stroke-width="0.9"
              stroke-dasharray="4 9" opacity="0.7"/>
    </g>
    <circle class="halo" cx="${cx}" cy="${cy}" r="84" fill="none" stroke="#00e5ff" stroke-width="2"/>
    <circle class="shell" cx="${cx}" cy="${cy}" r="44" fill="rgba(3,10,18,0.92)"
            stroke="#00e5ff" stroke-width="1.6" filter="url(#glow)"/>
    <text x="${cx}" y="${cy - 3}" text-anchor="middle" fill="#aef7ff"
          font-size="14" letter-spacing="3" font-weight="600">JARVIS</text>
    <text x="${cx}" y="${cy + 14}" text-anchor="middle" fill="#2c8da0" font-size="8"
          letter-spacing="2">PRIME ORCHESTRATOR</text>`;
  svg.appendChild(core);
  if (jarvis) S.nodes.jarvis = { g: core, beam: null, cfg: jarvis };
  attachNodeEvents(core, jarvis);

  /* ---- 20 orbital nodes (slots 1..20) ---- */
  orbitals.forEach((cfg) => {
    const idx = cfg.slot - 1;
    const angle = (idx / orbitals.length) * Math.PI * 2 - Math.PI / 2;
    const x = cx + R * Math.cos(angle);
    const y = cy + R * Math.sin(angle);

    const beam = document.createElementNS(svg.namespaceURI, "line");
    beam.setAttribute("x1", cx); beam.setAttribute("y1", cy);
    beam.setAttribute("x2", x);  beam.setAttribute("y2", y);
    beam.setAttribute("stroke", cfg.color);
    beam.setAttribute("stroke-width", "1.4");
    beam.classList.add("beam");
    beams.appendChild(beam);

    const g = document.createElementNS(svg.namespaceURI, "g");
    g.classList.add("node");
    g.dataset.pid = cfg.id;
    g.innerHTML = `
      <circle class="halo"  cx="${x}" cy="${y}" r="34" fill="none" stroke="${cfg.color}" stroke-width="2"/>
      <circle class="shell" cx="${x}" cy="${y}" r="25" fill="rgba(4,11,19,0.94)"
              stroke="${cfg.color}" stroke-width="1.4" filter="url(#glow)"/>
      <text class="node-glyph" x="${x}" y="${y + 5}" text-anchor="middle" fill="${cfg.color}">${cfg.glyph}</text>
      <text x="${x}" y="${y + 41}" text-anchor="middle" fill="#7fb6c4" font-size="9"
            letter-spacing="1.5">${cfg.codename}</text>`;
    svg.appendChild(g);
    S.nodes[cfg.id] = { g, beam, cfg };
    attachNodeEvents(g, cfg);
  });

  host.appendChild(svg);
}

function attachNodeEvents(g, cfg) {
  if (!cfg) return;
  const tip = $("tooltip");
  g.addEventListener("mouseenter", (e) => {
    tip.style.display = "block";
    tip.innerHTML = `<div style="color:${cfg.color};letter-spacing:.2em">${cfg.codename}</div>
      <div class="mono" style="color:#9beaf7;margin:2px 0">${cfg.title}</div>
      <div style="color:#51808f">${cfg.description}</div>
      <div class="mono" style="color:#2c5560;margin-top:4px;font-size:10px">↳ ${cfg.backend}</div>`;
  });
  g.addEventListener("mousemove", (e) => {
    tip.style.left = Math.min(e.clientX + 14, window.innerWidth - 300) + "px";
    tip.style.top = e.clientY + 14 + "px";
  });
  g.addEventListener("mouseleave", () => { tip.style.display = "none"; });
  g.addEventListener("click", () => {
    const cmd = $("cmd");
    cmd.value = `@${cfg.id} `;
    cmd.focus();
    line("ln-sys", `· directive channel locked to ${cfg.codename} — type your order`);
  });
}

function setAgentState(pid, state) {
  const node = S.nodes[pid];
  if (!node) return;
  node.g.classList.remove("active", "error");
  if (node.beam) node.beam.classList.remove("on");
  if (state === "active") {
    node.g.classList.add("active");
    if (node.beam) node.beam.classList.add("on");
    S.activeAgent = pid;
    $("active-agent").textContent = `CORE LINK: ${node.cfg.codename} · ${node.cfg.title.toUpperCase()}`;
  } else if (state === "error") {
    node.g.classList.add("error");
  } else if (S.activeAgent === pid) {
    S.activeAgent = null;
    $("active-agent").textContent = "CORE: STANDBY";
  }
}

function buildRosterStrip() {
  const strip = $("roster-strip");
  strip.innerHTML = "";
  S.roster.forEach((cfg) => {
    const chip = document.createElement("button");
    chip.className = "btn shrink-0";
    chip.style.borderColor = cfg.color + "55";
    chip.style.color = cfg.color;
    chip.textContent = `${cfg.glyph} ${cfg.codename}`;
    chip.title = `${cfg.title} — ${cfg.description}`;
    chip.onclick = () => { $("cmd").value = `@${cfg.id} `; $("cmd").focus(); };
    strip.appendChild(chip);
  });
}

/* ========================================================== websocket === */
function wsConnect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/avengers`);
  S.ws = ws;

  ws.onopen = () => {
    S.wsReady = true;
    S.reconnectDelay = 1000;
    $("net-badge").textContent = "LINK: SECURE";
    $("net-badge").style.color = "#5dfdcb";
    line("ln-sys", "· uplink established — A.V.E.N.G.E.R.S protocol active");
    ws.send(JSON.stringify({ type: "subscribe_logs" }));
  };

  ws.onclose = () => {
    S.wsReady = false;
    $("net-badge").textContent = "LINK: OFFLINE";
    $("net-badge").style.color = "#ef233c";
    line("ln-err", `· uplink lost — re-establishing in ${S.reconnectDelay / 1000}s`);
    setTimeout(wsConnect, S.reconnectDelay);
    S.reconnectDelay = Math.min(S.reconnectDelay * 2, 15000);
  };

  ws.onerror = () => ws.close();

  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    handleEvent(msg);
  };
}

function wsSend(obj) {
  if (S.wsReady) S.ws.send(JSON.stringify(obj));
}

function handleEvent(msg) {
  switch (msg.type) {
    case "boot":
      S.sessionId = msg.session_id;
      S.roster = msg.roster || [];
      buildBoardroom();
      buildRosterStrip();
      line("ln-sys", `· session ${msg.session_id} — ${S.roster.length} agents on the board · wake word: "${msg.wake_word}"`);
      break;

    case "agent_state":
      setAgentState(msg.id, msg.state);
      break;

    case "ops": {
      const node = S.nodes[msg.persona];
      const name = node ? node.cfg.codename : msg.persona.toUpperCase();
      line("ln-ops", `[${name}::ops] ${msg.summary}`);
      break;
    }

    case "meta": {
      const lang = msg.language_profile;
      const langBadge = $("lang-badge");
      if (lang && lang.detected) {
        langBadge.textContent = `తెలుగు · ${lang.dialect || "telugu"}`;
        langBadge.classList.remove("hidden");
      } else langBadge.classList.add("hidden");
      const emo = msg.emotion;
      const emoBadge = $("emotion-badge");
      if (emo && emo !== "neutral") {
        emoBadge.textContent = `EMO: ${emo}`;
        emoBadge.classList.remove("hidden");
      } else emoBadge.classList.add("hidden");
      const who = msg.codename || (msg.avenger || msg.persona || "jarvis").toUpperCase();
      S.streamEl = line("ln-agent", `[${who}] `, { stream: true });
      break;
    }

    case "text":
      if (!S.streamEl) S.streamEl = line("ln-agent", "", { stream: true });
      S.streamEl.textContent += msg.content || "";
      consoleEl.scrollTop = consoleEl.scrollHeight;
      break;

    case "done":
      if (S.streamEl) { S.streamEl.classList.remove("cursor"); S.streamEl = null; }
      if (msg.model && msg.model !== "avengers_ops")
        line("ln-sys", `· ${msg.model} · ${msg.latency_ms}ms · ${msg.tokens_used || 0} tok`);
      break;

    case "transcript":
      line("ln-user", `🎙 ${msg.text}`);
      break;

    case "wake":
      if (msg.detected) line("ln-sys", "· wake word confirmed — executing");
      else line("ln-sys", "· no wake word — standing down");
      break;

    case "audio":
      enqueueAudio(msg);
      break;

    case "log":
      logLine(msg.line);
      break;

    case "error":
      line("ln-err", `! ${msg.message}`);
      if (S.streamEl) { S.streamEl.classList.remove("cursor"); S.streamEl = null; }
      break;

    case "agent_step": {
      if (msg.status === "next") {
        const node = S.nodes[msg.persona];
        const name = node ? node.cfg.codename : (msg.persona || "").toUpperCase();
        line("ln-sys", `· agent mode → step ${msg.step}: handing off to ${name} — "${msg.instruction}"`);
      } else if (msg.status === "complete") {
        line("ln-sys", "· agent mode chain complete");
      } else if (msg.status === "stopped") {
        line("ln-sys", "· agent mode chain stopped");
      } else if (msg.status === "max_steps") {
        line("ln-warn", `· agent mode reached max steps (${msg.step}) — stopping`);
      }
      break;
    }

    case "agent_strip": {
      // remove the trailing ###NEXT...### continuation marker from the
      // most recently streamed agent line (S.streamEl is already null by
      // the time this arrives, since "done" fired first).
      if (!msg.text) break;
      const lines = consoleEl.querySelectorAll(".ln-agent");
      const last = lines[lines.length - 1];
      if (last) {
        const idx = last.textContent.lastIndexOf(msg.text);
        if (idx !== -1) last.textContent = last.textContent.slice(0, idx).trimEnd();
      }
      break;
    }

    case "pong":
      break;
  }
}

/* ====================================================== audio playback === */
function enqueueAudio(msg) {
  S.audioQueue.push(msg);
  if (!S.audioPlaying) playNextAudio();
}

function playNextAudio() {
  const msg = S.audioQueue.shift();
  if (!msg) { S.audioPlaying = false; return; }
  S.audioPlaying = true;
  try {
    const bytes = Uint8Array.from(atob(msg.b64), (c) => c.charCodeAt(0));
    const mime = msg.format === "mp3" ? "audio/mpeg" : "audio/wav";
    const url = URL.createObjectURL(new Blob([bytes], { type: mime }));
    const audio = new Audio(url);
    audio.onended = () => { URL.revokeObjectURL(url); playNextAudio(); };
    audio.onerror = () => { URL.revokeObjectURL(url); playNextAudio(); };
    audio.play().catch(() => playNextAudio());
  } catch {
    playNextAudio();
  }
}

/* ============================================================= voice ==== */
async function ensureMic() {
  if (S.mediaStream) return true;
  try {
    S.mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
    });
    S.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const src = S.audioCtx.createMediaStreamSource(S.mediaStream);
    S.analyser = S.audioCtx.createAnalyser();
    S.analyser.fftSize = 1024;
    src.connect(S.analyser);
    requestAnimationFrame(vadTick);
    return true;
  } catch (e) {
    line("ln-err", `! microphone denied: ${e.message}`);
    return false;
  }
}

function micRms() {
  const buf = new Float32Array(S.analyser.fftSize);
  S.analyser.getFloatTimeDomainData(buf);
  let sum = 0;
  for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
  return Math.sqrt(sum / buf.length);
}

function updateVu(rms) {
  const bars = document.querySelectorAll("#vu-meter .vu");
  const level = Math.min(1, rms * 18);
  bars.forEach((b, i) => {
    const on = level > (i + 1) / bars.length;
    b.style.transform = `scaleY(${on ? 1 : 0.18})`;
    b.setAttribute("fill", on ? (i >= 4 ? "#ef233c" : "#00e5ff") : "#0e3a4a");
  });
}

function startRecording() {
  if (S.recorder && S.recorder.state === "recording") return;
  S.chunks = [];
  const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
    ? "audio/webm;codecs=opus" : "audio/webm";
  S.recorder = new MediaRecorder(S.mediaStream, { mimeType: mime });
  S.recorder.ondataavailable = (e) => { if (e.data.size) S.chunks.push(e.data); };
  S.recorder.onstop = sendRecording;
  S.recorder.start();
  $("boardroom").querySelector("#core-group")?.classList.add("core-listening");
}

function stopRecording() {
  if (S.recorder && S.recorder.state === "recording") S.recorder.stop();
  $("boardroom").querySelector("#core-group")?.classList.remove("core-listening");
}

async function sendRecording() {
  const blob = new Blob(S.chunks, { type: "audio/webm" });
  S.chunks = [];
  if (blob.size < 2000) return; // too short
  const buf = await blob.arrayBuffer();
  const bytes = new Uint8Array(buf);
  // chunked base64 (avoid call-stack limits on large buffers)
  let bin = "";
  const STEP = 0x8000;
  for (let i = 0; i < bytes.length; i += STEP)
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + STEP));
  wsSend({
    type: "audio",
    b64: btoa(bin),
    format: "webm",
    final: true,
    wake_armed: S.wakeMode && !S.pttHeld,
    session_id: S.sessionId,
    speak: S.voiceOut,
    agent_mode: S.agentMode,
  });
}

function vadTick(ts) {
  if (!S.analyser) return;
  requestAnimationFrame(vadTick);
  const dt = S.vadLastTick ? ts - S.vadLastTick : 16;
  S.vadLastTick = ts;
  const rms = micRms();
  updateVu(rms);

  if (!S.wakeMode || S.pttHeld) return; // VAD auto-capture only in wake mode

  if (rms > VAD.threshold) {
    S.vadSilenceMs = 0;
    if (!S.vadSpeaking) {
      S.vadSpeaking = true;
      speechStartedAt = ts;
      startRecording();
    }
  } else if (S.vadSpeaking) {
    S.vadSilenceMs += dt;
    if (S.vadSilenceMs > VAD.hangoverMs) {
      S.vadSpeaking = false;
      S.vadSilenceMs = 0;
      if (ts - speechStartedAt > VAD.minSpeechMs + VAD.hangoverMs) stopRecording();
      else { // too short — discard
        if (S.recorder && S.recorder.state === "recording") {
          S.recorder.ondataavailable = null;
          S.recorder.onstop = null;
          S.recorder.stop();
          $("boardroom").querySelector("#core-group")?.classList.remove("core-listening");
        }
      }
    }
  }
}

/* push-to-talk */
async function pttDown() {
  if (S.pttHeld) return;
  if (!(await ensureMic())) return;
  S.pttHeld = true;
  $("btn-ptt").classList.add("hot");
  startRecording();
}
function pttUp() {
  if (!S.pttHeld) return;
  S.pttHeld = false;
  $("btn-ptt").classList.remove("hot");
  stopRecording();
}

/* wake-word mode toggle */
async function toggleWake() {
  if (!S.wakeMode) {
    if (!(await ensureMic())) return;
    S.wakeMode = true;
    $("btn-wake").textContent = "WAKE-WORD: ARMED";
    $("btn-wake").classList.add("hot");
    line("ln-sys", '· wake-word mode armed — say "Jarvis, …" (browser VAD + server confirmation)');
  } else {
    S.wakeMode = false;
    $("btn-wake").textContent = "WAKE-WORD: OFF";
    $("btn-wake").classList.remove("hot");
    stopRecording();
    line("ln-sys", "· wake-word mode disarmed");
  }
}

/* ====================================================== output toggles === */
function updateVoiceButton() {
  const btn = $("btn-voice");
  btn.textContent = `VOICE OUT: ${S.voiceOut ? "ON" : "OFF"}`;
  btn.classList.toggle("hot", !S.voiceOut);
}

function toggleVoiceOut() {
  S.voiceOut = !S.voiceOut;
  localStorage.setItem("tronx_avengers_voice_out", String(S.voiceOut));
  updateVoiceButton();
  line("ln-sys", `· voice output ${S.voiceOut ? "enabled" : "disabled"} — console text always streams`);
}

function updateAgentButton() {
  const btn = $("btn-agent");
  btn.textContent = `AGENT MODE: ${S.agentMode ? "ON" : "OFF"}`;
  btn.classList.toggle("hot", S.agentMode);
  $("btn-agent-stop").classList.toggle("hidden", !S.agentMode);
}

function toggleAgentMode() {
  S.agentMode = !S.agentMode;
  localStorage.setItem("tronx_avengers_agent_mode", String(S.agentMode));
  updateAgentButton();
  line("ln-sys", S.agentMode
    ? "· agent mode engaged — JARVIS may chain personas autonomously to finish multi-step objectives"
    : "· agent mode disengaged");
}

function loadOutputToggles() {
  const vo = localStorage.getItem("tronx_avengers_voice_out");
  S.voiceOut = vo === null ? true : vo === "true";
  S.agentMode = localStorage.getItem("tronx_avengers_agent_mode") === "true";
  updateVoiceButton();
  updateAgentButton();
}

/* ========================================================== commands ==== */
function execCommand() {
  const input = $("cmd");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  let persona = null;
  let body = text;
  const m = text.match(/^@([a-z_.]+)\s+(.*)$/i);
  if (m) { persona = m[1].toLowerCase(); body = m[2]; }
  line("ln-user", `❯ ${text}`);
  wsSend({ type: "command", text: body, persona, session_id: S.sessionId, speak: S.voiceOut, agent_mode: S.agentMode });
}

/* ============================================================ wiring ==== */
document.addEventListener("DOMContentLoaded", () => {
  loadOutputToggles();
  wsConnect();
  setInterval(() => wsSend({ type: "ping" }), 25000);
  setInterval(() => {
    $("clock").textContent = new Date().toLocaleTimeString("en-GB");
  }, 1000);

  $("btn-send").onclick = execCommand;
  $("cmd").addEventListener("keydown", (e) => { if (e.key === "Enter") execCommand(); });

  $("btn-ptt").addEventListener("mousedown", pttDown);
  $("btn-ptt").addEventListener("mouseup", pttUp);
  $("btn-ptt").addEventListener("mouseleave", pttUp);
  $("btn-ptt").addEventListener("touchstart", (e) => { e.preventDefault(); pttDown(); });
  $("btn-ptt").addEventListener("touchend", (e) => { e.preventDefault(); pttUp(); });
  $("btn-wake").onclick = toggleWake;
  $("btn-voice").onclick = toggleVoiceOut;
  $("btn-agent").onclick = toggleAgentMode;
  $("btn-agent-stop").onclick = () => {
    wsSend({ type: "agent_stop" });
    line("ln-sys", "· agent stop requested");
  };

  document.addEventListener("keydown", (e) => {
    if (e.code === "Space" && document.activeElement !== $("cmd")) {
      e.preventDefault();
      pttDown();
    }
  });
  document.addEventListener("keyup", (e) => {
    if (e.code === "Space" && document.activeElement !== $("cmd")) {
      e.preventDefault();
      pttUp();
    }
  });

  line("ln-sys", "· TRON-X A.V.E.N.G.E.R.S command center initialising…");
});
