/**
 * TRON-X Chat Module  v3
 * ───────────────────────
 * Streaming SSE → buffer text silently → reveal text + voice simultaneously.
 * Full response TTS — no snippet cap.
 */

const Chat = (() => {
  let sessionId    = localStorage.getItem('tronx_session') || null;
  let isProcessing = false;

  // ── Message rendering ─────────────────────────────────────────────────────

  function addMsg(role, text, meta = {}) {
    const box = document.getElementById('messages');
    if (!box) return;
    const wrap   = document.createElement('div');
    wrap.className = 'msg ' + role;
    const bubble = document.createElement('div');
    bubble.className = 'msg-bubble';
    if (role === 'user') { bubble.innerHTML = _escHtml(text); }
    else { _renderInto(bubble, text); }
    wrap.appendChild(bubble);
    const tag    = meta.tag || (role === 'user' ? 'YOU' : null);
    const intent = meta.intent ? meta.intent.toUpperCase() : '';
    const model  = meta.model  ? meta.model.split('/').pop() : '';
    if (tag || intent || model) {
      const el = document.createElement('div');
      el.className = 'msg-meta';
      el.textContent = [tag, intent, model].filter(Boolean).join(' · ');
      wrap.appendChild(el);
    }
    box.appendChild(wrap);
    box.scrollTop = box.scrollHeight;
    return wrap;
  }

  function _appendProcessingBubble() {
    const box  = document.getElementById('messages');
    const wrap = document.createElement('div');
    wrap.className = 'msg assistant streaming';
    const bubble = document.createElement('div');
    bubble.className = 'msg-bubble';
    bubble.innerHTML = '<span class="thinking-dots">Thinking</span>';
    wrap.appendChild(bubble);
    box.appendChild(wrap);
    box.scrollTop = box.scrollHeight;
    return { bubble, wrap };
  }

  function _setBubbleState(bubble, state) {
    const labels = {
      thinking:     '<span class="thinking-dots">Thinking</span>',
      composing:    '<span class="thinking-dots">Composing</span>',
      synthesising: '<span class="thinking-dots">Synthesising voice</span>',
    };
    if (labels[state]) bubble.innerHTML = labels[state];
  }

  function _addMetaLine(wrap, meta, fullText) {
    wrap.querySelector('.msg-meta')?.remove();
    const intent = meta.intent ? meta.intent.toUpperCase() : '';
    const model  = meta.model  ? meta.model.split('/').pop() : '';
    const parts  = [intent, model].filter(Boolean);
    const el = document.createElement('div');
    el.className = 'msg-meta';
    el.textContent = parts.join(' · ');

    // Phase 38: per-message speak button — voice is now on demand only
    if (fullText) {
      const spk = document.createElement('button');
      spk.className = 'msg-speak-btn';
      spk.title = 'Speak this reply';
      spk.textContent = '🔊';
      spk.addEventListener('click', () => {
        spk.disabled = true;
        const persona = document.body.dataset.persona || 'jarvis';
        Voice.speakAndReveal(fullText, persona,
          () => { if (window.Scene) Scene.setState('speaking'); },
          () => { spk.disabled = false; if (window.Scene) Scene.setState('idle'); });
      });
      el.appendChild(spk);
    }
    wrap.appendChild(el);

    // Phase 38: live-search citation chips
    if (meta.citations && meta.citations.length) {
      const srcRow = document.createElement('div');
      srcRow.className = 'msg-sources';
      const label = document.createElement('span');
      label.className = 'src-label';
      label.textContent = 'SOURCES';
      srcRow.appendChild(label);
      meta.citations.slice(0, 6).forEach((c) => {
        const a = document.createElement('a');
        a.className = 'src-chip';
        a.href = c.url || '#'; a.target = '_blank'; a.rel = 'noopener';
        a.textContent = `${c.index ?? ''} ${c.title || c.url || 'source'}`.trim().slice(0, 48);
        a.title = c.snippet || c.url || '';
        srcRow.appendChild(a);
      });
      wrap.appendChild(srcRow);
    }
    wrap.classList.remove('streaming');
  }

  // ── Markdown + HTML escape ────────────────────────────────────────────────

  function _markdownLite(text) {
    return _escHtml(text)
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/\*(.+?)\*/g, '<em>$1</em>')
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\n/g, '<br>');
  }

  /**
   * Phase 38: full rich rendering — proper Markdown (marked + DOMPurify when
   * loaded) and crisp KaTeX math so formulas never display as gibberish.
   */
  function _renderInto(el, text) {
    if (window.marked && window.DOMPurify) {
      marked.setOptions({ breaks: true, gfm: true, headerIds: false, mangle: false });
      el.innerHTML = DOMPurify.sanitize(marked.parse(text));
    } else {
      el.innerHTML = _markdownLite(text);
    }
    if (window.renderMathInElement) {
      try {
        window.renderMathInElement(el, {
          delimiters: [
            { left: '$$',  right: '$$',  display: true  },
            { left: '\\[', right: '\\]', display: true  },
            { left: '$',   right: '$',   display: false },
            { left: '\\(', right: '\\)', display: false },
          ],
          throwOnError: false,
        });
      } catch (e) { /* math render is best-effort */ }
    }
  }

  function _escHtml(s) {
    return String(s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  // ── Info panel ────────────────────────────────────────────────────────────

  function updateInfo(data) {
    const set = (id, val, cls) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.textContent = val;
      if (cls) el.className = 'val ' + cls;
    };
    if (data.model)               set('info-model',   data.model.split('/').pop(), 'ok');
    if (data.intent)              set('info-intent',  data.intent.toUpperCase());
    if (data.tokens_used != null) set('info-tokens',  data.tokens_used);
    if (data.latency_ms   != null) set('info-latency', data.latency_ms + 'ms');
  }

  // ── Main send (streaming + synced voice) ──────────────────────────────────

  async function send(text, persona = 'jarvis', intent = 'auto') {
    if (!text.trim() || isProcessing) return;
    isProcessing = true;

    const sendBtn = document.getElementById('send-btn');
    const input   = document.getElementById('msg-input');
    if (sendBtn) sendBtn.disabled = true;
    if (input)   input.disabled  = true;

    Scene.setState('thinking');
    _setStatus('PROCESSING', true);
    addMsg('user', text);

    const { bubble, wrap } = _appendProcessingBubble();
    let fullText = '';
    let metaInfo = {};

    try {
      // ── 1. Stream text silently ──────────────────────────────────────────
      const resp = await fetch('/api/chat/stream', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: text, session_id: sessionId,
          persona, intent, max_tokens: 2048,
        }),
      });
      if (!resp.ok) throw new Error('Server error ' + resp.status);

      _setBubbleState(bubble, 'composing');

      const reader  = resp.body.getReader();
      const decoder = new TextDecoder();
      let   buffer  = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const events = buffer.split('\n\n');
        buffer = events.pop();

        for (const raw of events) {
          const line = raw.trim();
          if (!line.startsWith('data:')) continue;
          const payload = line.slice(5).trim();
          if (payload === '[DONE]') break;
          let ev;
          try { ev = JSON.parse(payload); } catch { continue; }

          if (ev.type === 'meta') {
            metaInfo  = { intent: ev.intent, persona: ev.persona };
            sessionId = ev.session_id;
            localStorage.setItem('tronx_session', sessionId);
            _addMetaLine(wrap, metaInfo, '');
            modelUsed = ev.model || '';
          } else if (ev.type === 'text') {
            fullText += ev.content;
            const voiceMode = (typeof Voice !== 'undefined') && !Voice.isMuted();
            if (!voiceMode) {
                // Instantly update the bubble as text streams in
                wrap.classList.remove('streaming');
                _renderInto(bubble, fullText);
                const box = document.getElementById('messages');
                if (box) box.scrollTop = box.scrollHeight;
            }
          } else if (ev.type === 'done') {
            metaInfo.model       = ev.model || modelUsed;
            metaInfo.tokens_used = ev.tokens_used;
            metaInfo.latency_ms  = ev.latency_ms;
            metaInfo.citations   = ev.citations || [];
          } else if (ev.type === 'error') {
            fullText = '⚠ ' + ev.message;
            wrap.classList.remove('streaming');
            _renderInto(bubble, fullText);
          }
        }
      }

      // ── 2. Render metadata immediately ───────────────────────────────────
      _addMetaLine(wrap, metaInfo, fullText);
      updateInfo(metaInfo);

      // ── 3. Phase 38: text renders IMMEDIATELY; voice only in voice mode ──
      // Voice-output mode (topbar toggle / Ctrl+M) speaks replies; otherwise
      // the reply appears instantly and each message has its own 🔊 button.
      if (fullText) {
        const voiceMode = (typeof Voice !== 'undefined') && !Voice.isMuted();
        if (voiceMode) {
          _setBubbleState(bubble, 'synthesising');
          await Voice.speakAndReveal(
            fullText,
            persona,
            () => {
              _renderInto(bubble, fullText);
              const box = document.getElementById('messages');
              if (box) box.scrollTop = box.scrollHeight;
              Scene.setState('speaking');
            },
            () => { Scene.setState('idle'); }
          );
        } else {
          _renderInto(bubble, fullText);
          const box = document.getElementById('messages');
          if (box) box.scrollTop = box.scrollHeight;
          Scene.setState('idle');
        }
      } else {
        bubble.innerHTML = _markdownLite('…');
        Scene.setState('idle');
      }

      setTimeout(refreshHistory, 800);
      return metaInfo;

    } catch (err) {
      bubble.innerHTML = '⚠ ' + _escHtml(err.message || 'Connection error');
      wrap.querySelector('.msg-bubble')?.classList.add('error');
      Scene.setState('error');
      setTimeout(() => Scene.setState('idle'), 2000);
      return null;
    } finally {
      isProcessing = false;
      if (sendBtn) sendBtn.disabled = false;
      if (input)   input.disabled  = false;
      if (input)   input.focus();
      _setStatus('ONLINE', false);
    }
  }

  function _setStatus(label, thinking) {
    const dot = document.getElementById('status-dot');
    const txt = document.getElementById('status-text');
    if (thinking) dot?.classList.add('thinking');
    else          dot?.classList.remove('thinking');
    if (txt) txt.textContent = label;
  }

  // ── Providers & memory stats ──────────────────────────────────────────────

  async function loadProviders() {
    try {
      const data = await fetch('/api/providers').then(r => r.json());
      const list = document.getElementById('provider-list');
      const dot  = document.getElementById('status-dot');
      const txt  = document.getElementById('status-text');
      const configured = data.configured_providers || [];

      if (list) {
        list.innerHTML = '';

        // Summary row: X providers / Y models active
        const summary = document.createElement('div');
        summary.className = 'provider-summary';
        summary.innerHTML =
          '<span class="ps-count">' + configured.length + ' / ' + (data.total_providers || 14) + '</span>' +
          '<span class="ps-label"> providers active</span>';
        list.appendChild(summary);

        const modelSummary = document.createElement('div');
        modelSummary.className = 'provider-summary';
        modelSummary.innerHTML =
          '<span class="ps-count">' + (data.total_models || 104) + '</span>' +
          '<span class="ps-label"> open-source models</span>';
        list.appendChild(modelSummary);

        // Divider
        const hr = document.createElement('div');
        hr.className = 'provider-divider';
        list.appendChild(hr);

        // One row per active provider
        configured.forEach(p => {
          const row = document.createElement('div');
          row.className = 'provider-row';
          row.innerHTML =
            '<span class="provider-name">' + p.toUpperCase() + '</span>' +
            '<span class="provider-status ok">ONLINE</span>';
          list.appendChild(row);
        });

        // Show unconfigured providers dimmed
        const allProviders = ['groq','cerebras','openrouter','gemini','together_ai',
          'fireworks_ai','deepinfra','mistral','cohere','perplexity','deepseek',
          'huggingface','ollama'];
        allProviders.filter(p => !configured.includes(p)).forEach(p => {
          const row = document.createElement('div');
          row.className = 'provider-row offline';
          row.innerHTML =
            '<span class="provider-name">' + p.replace('_ai','').toUpperCase() + '</span>' +
            '<span class="provider-status offline">NO KEY</span>';
          list.appendChild(row);
        });
      }

      if (configured.length > 0) {
        dot?.classList.add('online');
        if (txt) txt.textContent = 'ONLINE';
      }
    } catch (e) { console.warn('[chat] Providers load failed:', e); }
  }

  async function loadMemoryStats() {
    try {
      const data  = await fetch('/api/memory/stats').then(r => r.json());
      const total = Object.values(data.chroma || {})
        .reduce((s, c) => s + (c.count || 0), 0);
      const el = document.getElementById('info-memory');
      if (el) el.textContent = total + ' chunks';
    } catch (e) {}
  }

  // ── Clear session ─────────────────────────────────────────────────────────

  async function clearSession() {
    if (sessionId) {
      await fetch('/api/chat/' + sessionId, { method: 'DELETE' }).catch(() => {});
    }
    sessionId = null;
    localStorage.removeItem('tronx_session');
    const box = document.getElementById('messages');
    if (box) box.innerHTML = '';
    addMsg('system', 'Session cleared. Ready for your next command.');
    Scene.setState('idle');
    setTimeout(refreshHistory, 400);
  }

  // ── Chat History Sidebar ──────────────────────────────────────────────────

  async function loadHistory()    { await refreshHistory(); }

  async function refreshHistory() {
    const panel = document.getElementById('history-list');
    if (!panel) return;
    try {
      const data     = await fetch('/api/chat/sessions').then(r => r.json());
      const sessions = data.sessions || [];
      panel.innerHTML = '';
      if (!sessions.length) {
        panel.innerHTML = '<div class="hist-empty">No history yet</div>';
        return;
      }
      sessions.forEach(s => {
        const item    = document.createElement('div');
        const isActive = s.id === sessionId;
        item.className = 'hist-item' + (isActive ? ' active' : '');
        item.dataset.sid = s.id;
        const ts      = new Date((s.updated_at || s.created_at || 0) * 1000);
        item.innerHTML =
          '<div class="hist-time">' + _fmtTime(ts) + '</div>' +
          '<div class="hist-preview">' + _escHtml((s.preview || '').slice(0, 60)) + '</div>';
        item.addEventListener('click', () => _loadSession(s.id));
        panel.appendChild(item);
      });
    } catch (e) {
      const p = document.getElementById('history-list');
      if (p) p.innerHTML = '<div class="hist-empty">─</div>';
    }
  }

  async function _loadSession(sid) {
    try {
      const data = await fetch('/api/chat/' + sid + '/history').then(r => r.json());
      sessionId  = sid;
      localStorage.setItem('tronx_session', sid);
      const box  = document.getElementById('messages');
      if (box) box.innerHTML = '';
      (data.messages || []).forEach(m => {
        if      (m.role === 'user')      addMsg('user',      m.content, { tag: 'YOU' });
        else if (m.role === 'assistant') addMsg('assistant', m.content, { intent: m.intent, model: m.model });
      });
      document.querySelectorAll('.hist-item').forEach(el => {
        el.classList.toggle('active', el.dataset.sid === sid);
      });
      if (box) box.scrollTop = box.scrollHeight;
    } catch (e) { console.warn('[chat] Load session failed:', e); }
  }

  function _fmtTime(date) {
    if (isNaN(date)) return '─';
    const diff = (Date.now() - date) / 1000;
    if (diff < 60)    return 'just now';
    if (diff < 3600)  return Math.floor(diff / 60)   + 'm ago';
    if (diff < 86400) return Math.floor(diff / 3600)  + 'h ago';
    const d = date.getDate(), m = date.getMonth() + 1;
    return (d < 10 ? '0' + d : d) + '/' + (m < 10 ? '0' + m : m);
  }

  // ── Phase 3: Live latency stats ──────────────────────────────────────────

  async function loadLatencyStats() {
    try {
      const data = await fetch('/api/models/stats').then(r => r.json());
      // best_p50_ms reflects the fastest warmed model; show it in the LATENCY field
      // only if no per-request latency has been set (i.e. latency field shows default)
      if (data.best_p50_ms != null) {
        const el = document.getElementById('info-latency');
        if (el && el.textContent === '─' || el && el.textContent === '') {
          el.textContent = data.best_p50_ms + 'ms';
        }
      }
    } catch (e) {}
  }

  return { send, addMsg, updateInfo, loadProviders, loadMemoryStats,
           loadLatencyStats, clearSession, loadHistory, refreshHistory };
})();
