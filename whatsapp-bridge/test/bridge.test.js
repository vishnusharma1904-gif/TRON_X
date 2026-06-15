'use strict';
const assert = require('assert');
const { createServer, normalizeJid, timingSafeEqualStr, checkAuth, extractInbound, parseEnvFile } = require('../server.js');
const fs = require('fs');
const path = require('path');

let pass = 0, fail = 0;
function ok(name, cond) { if (cond) { pass++; console.log('  PASS  ' + name); } else { fail++; console.log('  FAIL  ' + name); } }

// ---- pure helpers ----
console.log('\n=== helpers ===');
ok('normalizeJid strips punctuation', normalizeJid('+1 (415) 555-0100') === '14155550100@s.whatsapp.net');
ok('normalizeJid passes through JID', normalizeJid('123456@g.us') === '123456@g.us');
ok('normalizeJid rejects too-short', normalizeJid('123') === null);
ok('normalizeJid rejects empty', normalizeJid('') === null && normalizeJid(null) === null);
ok('timingSafeEqualStr equal', timingSafeEqualStr('abc', 'abc') === true);
ok('timingSafeEqualStr unequal', timingSafeEqualStr('abc', 'abd') === false);
ok('timingSafeEqualStr length-diff', timingSafeEqualStr('abc', 'abcd') === false);
ok('checkAuth no-token -> false', checkAuth({ headers: { authorization: 'Bearer x' } }, '') === false);
ok('checkAuth good', checkAuth({ headers: { authorization: 'Bearer s3cret' } }, 's3cret') === true);
ok('checkAuth bad', checkAuth({ headers: { authorization: 'Bearer nope' } }, 's3cret') === false);

const inText = extractInbound({ key: { id: 'M1', fromMe: false, remoteJid: '14155550100@s.whatsapp.net' }, pushName: 'Alice', messageTimestamp: 1700000000, message: { conversation: 'hello' } });
ok('extractInbound text body', inText.type === 'text' && inText.body === 'hello' && inText.from_me === false);
ok('extractInbound push_name', inText.push_name === 'Alice');
const inImg = extractInbound({ key: { id: 'M2', fromMe: true, remoteJid: '14155550100@s.whatsapp.net' }, message: { imageMessage: { caption: 'pic' } } });
ok('extractInbound image caption + fromMe', inImg.type === 'image' && inImg.body === 'pic' && inImg.from_me === true);

// ---- .env parsing / auto-load ----
const pe = parseEnvFile([
  '# comment line',
  'WHATSAPP_BRIDGE_TOKEN=abc123  ',
  'WHATSAPP_INGEST_URL=http://127.0.0.1:8000/api/whatsapp/bridge/ingest',
  'TOGETHER_API_KEY=            # https://together.ai',
  'BAD LINE NO EQUALS',
  '',
].join('\n'));
ok('parseEnvFile reads token (trimmed)', pe.WHATSAPP_BRIDGE_TOKEN === 'abc123');
ok('parseEnvFile reads url', pe.WHATSAPP_INGEST_URL === 'http://127.0.0.1:8000/api/whatsapp/bridge/ingest');
ok('parseEnvFile treats comment-only value as empty', pe.TOGETHER_API_KEY === '');
ok('parseEnvFile skips non KEY=VALUE lines', !('BAD LINE NO EQUALS' in pe));

// real project .env should now carry a 64-hex token the bridge will auto-load
const real = parseEnvFile(fs.readFileSync(path.join(__dirname, '..', '..', '.env'), 'utf8'));
ok('project .env has 64-hex bridge token', /^[0-9a-f]{64}$/.test(real.WHATSAPP_BRIDGE_TOKEN || ''));

// ---- HTTP layer with fake deps ----
const state = { connected: false, qr: 'QRDATA', me: null, lastError: null };
const deps = { token: 'secret123', getState: () => state, sendText: async (jid, text) => ({ id: 'BRIDGEID1' }) };
const server = createServer(deps);

function req(method, pathname, { token, body } = {}) {
  const port = server.address().port;
  const headers = {};
  if (token) headers['Authorization'] = 'Bearer ' + token;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  return fetch('http://127.0.0.1:' + port + pathname, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined })
    .then(async (r) => ({ status: r.status, json: await r.json().catch(() => ({})) }));
}

(async () => {
  await new Promise((res) => server.listen(0, '127.0.0.1', res));
  console.log('\n=== HTTP layer ===');

  let r = await req('GET', '/health');
  ok('health no-auth 200', r.status === 200 && r.json.ok === true && r.json.connected === false);

  r = await req('GET', '/status');
  ok('status without token -> 401', r.status === 401);
  r = await req('GET', '/status', { token: 'wrong' });
  ok('status wrong token -> 401', r.status === 401);
  r = await req('GET', '/status', { token: 'secret123' });
  ok('status with token -> 200 hasQr', r.status === 200 && r.json.hasQr === true && r.json.connected === false);

  r = await req('GET', '/qr', { token: 'secret123' });
  ok('qr returns raw string', r.status === 200 && r.json.qr === 'QRDATA');

  r = await req('POST', '/send', { token: 'secret123', body: { to: 'abc', message: 'hi' } });
  ok('send invalid recipient -> 400', r.status === 400 && r.json.error === 'invalid_recipient');
  r = await req('POST', '/send', { token: 'secret123', body: { to: '14155550100', message: '   ' } });
  ok('send empty message -> 400', r.status === 400 && r.json.error === 'empty_message');
  r = await req('POST', '/send', { token: 'secret123', body: { to: '14155550100', message: 'hi' } });
  ok('send while not connected -> 503', r.status === 503 && r.json.error === 'not_connected');

  state.connected = true;
  r = await req('POST', '/send', { token: 'secret123', body: { to: '+1 (415) 555-0100', message: 'hi there' } });
  ok('send success -> 200 id+jid', r.status === 200 && r.json.success === true && r.json.id === 'BRIDGEID1' && r.json.jid === '14155550100@s.whatsapp.net');
  r = await req('POST', '/send', { token: 'secret123' });
  ok('send no body -> 400 empty', r.status === 400);

  r = await req('GET', '/qr', { token: 'secret123' });
  ok('qr when connected -> 409', r.status === 409);

  r = await req('GET', '/nope', { token: 'secret123' });
  ok('unknown route -> 404', r.status === 404);

  // delivery confirmation: unconfirmed send -> 502 not_confirmed (no false positive)
  deps.sendText = async () => ({ id: 'Q1', delivered: false, status: 'timeout' });
  r = await req('POST', '/send', { token: 'secret123', body: { to: '14155550100', message: 'hi' } });
  ok('unconfirmed send -> 502 not_confirmed', r.status === 502 && r.json.error === 'not_confirmed' && !!r.json.hint);

  deps.sendText = async () => { throw new Error('not_on_whatsapp'); };
  r = await req('POST', '/send', { token: 'secret123', body: { to: '14155550100', message: 'hi' } });
  ok('not-on-whatsapp -> 422', r.status === 422 && r.json.error === 'not_on_whatsapp');

  deps.sendText = async () => ({ id: 'OK1', delivered: true, status: 2 });
  r = await req('POST', '/send', { token: 'secret123', body: { to: '14155550100', message: 'hi' } });
  ok('confirmed send -> 200 success', r.status === 200 && r.json.success === true && r.json.status === 2);

  server.close();
  console.log('\n==================== ' + pass + ' passed, ' + fail + ' failed ====================');
  process.exit(fail ? 1 : 0);
})();
