'use strict';
/**
 * TRON-X WhatsApp Bridge (Baileys)
 * Localhost-only HTTP service that sends/receives WhatsApp messages over the
 * multi-device WebSocket protocol (no browser). The Python app calls it with a
 * shared bearer token.
 *
 * Pure helpers + the request handler are exported for unit testing; the actual
 * WhatsApp socket is only started when this file is run directly.
 */
const http = require('http');
const crypto = require('crypto');
const path = require('path');
const fs = require('fs');

// Quieten the (very noisy, non-fatal) libsignal session logs — they dump huge
// Buffers via console.log/error on every session ratchet / Bad MAC. Real
// "[bridge] ..." logs are always kept.
(function quietLibsignal() {
  const NOISE = ['Closing session', 'Closing open session', 'SessionEntry',
    'Failed to decrypt message', 'Bad MAC', 'Session error', 'Removing old closed session',
    'No matching sessions', 'No session record'];
  const wrap = (orig) => function (...args) {
    try { if (args.length && NOISE.some((n) => String(args[0]).includes(n))) return; } catch (_) {}
    return orig.apply(console, args);
  };
  console.log = wrap(console.log.bind(console));
  console.error = wrap(console.error.bind(console));
  console.warn = wrap(console.warn.bind(console));
})();

// ---------------------------------------------------------------------------
// Config — resolves from process.env first, then the project .env file, then a
// default. This lets `npm start` work with just the token saved in ../.env, so
// you don't have to export environment variables by hand.
// ---------------------------------------------------------------------------
function parseEnvFile(text) {
  const out = {};
  for (const line of String(text).split(/\r?\n/)) {
    const t = line.trim();
    if (!t || t.startsWith('#')) continue;
    const eq = t.indexOf('=');
    if (eq < 0) continue;
    const key = t.slice(0, eq).trim();
    let val = t.slice(eq + 1).trim();
    if (val.startsWith('#')) val = '';   // whole value is a comment -> unset
    if (key) out[key] = val;
  }
  return out;
}

function loadProjectEnv() {
  const candidate = process.env.WHATSAPP_ENV_FILE || path.join(__dirname, '..', '.env');
  try {
    if (fs.existsSync(candidate)) return parseEnvFile(fs.readFileSync(candidate, 'utf8'));
  } catch (_) { /* ignore */ }
  return {};
}

const _fileEnv = loadProjectEnv();
function cfg(name, fallback) {
  const v = process.env[name];
  if (v !== undefined && v !== '') return v;
  if (_fileEnv[name] !== undefined && _fileEnv[name] !== '') return _fileEnv[name];
  return fallback;
}

const HOST = cfg('WHATSAPP_BRIDGE_HOST', '127.0.0.1');
const PORT = parseInt(cfg('WHATSAPP_BRIDGE_PORT', '8088'), 10);
const TOKEN = cfg('WHATSAPP_BRIDGE_TOKEN', '');
const AUTH_DIR = cfg('WHATSAPP_BRIDGE_AUTH_DIR', path.join(__dirname, 'auth'));
const INGEST_URL = cfg('WHATSAPP_INGEST_URL', '');
const MAX_BODY = 64 * 1024; // 64 KB request cap

// ---------------------------------------------------------------------------
// Pure helpers (exported, no I/O)
// ---------------------------------------------------------------------------
function normalizeJid(to) {
  if (to === undefined || to === null) return null;
  const s = String(to).trim();
  if (!s) return null;
  if (s.includes('@')) return s;                 // already a JID (user or @g.us group)
  const digits = s.replace(/\D/g, '');           // strip +, spaces, punctuation
  if (digits.length < 7 || digits.length > 15) return null;
  return digits + '@s.whatsapp.net';
}

function timingSafeEqualStr(a, b) {
  const ab = Buffer.from(String(a));
  const bb = Buffer.from(String(b));
  if (ab.length !== bb.length) return false;     // length leak is acceptable; values differ anyway
  return crypto.timingSafeEqual(ab, bb);
}

function bearerOf(req) {
  const h = req.headers['authorization'] || '';
  const m = /^Bearer\s+(.+)$/i.exec(h);
  return m ? m[1] : null;
}

function checkAuth(req, token) {
  if (!token) return false;                      // never authorize when no token configured
  const presented = bearerOf(req);
  if (!presented) return false;
  return timingSafeEqualStr(presented, token);
}

// ---------------------------------------------------------------------------
// Inbound extraction (Baileys message -> compact normalized record)
// ---------------------------------------------------------------------------
function extractInbound(m) {
  const msg = (m && m.message) || {};
  let type = 'unknown';
  let body = '';
  let mediaType = null;
  if (msg.conversation != null) { type = 'text'; body = msg.conversation; }
  else if (msg.extendedTextMessage) { type = 'text'; body = msg.extendedTextMessage.text || ''; }
  else if (msg.imageMessage) { type = 'image'; mediaType = 'image'; body = msg.imageMessage.caption || '[image]'; }
  else if (msg.videoMessage) { type = 'video'; mediaType = 'video'; body = msg.videoMessage.caption || '[video]'; }
  else if (msg.audioMessage) { type = 'audio'; mediaType = 'audio'; body = '[audio]'; }
  else if (msg.documentMessage) { type = 'document'; mediaType = 'document'; body = msg.documentMessage.caption || ('[' + (msg.documentMessage.fileName || 'document') + ']'); }
  else if (msg.stickerMessage) { type = 'sticker'; mediaType = 'sticker'; body = '[sticker]'; }
  else if (msg.locationMessage) { type = 'location'; body = '[location] ' + (msg.locationMessage.name || ''); }
  else if (msg.reactionMessage) { type = 'reaction'; body = '[reaction] ' + (msg.reactionMessage.text || ''); }
  else {
    const k = Object.keys(msg)[0] || 'unknown';
    type = k; body = '[' + k + ']';
  }
  const key = (m && m.key) || {};
  return {
    id: key.id || null,
    from_me: !!key.fromMe,
    jid: key.remoteJid || '',
    participant: key.participant || null,
    push_name: m.pushName || '',
    ts: Number(m.messageTimestamp) || 0,
    type,
    media: mediaType,
    body,
  };
}

// ---------------------------------------------------------------------------
// HTTP layer (testable: inject deps = { token, getState, sendText })
// ---------------------------------------------------------------------------
function sendJson(res, status, obj) {
  const data = JSON.stringify(obj);
  res.writeHead(status, { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data) });
  res.end(data);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on('data', (c) => {
      size += c.length;
      if (size > MAX_BODY) { reject(new Error('payload_too_large')); req.destroy(); return; }
      chunks.push(c);
    });
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

async function handle(req, res, deps) {
  const url = new URL(req.url, 'http://localhost');
  const route = req.method + ' ' + url.pathname;

  // Liveness — no auth, no secrets.
  if (route === 'GET /health') {
    const st = deps.getState();
    return sendJson(res, 200, { ok: true, connected: !!st.connected });
  }

  // Everything else requires the bearer token.
  if (!checkAuth(req, deps.token)) {
    return sendJson(res, 401, { success: false, error: 'unauthorized' });
  }

  if (route === 'GET /status') {
    const st = deps.getState();
    return sendJson(res, 200, {
      connected: !!st.connected,
      me: st.me || null,
      hasQr: !!st.qr,
      lastError: st.lastError || null,
    });
  }

  if (route === 'GET /qr') {
    const st = deps.getState();
    if (st.connected) return sendJson(res, 409, { success: false, error: 'already_linked' });
    if (!st.qr) return sendJson(res, 404, { success: false, error: 'no_qr_yet' });
    return sendJson(res, 200, { qr: st.qr });
  }

  if (route === 'GET /groups') {
    if (typeof deps.getGroups !== 'function') return sendJson(res, 501, { success: false, error: 'groups_unsupported' });
    try {
      const groups = await deps.getGroups();
      return sendJson(res, 200, { groups: groups || [] });
    } catch (e) {
      return sendJson(res, 502, { success: false, error: String((e && e.message) || e) });
    }
  }

  if (route === 'POST /send') {
    let payload;
    try {
      const raw = await readBody(req);
      payload = JSON.parse(raw || '{}');
    } catch (e) {
      const code = e.message === 'payload_too_large' ? 413 : 400;
      return sendJson(res, code, { success: false, error: e.message === 'payload_too_large' ? 'payload_too_large' : 'invalid_json' });
    }
    const jid = normalizeJid(payload.to);
    const message = typeof payload.message === 'string' ? payload.message : '';
    if (!jid) return sendJson(res, 400, { success: false, error: 'invalid_recipient' });
    if (!message.trim()) return sendJson(res, 400, { success: false, error: 'empty_message' });

    const st = deps.getState();
    if (!st.connected) return sendJson(res, 503, { success: false, error: 'not_connected' });

    try {
      const result = await deps.sendText(jid, message);
      const delivered = !result || result.delivered !== false;   // confirmed by WhatsApp?
      if (delivered) {
        return sendJson(res, 200, { success: true, id: (result && result.id) || null, jid,
                                    status: (result && result.status) || null });
      }
      return sendJson(res, 502, {
        success: false, error: 'not_confirmed', id: (result && result.id) || null, jid,
        status: (result && result.status) || null,
        hint: 'WhatsApp did not acknowledge the message within 12s. The linked-device session is likely '
            + 'degraded (Bad MAC in the logs). Re-link: stop the bridge, delete the auth/ folder, npm start, rescan the QR.',
      });
    } catch (e) {
      const msg = String(e && e.message || e);
      const isNoWa = msg === 'not_on_whatsapp';
      return sendJson(res, isNoWa ? 422 : 502, {
        success: false, error: isNoWa ? 'not_on_whatsapp' : 'send_failed', detail: msg,
      });
    }
  }

  return sendJson(res, 404, { success: false, error: 'not_found' });
}

function createServer(deps) {
  return http.createServer((req, res) => {
    handle(req, res, deps).catch((e) => {
      try { sendJson(res, 500, { success: false, error: 'internal', detail: String(e && e.message || e) }); } catch (_) {}
    });
  });
}

// ---------------------------------------------------------------------------
// Production wiring (only when run directly)
// ---------------------------------------------------------------------------
const state = { sock: null, connected: false, qr: null, me: null, lastError: null };

// Delivery confirmation: WhatsApp acks sent messages asynchronously via
// 'messages.update' (status >= SERVER_ACK means the server accepted it). We
// wait for that before reporting success, so a degraded session can't produce
// a false "sent".
const _STATUS = { ERROR: 0, PENDING: 1, SERVER_ACK: 2, DELIVERY_ACK: 3, READ: 4, PLAYED: 5 };
const _pendingAcks = new Map();   // wamid -> { resolve, timer }

function _waitForAck(id, timeoutMs) {
  return new Promise((resolve) => {
    const timer = setTimeout(() => { _pendingAcks.delete(id); resolve({ ok: false, status: 'timeout' }); }, timeoutMs);
    _pendingAcks.set(id, { resolve, timer });
  });
}
function _settleAck(id, status) {
  const p = _pendingAcks.get(id);
  if (!p) return;
  clearTimeout(p.timer);
  _pendingAcks.delete(id);
  p.resolve({ ok: status >= _STATUS.SERVER_ACK, status });
}

const silentLogger = (() => {
  const noop = () => {};
  const l = { level: 'silent', trace: noop, debug: noop, info: noop, warn: noop, error: noop, fatal: noop };
  l.child = () => l;
  return l;
})();

function renderQR(qr) {
  try {
    require('qrcode-terminal').generate(qr, { small: true });
  } catch (_) {
    console.log('[bridge] Scan this QR (or GET /qr). Raw string:\n' + qr);
  }
}

async function forwardInbound(ev) {
  if (!INGEST_URL || !ev || ev.type !== 'notify' || !Array.isArray(ev.messages)) return;
  const records = ev.messages.map(extractInbound).filter((r) => r.id && r.jid);
  if (!records.length) return;
  try {
    await fetch(INGEST_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + TOKEN },
      body: JSON.stringify({ messages: records }),
    });
  } catch (e) {
    console.error('[bridge] inbound forward failed:', e.message);
  }
}

async function startWhatsApp() {
  const {
    default: makeWASocket,
    useMultiFileAuthState,
    fetchLatestBaileysVersion,
    DisconnectReason,
  } = require('@whiskeysockets/baileys');

  const { state: authState, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  let version;
  try { ({ version } = await fetchLatestBaileysVersion()); } catch (_) { version = undefined; }

  const sock = makeWASocket({
    version,
    auth: authState,
    logger: silentLogger,
    printQRInTerminal: false,
    browser: ['TRON-X', 'Chrome', '1.0.0'],
    syncFullHistory: false,
  });
  state.sock = sock;

  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', (u) => {
    const { connection, lastDisconnect, qr } = u;
    if (qr) { state.qr = qr; renderQR(qr); }
    if (connection === 'open') {
      state.connected = true; state.qr = null; state.lastError = null; state.me = sock.user;
      console.log('[bridge] connected as', sock.user && sock.user.id);
    } else if (connection === 'close') {
      state.connected = false;
      const code = lastDisconnect && lastDisconnect.error && lastDisconnect.error.output
        ? lastDisconnect.error.output.statusCode : undefined;
      const loggedOut = code === DisconnectReason.loggedOut;
      if (loggedOut) {
        state.lastError = 'logged_out';
        console.error('[bridge] logged out — delete auth/ and re-link.');
      } else {
        console.warn('[bridge] connection closed (code ' + code + '); reconnecting in 3s...');
        setTimeout(() => { startWhatsApp().catch((e) => console.error('[bridge] reconnect failed:', e.message)); }, 3000);
      }
    }
  });

  sock.ev.on('messages.upsert', (ev) => { forwardInbound(ev).catch(() => {}); });

  sock.ev.on('messages.update', (updates) => {
    for (const u of (updates || [])) {
      const id = u && u.key && u.key.id;
      const status = u && u.update && u.update.status;
      if (id && typeof status === 'number') _settleAck(id, status);
    }
  });

  return sock;
}

let _groupsCache = { at: 0, list: [] };
async function realGetGroups() {
  if (!state.sock || !state.connected) throw new Error('not_connected');
  const now = Date.now();
  if (_groupsCache.list.length && (now - _groupsCache.at) < 60000) return _groupsCache.list;
  const map = await state.sock.groupFetchAllParticipating();
  const list = Object.values(map || {}).map((g) => ({
    id: g.id,
    subject: g.subject || '',
    size: Array.isArray(g.participants) ? g.participants.length : 0,
  }));
  _groupsCache = { at: now, list };
  return list;
}

async function realSendText(jid, text) {
  if (!state.sock || !state.connected) throw new Error('not_connected');

  // Groups (...@g.us) aren't "on WhatsApp" in the onWhatsApp sense — skip the
  // probe for them. For individuals, block only on a definitive "does not exist".
  if (!String(jid).endsWith('@g.us')) {
    try {
      const probe = await state.sock.onWhatsApp(jid);
      if (Array.isArray(probe) && probe.length && probe[0] && probe[0].exists === false) {
        throw new Error('not_on_whatsapp');
      }
    } catch (e) {
      if (e && e.message === 'not_on_whatsapp') throw e;
    }
  }

  const res = await state.sock.sendMessage(jid, { text });
  const id = res && res.key ? res.key.id : null;
  const initial = res && typeof res.status === 'number' ? res.status : undefined;
  if (!id) return { id: null, delivered: false, status: 'no_id' };
  if (initial !== undefined && initial >= _STATUS.SERVER_ACK) {
    return { id, delivered: true, status: initial };
  }
  const ack = await _waitForAck(id, 12000);   // wait up to 12s for server ack
  return { id, delivered: ack.ok, status: ack.status };
}

function acquireAuthLock() {
  const lockPath = path.join(AUTH_DIR, '.bridge.lock');
  try {
    fs.mkdirSync(AUTH_DIR, { recursive: true });
    if (fs.existsSync(lockPath)) {
      const pid = parseInt(String(fs.readFileSync(lockPath, 'utf8')).trim(), 10);
      if (pid && pid !== process.pid) {
        let alive = false;
        try { process.kill(pid, 0); alive = true; } catch (_) { alive = false; }
        if (alive) {
          console.error('FATAL: another bridge instance (pid ' + pid + ') is already using ' + AUTH_DIR + '.');
          console.error('Running two bridges on the same auth corrupts the WhatsApp session (Bad MAC). Close the other one first.');
          process.exit(1);
        }
      }
    }
    fs.writeFileSync(lockPath, String(process.pid));
    const cleanup = () => { try { if (String(fs.readFileSync(lockPath, 'utf8')).trim() === String(process.pid)) fs.unlinkSync(lockPath); } catch (_) {} };
    process.on('exit', cleanup);
    process.on('SIGINT', () => { cleanup(); process.exit(0); });
    process.on('SIGTERM', () => { cleanup(); process.exit(0); });
  } catch (e) {
    console.warn('[bridge] could not set auth lock:', e.message);
  }
}

if (require.main === module) {
  if (!TOKEN) {
    console.error('FATAL: WHATSAPP_BRIDGE_TOKEN is not set. Refusing to start.');
    process.exit(1);
  }
  acquireAuthLock();
  const server = createServer({ token: TOKEN, getState: () => state, sendText: realSendText, getGroups: realGetGroups });
  server.listen(PORT, HOST, () => {
    console.log('[bridge] listening on http://' + HOST + ':' + PORT + (INGEST_URL ? '  (inbound -> ' + INGEST_URL + ')' : ''));
  });
  startWhatsApp().catch((e) => { console.error('[bridge] startup failed:', e.message); process.exit(1); });
}

module.exports = { createServer, handle, normalizeJid, timingSafeEqualStr, checkAuth, extractInbound, parseEnvFile };
