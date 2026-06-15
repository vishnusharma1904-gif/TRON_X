/**
 * TRON-X App — Main initialiser
 * Boot sequence → module init → event wiring
 */

(async () => {

  // ── Boot sequence ──────────────────────────────────────────────────────────
  const overlay  = document.getElementById('boot-overlay');
  const bootLine = document.getElementById('boot-line');
  const bootProg = document.getElementById('boot-progress');

  const steps = [
    [10,  'LOADING CORE SYSTEMS...'],
    [25,  'INITIALISING NEURAL ROUTER...'],
    [45,  'CONNECTING TO PROVIDERS...'],
    [60,  'MOUNTING MEMORY CORE...'],
    [76,  'CALIBRATING VOICE ENGINE...'],
    [90,  'RENDERING HOLOGRAPHIC INTERFACE...'],
    [100, 'ALL SYSTEMS ONLINE'],
  ];

  for (const [pct, msg] of steps) {
    bootLine.textContent = msg;
    bootProg.style.width = pct + '%';
    await _sleep(pct === 100 ? 600 : 260);
  }
  overlay.classList.add('hidden');
  await _sleep(900);

  // ── Init Three.js scene ────────────────────────────────────────────────────
  Scene.init(document.getElementById('three-canvas'));
  Scene.setState('idle');

  // ── Init voice (wires mic button, mute button, fetches voice status) ───────
  Voice.init();

  // ── Load providers + memory stats ─────────────────────────────────────────
  await Promise.all([Chat.loadProviders(), Chat.loadMemoryStats()]);
  Chat.loadLatencyStats();

  // ── Clock ──────────────────────────────────────────────────────────────────
  function _tick() {
    const n = new Date();
    const el = document.getElementById('clock');
    if (el) el.textContent =
      String(n.getHours()).padStart(2,'0') + ':' +
      String(n.getMinutes()).padStart(2,'0') + ':' +
      String(n.getSeconds()).padStart(2,'0');
  }
  _tick();
  setInterval(_tick, 1000);

  // ── Refresh memory stats every 60s ────────────────────────────────────────
  setInterval(Chat.loadMemoryStats, 60000);
  // ── Refresh latency stats every 30s (Phase 3) ────────────────────────────
  setInterval(Chat.loadLatencyStats, 30000);

  // ── Event wiring ───────────────────────────────────────────────────────────
  const input      = document.getElementById('msg-input');
  const sendBtn    = document.getElementById('send-btn');
  const clearBtn   = document.getElementById('clear-btn');
  const personaSel = document.getElementById('persona-select');
  const personaBadge = document.getElementById('persona-badge');

  sendBtn.addEventListener('click', _sendText);
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); _sendText(); }
  });

  clearBtn.addEventListener('click', Chat.clearSession);

  const newChatBtn = document.getElementById('new-chat-btn');
  if (newChatBtn) newChatBtn.addEventListener('click', Chat.clearSession);

  personaSel.addEventListener('change', () => {
    const p = personaSel.value;
    const badge = personaBadge;
    if (badge) {
      badge.textContent = p.toUpperCase();
      badge.style.color       = p === 'friday' ? 'var(--purple)' : 'var(--cyan)';
      badge.style.borderColor = p === 'friday' ? 'rgba(180,79,255,0.4)' : 'var(--border)';
    }
  });

  // ── Welcome message ────────────────────────────────────────────────────────
  const persona = personaSel?.value || 'jarvis';
  const greet   = persona === 'friday' ? 'FRIDAY' : 'JARVIS';
  Chat.addMsg('system',
    greet + ' online. All systems operational. How may I assist you today?'
  );

  // Load existing session history on boot
  await Chat.loadHistory();

  // ── Input focus ────────────────────────────────────────────────────────────
  input?.focus();

  // ── Send helper ────────────────────────────────────────────────────────────
  async function _sendText() {
    const text    = input?.value.trim();
    const persona = personaSel?.value || 'jarvis';
    if (!text) return;
    input.value = '';
    await Chat.send(text, persona);
    Chat.loadMemoryStats();
    Chat.refreshHistory();
  }

})();

// Global sleep helper (also used by boot sequence)
function _sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
