/**
 * TRON-X NEXUS — Panel Controller v4
 *
 * Features:
 *  - Session sidebar: list, open, rename, delete
 *  - Auto-title from first message
 *  - Session history loading
 *  - Streaming chat with intent → card system
 *  - Emotion state badge (detected from user text via backend)
 *  - Telugu language badge (auto-detected)
 *  - Persona-reactive color theming (Jarvis=cyan, Friday=magenta)
 *  - Weather / Crypto / Stocks / News / System / IoT / Analytics cards
 *  - Voice PTT with Web Audio playback
 *  - Keyboard shortcuts
 */
(function () {
  'use strict';

  var API = '';
  var $   = function(id) { return document.getElementById(id); };

  // ── State ─────────────────────────────────────────────────────────────────
  var chatSession     = null;   // active session id
  var sessionList     = [];     // array of {id, title, updated_at, message_count, persona}
  var _audioEnabled   = true;   // ON by default — toggle in topbar to disable
  var _audioCtx       = null;
  var _audioQueue     = Promise.resolve();
  var _pttActive      = false;
  var _mediaRec       = null;
  var _audioChunks    = [];
  var _lastTranscript = '';
  var _renameTarget   = null;   // session id being renamed in modal
  var _cardPollTimer  = null;
  var _chatInProgress = false;  // suppress false OFFLINE during active streams

  _audioEnabled = false;
  var _academicMode = false;
  var _textMode = true;
  var _agentMode = false;
  var _voiceModeState = null;

  function renderMessageMarkup(targetEl, text) {
    if (!targetEl) return;
    targetEl.innerHTML = renderMarkdown(text);
    if (window.renderMathInElement) {
      try {
        window.renderMathInElement(targetEl, {
          delimiters: [
            {left: '$$', right: '$$', display: true},
            {left: '\\[', right: '\\]', display: true},
            {left: '$', right: '$', display: false},
            {left: '\\(', right: '\\)', display: false}
          ],
          throwOnError: false
        });
      } catch (e) {
        console.warn('[math] render failed:', e);
      }
    }
  }

  // ── Persona / Emotion / Telugu HUD functions ──────────────────────────────

  /** Switch body[data-persona] and topbar badge when AI persona changes */
  function setPersonaTheme(persona) {
    var p = (persona || 'jarvis').toLowerCase();
    document.body.setAttribute('data-persona', p);
    var badge = $('active-persona');
    if (badge) {
      badge.textContent = p.toUpperCase();
      if (p === 'friday') {
        badge.style.color       = '#ff00cc';
        badge.style.borderColor = 'rgba(255,0,204,0.4)';
        badge.style.textShadow  = '0 0 8px #ff00cc';
      } else {
        badge.style.color       = '#00e5ff';
        badge.style.borderColor = 'rgba(0,229,255,0.4)';
        badge.style.textShadow  = '0 0 8px #00e5ff';
      }
    }
  }

  /** Update the emotion badge in the topbar */
  function updateEmotionBadge(emotion, intensity) {
    var badge = $('emotion-badge');
    var label = $('emotion-label');
    if (!badge) return;
    var e = (emotion || 'neutral').toLowerCase();
    // Reset classes
    badge.className = 'emotion-badge';
    if (e === 'neutral' || !e) {
      return; // .neutral class means display:none by default
    }
    badge.classList.add(e, 'visible');
    if (label) {
      var icons = {
        frustrated: '⚡ FRUSTRATED',
        excited:    '★ EXCITED',
        confused:   '? CONFUSED',
        tired:      '~ TIRED',
        playful:    '♦ PLAYFUL',
        sad:        '♡ SAD',
        stressed:   '▲ STRESSED',
      };
      var pct = intensity != null ? ' ' + Math.round(intensity * 100) + '%' : '';
      label.textContent = (icons[e] || e.toUpperCase()) + pct;
    }
  }

  /** Show/hide the Telugu language badge */
  function updateTeluguBadge(dialect) {
    var badge = $('telugu-badge');
    var label = $('telugu-label');
    if (!badge) return;
    if (!dialect) {
      badge.classList.remove('visible');
      return;
    }
    badge.classList.add('visible');
    var dialectLabels = {
      telugu_script: 'తె SCRIPT',
      romanised:     'తె ROM',
      tenglish:      'TENGLISH',
      hyderabadi:    'HYD',
    };
    if (label) label.textContent = dialectLabels[dialect] || dialect.toUpperCase();
  }

  /** Reset emotion + Telugu badges (e.g., on new chat) */
  function resetBadges() {
    updateEmotionBadge('neutral');
    updateTeluguBadge(null);
  }

  // ── Search progress + citation rendering ─────────────────────────────────

  var _SEARCH_STEP_ICONS = {
    expanding:    '⟳',
    searching:    '⊕',
    reading:      '▤',
    ranking:      '◈',
    synthesising: '◎',
  };

  /**
   * Show/update a search-progress indicator inside a message bubble.
   * Replaces typing dots on first call, updates text on subsequent calls.
   */
  function updateSearchStatus(bubbleEl, ev) {
    if (!bubbleEl) return;
    var step = ev.step || '';
    var msg  = ev.message || '';
    var icon = _SEARCH_STEP_ICONS[step] || '•';
    var queries = ev.queries ? ev.queries.join('  ·  ') : '';

    // Create status container once
    var statusEl = bubbleEl.querySelector('.search-status');
    if (!statusEl) {
      bubbleEl.innerHTML = '';
      statusEl = document.createElement('div');
      statusEl.className = 'search-status';
      bubbleEl.appendChild(statusEl);
    }
    statusEl.innerHTML =
      '<span class="ss-icon">' + icon + '</span>' +
      '<span class="ss-msg">' + escHtml(msg) + '</span>' +
      (queries ? '<div class="ss-queries">' + escHtml(queries) + '</div>' : '');
  }

  function escHtml(s) {
    return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  /**
   * Render a citations block under the AI bubble.
   */
  function renderCitations(bubbleEl, citations) {
    if (!bubbleEl || !citations || !citations.length) return;
    var citEl = document.createElement('div');
    citEl.className = 'msg-citations';

    var hdr = document.createElement('div');
    hdr.className = 'citations-hdr';
    hdr.textContent = '◈ SOURCES';
    citEl.appendChild(hdr);

    citations.slice(0, 6).forEach(function(c) {
      var item = document.createElement('a');
      item.className = 'citation-item';
      item.href   = c.url || '#';
      item.target = '_blank';
      item.rel    = 'noopener noreferrer';
      item.innerHTML =
        '<span class="cite-num">[' + c.index + ']</span>' +
        '<span class="cite-title">' + escHtml(c.title || c.url || '') + '</span>' +
        '<span class="cite-snippet">' + escHtml((c.snippet || '').slice(0, 140)) + '</span>' +
        '<span class="cite-date">' + escHtml(c.date || '') + '</span>';
      citEl.appendChild(item);
    });

    bubbleEl.appendChild(citEl);
  }

  // ── Phase 4: Computer control HUD helpers ───────────────────────────────────

  /** Create or get the computer-feed container inside a bubble */
  function getComputerFeed(bubbleEl) {
    var feed = bubbleEl.querySelector('.computer-feed');
    if (!feed) {
      bubbleEl.innerHTML = '';           // clear typing indicator
      feed = document.createElement('div');
      feed.className = 'computer-feed';
      var header = document.createElement('div');
      header.className = 'cf-header';
      header.innerHTML = '<span class="cf-icon">⬡</span> <span class="cf-title">COMPUTER CONTROL</span>';
      feed.appendChild(header);
      var screenWrap = document.createElement('div');
      screenWrap.className = 'cf-screen-wrap';
      var screenImg = document.createElement('img');
      screenImg.className = 'cf-screen';
      screenImg.alt = 'Screen';
      screenWrap.appendChild(screenImg);
      feed.appendChild(screenWrap);
      var log = document.createElement('div');
      log.className = 'action-log';
      feed.appendChild(log);
      bubbleEl.appendChild(feed);
    }
    return feed;
  }

  /** Render a computer event step into the feed */
  function renderComputerEvent(bubbleEl, ev) {
    var feed      = getComputerFeed(bubbleEl);
    var screenImg = feed.querySelector('.cf-screen');
    var actionLog = feed.querySelector('.action-log');

    if (ev.screenshot && screenImg) {
      screenImg.src = 'data:image/jpeg;base64,' + ev.screenshot;
    }

    if (ev.type === 'computer_start') {
      var row = document.createElement('div');
      row.className = 'action-step as-info';
      row.textContent = '▶ Starting: ' + (ev.instruction || '');
      actionLog.appendChild(row);
    } else if (ev.type === 'computer_step') {
      var row = document.createElement('div');
      var phase = ev.phase || 'executing';
      row.className = 'action-step as-' + phase;
      var dot  = phase === 'result' ? (ev.success ? '✓' : '✗') :
                 phase === 'analysing' ? '◉' : '⟳';
      var desc = ev.description || ev.message || ev.action || phase;
      row.innerHTML =
        '<span class="as-dot">' + dot + '</span>' +
        '<span class="as-desc">' + escHtml(desc) + '</span>';
      if (ev.error) {
        var err = document.createElement('div');
        err.className = 'as-error';
        err.textContent = ev.error;
        row.appendChild(err);
      }
      actionLog.appendChild(row);
    } else if (ev.type === 'computer_done') {
      var row = document.createElement('div');
      row.className = 'action-step as-done';
      row.innerHTML =
        '<span class="as-dot">■</span>' +
        '<span class="as-desc">' + escHtml(ev.result || 'Done') + '</span>' +
        '<span class="as-latency"> ' + (ev.latency_ms || 0) + 'ms</span>';
      actionLog.appendChild(row);
    }
    actionLog.scrollTop = actionLog.scrollHeight;
    scrollToBottom();
  }

  // Wire persona select change to theme update
  var _personaSel = $('chat-persona');
  if (_personaSel) {
    _personaSel.addEventListener('change', function() {
      setPersonaTheme(_personaSel.value);
    });
  }
  // Initialize persona theme on load
  setPersonaTheme(_personaSel ? _personaSel.value : 'jarvis');

  // ── Helpers ───────────────────────────────────────────────────────────────
  function fmt(n, unit) {
    if (n === undefined || n === null) return '--';
    return typeof n === 'number' ? n.toFixed(1) + (unit || '') : String(n);
  }
  function fmtBytes(b) {
    if (!b) return '--';
    if (b > 1e9) return (b/1e9).toFixed(1)+' GB';
    if (b > 1e6) return (b/1e6).toFixed(1)+' MB';
    return (b/1e3).toFixed(0)+' KB';
  }
  function fmtNum(n) {
    if (n==null) return '--';
    if (n>=1e9) return (n/1e9).toFixed(2)+'B';
    if (n>=1e6) return (n/1e6).toFixed(1)+'M';
    if (n>=1e3) return (n/1e3).toFixed(1)+'K';
    return String(n);
  }
  function timeAgo(ts) {
    var diff = Date.now()/1000 - ts;
    if (diff < 60)   return 'just now';
    if (diff < 3600) return Math.floor(diff/60)+'m ago';
    if (diff < 86400)return Math.floor(diff/3600)+'h ago';
    return Math.floor(diff/86400)+'d ago';
  }
  function makeTable(rows) {
    var t = document.createElement('table');
    t.className = 'data-table';
    rows.forEach(function(r) {
      var tr = t.insertRow();
      tr.insertCell().textContent = r[0];
      var td = tr.insertCell(); td.textContent = r[1];
      if (r[2]) td.className = r[2];
    });
    return t;
  }
  function nowTime() {
    return new Date().toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'});
  }

  // ── Markdown ──────────────────────────────────────────────────────────────
  function renderMarkdown(text) {
    if (!text) return '';

    var mathBlocks = [];
    function stash(m) {
      mathBlocks.push(m);
      return '%%%MATH_BLOCK_' + (mathBlocks.length - 1) + '%%%';
    }

    // Extract $$ ... $$
    text = text.replace(/\$\$[\s\S]+?\$\$/g, stash);
    // Extract \[ ... \]
    text = text.replace(/\\\[[\s\S]+?\\\]/g, stash);
    // Extract \( ... \)
    text = text.replace(/\\\([\s\S]+?\\\)/g, stash);
    // Extract $ ... $
    text = text.replace(/(^|[^\\])\$([^\$\n]+?)\$/g, function(m, pre, math) {
      mathBlocks.push('$' + math + '$');
      return pre + '%%%MATH_BLOCK_' + (mathBlocks.length - 1) + '%%%';
    });

    if (window.marked && window.DOMPurify) {
      marked.setOptions({ breaks: true, gfm: true, headerIds: false, mangle: false });
      var rendered = marked.parse(text);
      var safe = DOMPurify.sanitize(rendered);

      // Restore math blocks
      for (var i = 0; i < mathBlocks.length; i++) {
        // HTML escape the restored math so it renders safely inside text nodes
        var safeMath = (mathBlocks[i] || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        safe = safe.replace('%%%MATH_BLOCK_' + i + '%%%', function() {
          return safeMath;
        });
      }
      return safe;
    }
    
    var html = text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    html = html.replace(/\n/g,'<br>');
    return '<p class="md-p">'+html+'</p>';
  }

  // ── Clock ─────────────────────────────────────────────────────────────────
  function tickClock() {
    var now = new Date();
    $('sys-time').textContent = now.toLocaleTimeString('en-GB',{hour12:false});
  }
  tickClock(); setInterval(tickClock,1000);

  // ── Connection probe ──────────────────────────────────────────────────────
  async function probe() {
    if (_chatInProgress) return;
    try {
      var r = await fetch(API+'/api/health',{signal:AbortSignal.timeout(8000)});
      if (r.ok) {
        $('conn-dot').classList.add('online');
        $('conn-label').textContent = 'ONLINE';
        $('status-pill').classList.add('online');
        return;
      }
    } catch(e) {}
    if (_chatInProgress) return;
    $('conn-dot').classList.remove('online');
    $('conn-label').textContent = 'OFFLINE';
    $('status-pill').classList.remove('online');
  }
  probe(); setInterval(probe,10000);

  // ── Audio ─────────────────────────────────────────────────────────────────
  function getAudioCtx() {
    if (!_audioCtx) _audioCtx = new (window.AudioContext||window.webkitAudioContext)();
    if (_audioCtx.state==='suspended') _audioCtx.resume();
    return _audioCtx;
  }
  function playChunk(b64, force) {
    if (!_audioEnabled && !force) return;
    _audioQueue = _audioQueue.then(async function() {
      try {
        var ctx = getAudioCtx();
        // decode base64 -> ArrayBuffer
        var bin = atob(b64);
        var arr = new Uint8Array(bin.length);
        for (var i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
        var buf;
        try {
          buf = await ctx.decodeAudioData(arr.buffer);
        } catch(decErr) {
          // MP3 decode can fail on first chunk (incomplete frame) — skip silently
          console.warn('[audio] decode skipped:', decErr.message);
          return;
        }
        var src = ctx.createBufferSource();
        src.buffer = buf;
        src.connect(ctx.destination);
        src.start(0);
        return new Promise(function(res) { src.onended = res; });
      } catch(e) { console.warn('[audio] playChunk error:', e); }
    });
  }

  // Audio toggle
  var _audioBtn = $('audio-toggle');
  _audioBtn.classList.add('active');  // ON by default
  _audioBtn.title = 'Audio ON — click to disable';
  _audioBtn.addEventListener('click', function() {
    _audioEnabled = !_audioEnabled;
    _audioBtn.classList.toggle('active', _audioEnabled);
    _audioBtn.title = _audioEnabled ? 'Audio ON — click to disable' : 'Audio OFF — click to enable';
    if (_audioEnabled) getAudioCtx();
  });

  // ── Keyboard shortcuts ────────────────────────────────────────────────────
  var _audioBtnReplacement = _audioBtn.cloneNode(true);
  _audioBtn.parentNode.replaceChild(_audioBtnReplacement, _audioBtn);
  _audioBtn = _audioBtnReplacement;
  function syncAudioButton() {
    _audioBtn.classList.toggle('active', _audioEnabled);
    _audioBtn.title = _audioEnabled ? 'Voice output mode ON' : 'Voice output mode OFF';
  }
  syncAudioButton();
  _audioBtn.addEventListener('click', async function() {
    _audioEnabled = !_audioEnabled;
    syncAudioButton();
    if (_audioEnabled) getAudioCtx();
    try {
      await fetch(API + '/api/voice/mode', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({voice_output_enabled: _audioEnabled}),
      });
    } catch (e) {}
  });

  var _academicBtn = $('academic-toggle');
  function syncAcademicButton() {
    if (!_academicBtn) return;
    _academicBtn.classList.toggle('active', _academicMode);
    _academicBtn.title = _academicMode ? 'Academic mode ON' : 'Academic mode OFF';
  }
  syncAcademicButton();
  if (_academicBtn) {
    _academicBtn.addEventListener('click', function() {
      _academicMode = !_academicMode;
      syncAcademicButton();
    });
  }

  var _textBtn = $('text-toggle');
  function syncTextButton() {
    if (!_textBtn) return;
    _textBtn.classList.toggle('active', _textMode);
    _textBtn.title = _textMode ? 'Text output ON' : 'Text output OFF';
    var msgs = $('chat-messages');
    if (msgs) {
      if (!_textMode) {
        msgs.classList.add('text-disabled');
      } else {
        msgs.classList.remove('text-disabled');
      }
    }
  }
  syncTextButton();
  if (_textBtn) {
    _textBtn.addEventListener('click', function() {
      _textMode = !_textMode;
      syncTextButton();
    });
  }

  var _agentBtn = $('agent-toggle');
  function syncAgentButton() {
    if (!_agentBtn) return;
    _agentBtn.classList.toggle('active', _agentMode);
    _agentBtn.title = _agentMode ? 'Agent mode ON' : 'Agent mode OFF';
  }
  syncAgentButton();
  if (_agentBtn) {
    _agentBtn.addEventListener('click', function() {
      _agentMode = !_agentMode;
      syncAgentButton();
    });
  }

  document.addEventListener('keydown', function(e) {
    if (e.key==='Escape') hideCard();
    if ((e.ctrlKey||e.metaKey) && e.key==='b') { e.preventDefault(); toggleSidebar(); }
    if ((e.ctrlKey||e.metaKey) && e.key==='l') { e.preventDefault(); $('chat-input').focus(); }
    if ((e.ctrlKey||e.metaKey) && e.key==='n') { e.preventDefault(); createNewChat(); }
  });

  // ── Sidebar toggle ────────────────────────────────────────────────────────
  // On small phone screens, start with the sidebar collapsed so it doesn't
  // eat the whole layout — it's an absolute overlay at this breakpoint anyway.
  var _sidebarOpen = !window.matchMedia('(max-width: 520px)').matches;
  if (!_sidebarOpen) $('sidebar').classList.add('collapsed');
  function toggleSidebar() {
    _sidebarOpen = !_sidebarOpen;
    $('sidebar').classList.toggle('collapsed', !_sidebarOpen);
  }
  $('sidebar-toggle').addEventListener('click', toggleSidebar);

  // Tapping outside an open overlay sidebar on small screens closes it.
  document.addEventListener('click', function(e) {
    if (!window.matchMedia('(max-width: 680px)').matches) return;
    if (!_sidebarOpen) return;
    var sb = $('sidebar');
    if (sb.contains(e.target) || e.target.closest('#sidebar-toggle')) return;
    toggleSidebar();
  });

  // ── Welcome screen ────────────────────────────────────────────────────────
  function showWelcome(show) {
    $('welcome-screen').classList.toggle('hidden', !show);
  }

  // Hint chips send preset messages
  document.querySelectorAll('.hint-chip').forEach(function(chip) {
    chip.addEventListener('click', function() {
      var hints = {
        '💬 Ask anything': '',
        '🌦 Weather in Tokyo': 'What\'s the weather in Tokyo?',
        '₿ Bitcoin price': 'What is the Bitcoin price?',
        '📈 AAPL stock': 'Show me AAPL stock',
        '🖥 System stats': 'Show system stats',
        '📰 Latest news': 'Show me latest news',
      };
      var msg = hints[chip.textContent] || chip.textContent;
      if (msg) { $('chat-input').value = msg; $('chat-input').focus(); }
    });
  });

  // ════════════════════════════════════════════════════════════════════════════
  // SESSION MANAGEMENT
  // ════════════════════════════════════════════════════════════════════════════

  async function loadSessions() {
    try {
      var r = await fetch(API+'/api/chat/sessions');
      if (!r.ok) return;
      var d = await r.json();
      sessionList = d.sessions || [];
      renderSessionList();
    } catch(e) {
      $('sessions-list').innerHTML = '<div class="sessions-loading">Could not load sessions</div>';
    }
  }

  function renderSessionList() {
    var list = $('sessions-list');
    if (!sessionList.length) {
      list.innerHTML = '<div class="sessions-empty">No previous chats</div>';
      return;
    }
    list.innerHTML = '';
    sessionList.forEach(function(s) {
      var item = document.createElement('div');
      item.className = 'session-item' + (s.id===chatSession ? ' active' : '');
      item.setAttribute('data-id', s.id);

      var body = document.createElement('div');
      body.className = 'session-item-body';

      var titleEl = document.createElement('div');
      titleEl.className = 'session-item-title';
      titleEl.textContent = s.title || s.preview || 'New Chat';

      var meta = document.createElement('div');
      meta.className = 'session-item-meta';
      meta.innerHTML = '<span>'+timeAgo(s.updated_at)+'</span>'
        +'<span class="meta-dot"></span>'
        +'<span>'+(s.message_count||0)+' msgs</span>';

      body.appendChild(titleEl);
      body.appendChild(meta);

      var actions = document.createElement('div');
      actions.className = 'session-actions';

      var renameBtn = document.createElement('button');
      renameBtn.className = 'session-action-btn';
      renameBtn.title = 'Rename';
      renameBtn.innerHTML = '<svg width="11" height="11" viewBox="0 0 12 12" fill="none"><path d="M8.5 1.5L10.5 3.5L4 10H2V8L8.5 1.5Z" stroke="currentColor" stroke-width="1.2" fill="none"/></svg>';
      renameBtn.addEventListener('click', function(e) {
        e.stopPropagation();
        openRenameModal(s.id, s.title || s.preview || 'New Chat');
      });

      var delBtn = document.createElement('button');
      delBtn.className = 'session-action-btn del-btn';
      delBtn.title = 'Delete';
      delBtn.innerHTML = '<svg width="10" height="10" viewBox="0 0 10 10" fill="none"><line x1="1" y1="1" x2="9" y2="9" stroke="currentColor" stroke-width="1.4"/><line x1="9" y1="1" x2="1" y2="9" stroke="currentColor" stroke-width="1.4"/></svg>';
      delBtn.addEventListener('click', function(e) {
        e.stopPropagation();
        deleteSession(s.id, item);
      });

      actions.appendChild(renameBtn);
      actions.appendChild(delBtn);
      item.appendChild(body);
      item.appendChild(actions);

      item.addEventListener('click', function() { openSession(s.id); });
      list.appendChild(item);
    });
  }

  async function openSession(id) {
    if (id===chatSession) return;
    chatSession = id;

    // Update active state
    document.querySelectorAll('.session-item').forEach(function(el){
      el.classList.toggle('active', el.getAttribute('data-id')===id);
    });

    // Update topbar title
    var s = sessionList.find(function(x){return x.id===id;});
    if (s) {
      $('session-title').textContent = s.title || s.preview || 'New Chat';
      $('active-persona').textContent = (s.persona||'jarvis').toUpperCase();
    }

    // Load messages
    var msgArea = $('chat-messages');
    msgArea.innerHTML = '<div class="history-loading"><div class="history-spinner"></div></div>';
    showWelcome(false);

    try {
      var r = await fetch(API+'/api/chat/'+id+'/history');
      if (!r.ok) throw new Error('HTTP '+r.status);
      var d = await r.json();
      var msgs = d.messages || [];

      msgArea.innerHTML = '';
      if (!msgs.length) {
        showWelcome(true);
      } else {
        msgs.forEach(function(m, i) {
          appendMsg(m.role==='user' ? 'user' : 'ai', m.content, null, i*30);
        });
        scrollToBottom();
      }
    } catch(e) {
      msgArea.innerHTML = '';
      showWelcome(true);
    }

    // Update persona select
    if (s && s.persona) $('chat-persona').value = s.persona;
  }

  function createNewChat() {
    chatSession = null;
    $('session-title').textContent = 'New Chat';
    $('chat-messages').innerHTML = '';
    showWelcome(true);
    resetBadges();
    $('active-persona').textContent = $('chat-persona').value.toUpperCase();
    // Clear active state in sidebar
    document.querySelectorAll('.session-item').forEach(function(el){
      el.classList.remove('active');
    });
    $('chat-input').focus();
  }

  $('new-chat-btn').addEventListener('click', createNewChat);

  // ── Rename modal ──────────────────────────────────────────────────────────
  function openRenameModal(id, currentTitle) {
    _renameTarget = id;
    $('rename-modal-input').value = currentTitle;
    $('rename-modal').style.display = 'flex';
    setTimeout(function(){
      $('rename-modal-input').focus();
      $('rename-modal-input').select();
    }, 50);
  }

  function closeRenameModal() {
    $('rename-modal').style.display = 'none';
    _renameTarget = null;
  }

  async function saveRename() {
    if (!_renameTarget) return;
    var newTitle = $('rename-modal-input').value.trim();
    if (!newTitle) return;
    try {
      var r = await fetch(API+'/api/chat/'+_renameTarget+'/title', {
        method: 'PATCH',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({title: newTitle}),
      });
      if (r.ok) {
        // Update local state
        var s = sessionList.find(function(x){return x.id===_renameTarget;});
        if (s) s.title = newTitle;
        // Update topbar if this is active session
        if (_renameTarget===chatSession) $('session-title').textContent = newTitle;
        renderSessionList();
      }
    } catch(e) {}
    closeRenameModal();
  }

  $('rename-modal-cancel').addEventListener('click', closeRenameModal);
  $('rename-modal-save').addEventListener('click', saveRename);
  $('rename-modal-input').addEventListener('keydown', function(e){
    if (e.key==='Enter') saveRename();
    if (e.key==='Escape') closeRenameModal();
  });
  $('rename-modal').addEventListener('click', function(e){
    if (e.target===$('rename-modal')) closeRenameModal();
  });

  // Topbar session title rename
  $('rename-title-btn').addEventListener('click', function(){
    if (!chatSession) return;
    var s = sessionList.find(function(x){return x.id===chatSession;});
    openRenameModal(chatSession, s ? s.title||s.preview||'New Chat' : 'New Chat');
  });
  $('session-title').addEventListener('click', function(){
    if (!chatSession) return;
    var s = sessionList.find(function(x){return x.id===chatSession;});
    openRenameModal(chatSession, s ? s.title||s.preview||'New Chat' : 'New Chat');
  });

  // ── Delete session ────────────────────────────────────────────────────────
  async function deleteSession(id, itemEl) {
    if (!confirm('Delete this chat?')) return;
    try {
      var r = await fetch(API+'/api/chat/'+id+'/delete', {method:'DELETE'});
      if (r.ok || r.status===404) {
        sessionList = sessionList.filter(function(s){return s.id!==id;});
        if (itemEl) {
          itemEl.style.transition = 'opacity 0.2s, transform 0.2s';
          itemEl.style.opacity = '0';
          itemEl.style.transform = 'translateX(-20px)';
          setTimeout(function(){ renderSessionList(); }, 220);
        } else {
          renderSessionList();
        }
        if (id===chatSession) createNewChat();
      }
    } catch(e) {}
  }

  // ════════════════════════════════════════════════════════════════════════════
  // CHAT MESSAGES
  // ════════════════════════════════════════════════════════════════════════════

  function scrollToBottom() {
    var el = $('chat-messages');
    el.scrollTop = el.scrollHeight;
  }

  function appendMsg(role, text, model, delay) {
    var wrap = document.createElement('div');
    wrap.className = 'chat-msg msg-'+role;
    if (delay) wrap.style.animationDelay = delay+'ms';

    var meta = document.createElement('div');
    meta.className = 'msg-meta';

    var lbl = document.createElement('span');
    lbl.className = 'msg-label-'+role;
    lbl.textContent = role==='user' ? 'YOU' : $('chat-persona').value.toUpperCase();

    var timeEl = document.createElement('span');
    timeEl.className = 'msg-time mono';
    timeEl.textContent = nowTime();

    meta.appendChild(lbl);
    meta.appendChild(timeEl);

    var bubble = document.createElement('div');
    bubble.className = 'msg-bubble';

    if (text==='...') {
      bubble.innerHTML = '<div class="typing-dots"><span class="typing-dot"></span><span class="typing-dot"></span><span class="typing-dot"></span></div>';
    } else {
      if (role==='user') bubble.textContent = text;
      else renderMessageMarkup(bubble, text);
    }

    wrap.appendChild(meta);
    wrap.appendChild(bubble);

    if (model) {
      var tag = document.createElement('div');
      tag.className = 'msg-model-tag mono';
      tag.textContent = model;
      wrap.appendChild(tag);
    }

    $('chat-messages').appendChild(wrap);
    scrollToBottom();
    return bubble;
  }

  function saveEpisode(userMsg, aiReply, sessionId) {
    if (!userMsg || !aiReply || aiReply.length < 5) return;
    fetch(API+'/api/memory/episodic/remember', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({user_msg:userMsg, assistant_reply:aiReply,
                            session_id:sessionId||'default', auto_extract:true}),
    }).catch(function(){});
  }

  // ── Send chat ─────────────────────────────────────────────────────────────



  // TTS: speak every AI text reply aloud
  async function speakReply(text, persona) {
    if (!text || !text.trim()) return;
    var clean = text
      .replace(/```[\s\S]*?```/g, '')
      .replace(/`[^`]+`/g, '')
      .replace(/#{1,6}\s/g, '')
      .replace(/\*{1,3}([^*]+)\*{1,3}/g, '$1')
      .replace(/\[([^\]]+)\]\([^\)]+\)/g, '$1')
      .replace(/\n{2,}/g, '. ')
      .replace(/\n/g, ' ')
      .trim();
    if (!clean) return;
    try {
      var resp = await fetch(API + '/api/voice/tts/stream', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text: clean, persona: persona || $('chat-persona').value}),
      });
      if (!resp.ok) return;
      var reader = resp.body.getReader(), dec = new TextDecoder();
      var streamDone = false;
      while (!streamDone) {
        var ch = await reader.read();
        if (ch.done) break;
        var lines = dec.decode(ch.value).split('\n');
        for (var i = 0; i < lines.length; i++) {
          var line = lines[i];
          if (!line.startsWith('data:')) continue;
          var raw = line.slice(5).trim();
          if (raw === '[DONE]') {
            streamDone = true;
            break;
          }
          try {
            var ev = JSON.parse(raw);
            if (ev.type === 'audio_chunk' && ev.audio_b64) playChunk(ev.audio_b64, true);
            else if (ev.type === 'done' || ev.type === 'error') streamDone = true;
          } catch(e2) {}
        }
      }
    } catch(e) { console.warn('[tts] speakReply error:', e); }
  }

  var _chatFiles = [];
  var _attachBtn = $('chat-attach-btn');
  var _fileInput = $('chat-file-input');
  var _filesStage = $('chat-files-stage');

  if (_attachBtn && _fileInput) {
    _attachBtn.addEventListener('click', function() { _fileInput.click(); });
    _fileInput.addEventListener('change', function(e) {
      for (var i=0; i<e.target.files.length; i++) _chatFiles.push(e.target.files[i]);
      _fileInput.value = '';
      _renderChatFiles();
    });
  }

  function _renderChatFiles() {
    if (!_filesStage) return;
    _filesStage.innerHTML = '';
    if (!_chatFiles.length) { _filesStage.style.display = 'none'; return; }
    _filesStage.style.display = 'flex';
    _chatFiles.forEach(function(f, i) {
      var badge = document.createElement('div');
      badge.className = 'file-badge';
      badge.innerHTML = '<span>' + escHtml(f.name) + '</span><button data-idx="' + i + '">&times;</button>';
      badge.querySelector('button').addEventListener('click', function() {
        _chatFiles.splice(i, 1);
        _renderChatFiles();
      });
      _filesStage.appendChild(badge);
    });
  }

  async function sendChat() {
    var input   = $('chat-input');
    var persona = $('chat-persona').value;
    var text    = input.value.trim();
    if (!text && !_chatFiles.length) return;
    input.value = '';
    input.disabled = true;
    $('chat-send').disabled = true;
    _chatInProgress = true;

    showWelcome(false);

    var displayMsg = text;
    if (_chatFiles.length) {
      displayMsg += ' <span style="opacity:0.6;font-size:0.8em">[' + _chatFiles.length + ' attachment(s)]</span>';
    }
    appendMsg('user', displayMsg || 'Attached files');
    var bubbleEl = appendMsg('ai', '...');

    var intent = detectIntent(text);
    if (intent) triggerCard(intent);

    var msgToSend = text || "Analyze the attached files.";
    if (intent && intent.type==='system') {
      var sc = await fetchSysCtx();
      if (sc) msgToSend = msgToSend+sc;
    }

    var attachmentsPayload = null;
    if (_chatFiles.length) {
      try {
        var fd = new FormData();
        _chatFiles.forEach(function(f) { fd.append('files', f); });
        var uRes = await fetch(API+'/api/attachments/process?full=true', {
          method: 'POST', body: fd
        });
        if (uRes.ok) {
          var uData = await uRes.json();
          attachmentsPayload = uData.attachments;
        }
      } catch (err) {
        console.warn('Attachment upload failed:', err);
      }
      _chatFiles = [];
      _renderChatFiles();
    }

    var fullText = '', modelUsed = '', isNew = false;
    try {
      var bodyObj = {
        message: msgToSend, 
        session_id: chatSession,
        persona: persona, 
        stream: true, 
        academic_mode: _academicMode,
        agent_mode: _agentMode
      };
      if (attachmentsPayload) bodyObj.attachments = attachmentsPayload;

      var resp = await fetch(API+'/api/chat/stream', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify(bodyObj),
      });

      if (!resp.ok) {
        bubbleEl.innerHTML = '<span style="color:var(--red)">[ERROR] Server '+resp.status+'</span>';
        return;
      }

      var reader = resp.body.getReader(), dec = new TextDecoder();
      var streamDone = false;

      while (!streamDone) {
        var ch = await reader.read();
        if (ch.done) break;
        var lines = dec.decode(ch.value).split('\n');
        for (var i=0; i<lines.length; i++) {
          var line = lines[i];
          if (!line.startsWith('data:')) continue;
          var raw = line.slice(5).trim();
          if (raw==='[DONE]') {
            streamDone = true;
            break;
          }
          try {
            var ev = JSON.parse(raw);
            if (ev.type==='meta') {
              if (ev.session_id) chatSession = ev.session_id;
              if (ev.persona) setPersonaTheme(ev.persona);
              if (typeof ev.voice_output_enabled === 'boolean') {
                _audioEnabled = ev.voice_output_enabled;
                syncAudioButton();
              }
              if (typeof ev.academic_mode === 'boolean') {
                _academicMode = ev.academic_mode;
                syncAcademicButton();
              }
            } else if (ev.type==='computer_start' || ev.type==='computer_step' || ev.type==='computer_done') {
              // Phase 4: computer control events
              renderComputerEvent(bubbleEl, ev);
            } else if (ev.type==='search_progress') {
              // Phase 3: live search status widget inside the bubble
              updateSearchStatus(bubbleEl, ev);
              scrollToBottom();
            } else if (ev.type==='text') {
              // Clear any search status indicator when LLM text starts flowing
              if (bubbleEl.querySelector('.search-status')) {
                bubbleEl.innerHTML = '';
              }
              fullText += ev.content||ev.delta||'';
              renderMessageMarkup(bubbleEl, fullText);
              scrollToBottom();
            } else if (ev.type==='done') {
              if (ev.reply && !fullText) {
                renderMessageMarkup(bubbleEl, ev.reply);
                fullText = ev.reply;
              }
              // Clear any lingering search status
              var ss = bubbleEl.querySelector('.search-status');
              if (ss && !fullText) ss.remove();
              if (ev.model) { $('chat-model').textContent = ev.model; modelUsed=ev.model; }
              if (ev.persona) setPersonaTheme(ev.persona);
              if (ev.session_id) chatSession = ev.session_id;
              if (typeof ev.voice_output_enabled === 'boolean') {
                _audioEnabled = ev.voice_output_enabled;
                syncAudioButton();
              }
              if (typeof ev.academic_mode === 'boolean') {
                _academicMode = ev.academic_mode;
                syncAcademicButton();
              }
              // Phase 2: emotion + Telugu HUD badges
              if (ev.emotion !== undefined) updateEmotionBadge(ev.emotion, ev.emotion_intensity);
              if (ev.telugu !== undefined) updateTeluguBadge(ev.telugu);
              // Phase 3: render citations below the bubble
              if (ev.citations && ev.citations.length) {
                renderCitations(bubbleEl, ev.citations);
              }
              streamDone = true;
            } else if (ev.type==='error') {
              bubbleEl.innerHTML = '<span style="color:var(--red)">[ERROR] '+(ev.message||'Unknown')+'</span>';
              streamDone = true;
            }
          } catch(e) {}
        }
      }

      if (bubbleEl.innerHTML.includes('typing-dot')) bubbleEl.textContent = '(no response)';

      isNew = !sessionList.find(function(s){return s.id===chatSession;});

    } catch(e) {
      bubbleEl.innerHTML = '<span style="color:var(--red)">[ERROR] '+e.message+'</span>';
    } finally {
      _chatInProgress = false;
      input.disabled = false;
      $('chat-send').disabled = false;
      input.focus();
      probe();
    }

    // Post-stream work — must not block the input or falsely show OFFLINE
    if (fullText && _audioEnabled) speakReply(fullText, $('chat-persona').value).catch(function(){});
    saveEpisode(text, fullText, chatSession);
    loadSessions().then(function() {
      if (isNew || !$('session-title').textContent || $('session-title').textContent==='New Chat') {
        var ss = sessionList.find(function(s){return s.id===chatSession;});
        if (ss && ss.title) $('session-title').textContent = ss.title;
      }
      document.querySelectorAll('.session-item').forEach(function(el){
        el.classList.toggle('active', el.getAttribute('data-id')===chatSession);
      });
    }).catch(function(){});
  }

  $('chat-send').addEventListener('click', sendChat);
  $('chat-input').addEventListener('keydown', function(e){
    if (e.key==='Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
  });

  // Auto-resize textarea as user types
  function autoResize(el) {
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 160) + 'px';
  }
  $('chat-input').addEventListener('input', function() { autoResize(this); });
  // Reset height after send
  var _origSendChat = sendChat;
  sendChat = async function() {
    await _origSendChat();
    var el = $('chat-input');
    el.style.height = 'auto';
  };

  // ── System context ────────────────────────────────────────────────────────
  async function fetchSysCtx() {
    try {
      var r = await fetch(API+'/api/system/info',{signal:AbortSignal.timeout(2000)});
      if (!r.ok) return '';
      var d = await r.json();
      var cpu  = d.cpu_percent  ?? d.cpu;
      var ram  = d.ram_used_pct ?? d.memory_percent ?? d.ram;
      var disk = d.disk_used_pct?? d.disk_percent   ?? d.disk;
      var parts=[];
      if (cpu!=null)  parts.push('CPU='+cpu.toFixed(1)+'%');
      if (ram!=null)  parts.push('RAM='+ram.toFixed(1)+'%');
      if (disk!=null) parts.push('Disk='+disk.toFixed(1)+'%');
      if (d.temperature!=null) parts.push('Temp='+d.temperature.toFixed(1)+'C');
      return parts.length ? '\n\n[TRON-X DATA: '+parts.join(', ')+']' : '';
    } catch(e){ return ''; }
  }

  // ════════════════════════════════════════════════════════════════════════════
  // CARD SYSTEM
  // ════════════════════════════════════════════════════════════════════════════

  function showCard(title, renderFn, pollMs) {
    if (_cardPollTimer) { clearInterval(_cardPollTimer); _cardPollTimer=null; }
    $('card-title').textContent = title;
    renderFn();
    $('info-card-area').classList.add('visible');
    if (pollMs) _cardPollTimer = setInterval(renderFn, pollMs);
  }
  function hideCard() {
    if (_cardPollTimer) { clearInterval(_cardPollTimer); _cardPollTimer=null; }
    $('info-card-area').classList.remove('visible');
  }

  $('card-close').addEventListener('click', hideCard);

  // ── Weather ───────────────────────────────────────────────────────────────
  async function renderWeather(loc) {
    var body=$('card-body');
    body.innerHTML='<div class="feed-loading">FETCHING WEATHER...</div>';
    try {
      var r=await fetch(API+'/api/feeds/weather/current?location='+encodeURIComponent(loc||'London'));
      if(!r.ok) throw new Error('HTTP '+r.status);
      var d=await r.json();
      var tempVal = d.temp??d.temperature;
      body.innerHTML='';
      var big=document.createElement('div'); big.className='card-big-val';
      big.textContent = tempVal!=null ? tempVal.toFixed(1)+'°C' : '--';
      var lbl=document.createElement('div'); lbl.className='card-big-label';
      lbl.textContent = ((d.location||d.city||loc||'?')+(d.country?', '+d.country:'')).toUpperCase();
      body.appendChild(big); body.appendChild(lbl);
      body.appendChild(makeTable([
        ['Condition', d.description||d.conditions||'--'],
        ['Feels like', d.feels_like!=null?d.feels_like.toFixed(1)+'°C':'--'],
        ['Humidity',   d.humidity!=null?d.humidity+'%':'--'],
        ['Wind',       d.wind_speed!=null?d.wind_speed.toFixed(1)+' m/s':'--'],
        ['Pressure',   d.pressure!=null?d.pressure+' hPa':'--'],
        ['Visibility', d.visibility!=null?(d.visibility/1000).toFixed(1)+' km':'--'],
      ]));
    } catch(e) { body.innerHTML='<div class="feed-loading">[ERROR] '+e.message+'</div>'; }
  }

  // ── Crypto ────────────────────────────────────────────────────────────────
  async function renderCrypto(coin) {
    var body=$('card-body');
    body.innerHTML='<div class="feed-loading">FETCHING MARKET DATA...</div>';
    try {
      var r=await fetch(API+'/api/feeds/crypto/market?coin='+encodeURIComponent(coin||'bitcoin'));
      if(!r.ok) throw new Error('HTTP '+r.status);
      var d=await r.json();
      var price=d.price_usd!=null?'$'+d.price_usd.toLocaleString(undefined,{maximumFractionDigits:6}):'--';
      var chg24=d.change_24h, chg7=d.change_7d;
      body.innerHTML='';
      var big=document.createElement('div'); big.className='card-big-val'; big.textContent=price;
      var lbl=document.createElement('div'); lbl.className='card-big-label';
      lbl.textContent=(d.name||d.id||coin||'').toUpperCase()+(d.symbol?' ('+d.symbol+')':'');
      body.appendChild(big); body.appendChild(lbl);
      body.appendChild(makeTable([
        ['24h Change', chg24!=null?(chg24>=0?'+':'')+chg24.toFixed(2)+'%':'--', chg24!=null?(chg24>=0?'card-positive':'card-negative'):''],
        ['7d Change',  chg7!=null?(chg7>=0?'+':'')+chg7.toFixed(2)+'%':'--', chg7!=null?(chg7>=0?'card-positive':'card-negative'):''],
        ['Market Cap', d.market_cap?'$'+fmtNum(d.market_cap):'--'],
        ['Volume 24h', d.volume_24h?'$'+fmtNum(d.volume_24h):'--'],
        ['ATH', d.ath?'$'+d.ath.toLocaleString(undefined,{maximumFractionDigits:4}):'--'],
        ['Supply', d.circulating_supply?fmtNum(d.circulating_supply)+' '+(d.symbol||''):'--'],
      ]));
    } catch(e) { body.innerHTML='<div class="feed-loading">[ERROR] '+e.message+'</div>'; }
  }

  // ── Stocks ────────────────────────────────────────────────────────────────
  async function renderStocks(sym) {
    var body=$('card-body');
    body.innerHTML='<div class="feed-loading">FETCHING QUOTE...</div>';
    try {
      var r=await fetch(API+'/api/feeds/stocks/quote?symbol='+encodeURIComponent(sym||'AAPL'));
      if(!r.ok) throw new Error('HTTP '+r.status);
      var d=await r.json();
      var chg=d.change;
      body.innerHTML='';
      var big=document.createElement('div'); big.className='card-big-val';
      big.textContent=d.price!=null?'$'+d.price.toFixed(2):'--';
      var lbl=document.createElement('div'); lbl.className='card-big-label';
      lbl.textContent=(d.name||d.symbol||sym||'').toUpperCase();
      lbl.style.fontSize='9px';
      body.appendChild(big); body.appendChild(lbl);
      body.appendChild(makeTable([
        ['Change', chg!=null?(chg>=0?'+':'')+chg.toFixed(2)+' ('+(d.change_pct||0).toFixed(2)+'%)':'--', chg!=null?(chg>=0?'card-positive':'card-negative'):''],
        ['Open',   d.open!=null?'$'+d.open.toFixed(2):'--'],
        ['High',   d.high!=null?'$'+d.high.toFixed(2):'--'],
        ['Low',    d.low!=null?'$'+d.low.toFixed(2):'--'],
        ['52W Hi', d['52w_high']!=null?'$'+d['52w_high'].toFixed(2):'--'],
        ['52W Lo', d['52w_low']!=null?'$'+d['52w_low'].toFixed(2):'--'],
        ['Vol',    d.volume?d.volume.toLocaleString():'--'],
        ['MktCap', d.market_cap?'$'+fmtNum(d.market_cap):'--'],
        ['P/E',    d.pe_ratio!=null?d.pe_ratio.toFixed(2):'--'],
        ['State',  d.market_state||'--'],
      ]));
    } catch(e) { body.innerHTML='<div class="feed-loading">[ERROR] '+e.message+'</div>'; }
  }

  // ── News ──────────────────────────────────────────────────────────────────
  async function renderNews(topic) {
    var body=$('card-body');
    body.innerHTML='<div class="feed-loading">FETCHING NEWS...</div>';
    try {
      var url=API+'/api/feeds/news/headlines?count=8';
      if (topic) url+='&topic='+encodeURIComponent(topic);
      var r=await fetch(url);
      if(!r.ok) throw new Error('HTTP '+r.status);
      var d=await r.json();
      var articles=d.articles||d.headlines||[];
      body.innerHTML='';
      if (!articles.length){body.innerHTML='<div class="feed-loading">NO RESULTS</div>';return;}
      articles.forEach(function(a){
        var item=document.createElement('div'); item.className='news-item';
        var src=document.createElement('span'); src.className='news-src'; src.textContent=a.source||'';
        var t=document.createElement('span'); t.className='news-title'; t.textContent=a.title||a.headline||'';
        if(a.url){item.style.cursor='pointer';item.addEventListener('click',function(){window.open(a.url,'_blank');});}
        item.appendChild(src); item.appendChild(t); body.appendChild(item);
      });
    } catch(e) { body.innerHTML='<div class="feed-loading">[ERROR] '+e.message+'</div>'; }
  }

  // ── System ────────────────────────────────────────────────────────────────
  async function renderSystem() {
    var body=$('card-body');
    try {
      var r=await fetch(API+'/api/system/info');
      if(!r.ok){body.innerHTML='<div class="feed-loading">UNAVAILABLE</div>';return;}
      var d=await r.json();
      var cpu  = d.cpu_percent  ??d.cpu  ??null;
      var ram  = d.ram_used_pct ??d.memory_percent??d.ram??null;
      var disk = d.disk_used_pct??d.disk_percent  ??d.disk??null;
      function bar(pct){
        var v=Math.min(100,Math.max(0,pct||0));
        var col=v>80?'#ff3366':v>60?'#ff9900':'var(--cyan)';
        return '<div class="bar-track"><div class="bar-fill" style="width:'+v+'%;background:'+col+'"></div></div>';
      }
      body.innerHTML='<div class="sys-grid">'
        +'<div class="sys-stat"><div class="sys-label">CPU</div><div class="sys-val">'+fmt(cpu,'%')+'</div>'+bar(cpu)+'</div>'
        +'<div class="sys-stat"><div class="sys-label">RAM</div><div class="sys-val">'+fmt(ram,'%')+'</div>'+bar(ram)+'</div>'
        +'<div class="sys-stat"><div class="sys-label">DISK</div><div class="sys-val">'+fmt(disk,'%')+'</div>'+bar(disk)+'</div>'
        +'<div class="sys-stat"><div class="sys-label">TEMP</div><div class="sys-val">'+(d.temperature!=null?d.temperature.toFixed(0)+'°':'N/A')+'</div></div>'
        +'</div>';
      var net=d.network||{};
      body.appendChild(makeTable([
        ['Net Sent', net.bytes_sent?fmtBytes(net.bytes_sent):'--'],
        ['Net Recv', net.bytes_recv?fmtBytes(net.bytes_recv):'--'],
      ]));
    } catch(e){}
  }

  // ── Self-Healing / Diagnostics (Phase 28) ──────────────────────────────────
  async function renderHealth() {
    var body=$('card-body');
    body.innerHTML='<div class="feed-loading">LOADING DIAGNOSTICS...</div>';
    try {
      var sr=await fetch(API+'/api/system/health/status');
      if(!sr.ok) throw new Error('HTTP '+sr.status);
      var s=await sr.json();
      var dr=await fetch(API+'/api/system/health/diagnostics?limit=1');
      var d=dr.ok?await dr.json():{entries:[]};
      var last=(d.entries&&d.entries.length)?d.entries[d.entries.length-1]:null;
      var res=(last&&last.resources)||{};

      function bar(pct){
        var v=Math.min(100,Math.max(0,pct||0));
        var col=v>80?'#ff3366':v>60?'#ff9900':'var(--cyan)';
        return '<div class="bar-track"><div class="bar-fill" style="width:'+v+'%;background:'+col+'"></div></div>';
      }
      body.innerHTML='<div class="sys-grid">'
        +'<div class="sys-stat"><div class="sys-label">CPU</div><div class="sys-val">'+fmt(res.cpu_percent,'%')+'</div>'+bar(res.cpu_percent)+'</div>'
        +'<div class="sys-stat"><div class="sys-label">RAM</div><div class="sys-val">'+fmt(res.ram_used_pct,'%')+'</div>'+bar(res.ram_used_pct)+'</div>'
        +'<div class="sys-stat"><div class="sys-label">DISK</div><div class="sys-val">'+fmt(res.disk_used_pct,'%')+'</div>'+bar(res.disk_used_pct)+'</div>'
        +'</div>';

      var rh=s.router_health||{};
      var tripped=rh.tripped_models||[];
      var heal=s.self_healing||{};
      var bias=heal.bias||{active:false};

      body.appendChild(makeTable([
        ['Self-Healing',   heal.enabled ? 'ENABLED ('+heal.interval_sec+'s)' : 'DISABLED'],
        ['Tripped Models', String(tripped.length)],
        ['Chain Bias',     bias.active ? (bias.biased_model+' ('+bias.remaining_s+'s)') : 'inactive'],
        ['Last Check',     last ? new Date(last.ts*1000).toLocaleTimeString() : '--'],
      ]));

      if (tripped.length) {
        var tDiv=document.createElement('div');
        tDiv.style.cssText='font-size:9px;color:var(--red);margin-top:8px;word-break:break-all;';
        tDiv.textContent='DEGRADED: '+tripped.join(', ');
        body.appendChild(tDiv);
      }

      var actions=(last&&last.actions)||[];
      var aTitle=document.createElement('div');
      aTitle.style.cssText='font-size:9px;color:var(--text-dim);margin-top:10px;text-transform:uppercase;letter-spacing:1px;';
      aTitle.textContent='Recent Actions';
      body.appendChild(aTitle);

      if (actions.length) {
        actions.forEach(function(a){
          var el=document.createElement('div');
          el.style.cssText='font-size:9px;color:var(--text);padding:3px 0;border-bottom:1px solid var(--border);';
          el.textContent='• '+a;
          body.appendChild(el);
        });
      } else {
        var none=document.createElement('div');
        none.style.cssText='font-size:9px;color:var(--green);margin-top:4px;';
        none.textContent='All systems nominal';
        body.appendChild(none);
      }
    } catch(e) {
      body.innerHTML='<div class="feed-loading">UNAVAILABLE</div>';
    }
  }

  // ── IoT ───────────────────────────────────────────────────────────────────
  async function renderIoT() {
    var body=$('card-body');
    body.innerHTML='<div class="feed-loading">SCANNING DEVICES...</div>';
    try {
      var r=await fetch(API+'/api/iot/summary',{signal:AbortSignal.timeout(5000)});
      if(!r.ok) throw new Error('not configured');
      var summary=await r.json();
      var lr=await fetch(API+'/api/iot/lights',{signal:AbortSignal.timeout(4000)});
      var lights=lr.ok?await lr.json():[];
      body.innerHTML='';
      var devs=Array.isArray(lights)?lights:(lights.lights||lights.entities||[]);
      if(!devs.length){body.innerHTML='<div class="feed-loading">NO DEVICES</div>';return;}
      var grid=document.createElement('div'); grid.className='iot-grid';
      devs.slice(0,8).forEach(function(dev){
        var eid=dev.entity_id||dev.id||'', state=(dev.state||'').toLowerCase(), isOn=state==='on';
        var card=document.createElement('div'); card.className='iot-device'+(isOn?' on':'');
        var nm=document.createElement('div'); nm.className='iot-dev-name'; nm.textContent=(dev.name||dev.friendly_name||eid).replace(/_/g,' ');
        var st=document.createElement('div'); st.className='iot-dev-state'+(isOn?' on':''); st.textContent=state.toUpperCase();
        card.appendChild(nm); card.appendChild(st);
        card.addEventListener('click',async function(){
          try{await fetch(API+(isOn?'/api/iot/turn_off/':'/api/iot/turn_on/')+encodeURIComponent(eid),{method:'POST'});renderIoT();}catch(e){}
        });
        grid.appendChild(card);
      });
      body.appendChild(grid);
    } catch(e) { body.innerHTML='<div class="feed-loading">NOT CONFIGURED<br><span style="font-size:9px;opacity:.5">Add HA_URL+HA_TOKEN to .env</span></div>'; }
  }

  // ── Analytics ─────────────────────────────────────────────────────────────
  async function renderAnalytics() {
    var body=$('card-body');
    body.innerHTML='<div class="feed-loading">LOADING STATS...</div>';
    try {
      var r=await fetch(API+'/api/analytics/summary?days=7');
      if(!r.ok) throw new Error('HTTP '+r.status);
      var d=await r.json();
      body.innerHTML='';
      var grid=document.createElement('div'); grid.className='analytics-grid';
      function addStat(label,value){
        var el=document.createElement('div'); el.className='analytics-stat';
        var v=document.createElement('div'); v.className='analytics-val'; v.textContent=value??'--';
        var l=document.createElement('div'); l.className='analytics-label'; l.textContent=label;
        el.appendChild(v); el.appendChild(l); grid.appendChild(el);
      }
      addStat('REQUESTS', d.requests??d.total_requests!=null?fmtNum(d.requests??d.total_requests):'--');
      addStat('CHATS',    d.chats??d.total_chats!=null?fmtNum(d.chats??d.total_chats??d.chat_calls):'--');
      addStat('SESSIONS', d.sessions!=null?fmtNum(d.sessions??d.unique_sessions):'--');
      addStat('ERRORS',   d.errors!=null?String(d.errors??d.total_errors):'--');
      body.appendChild(grid);
      var note=document.createElement('div');
      note.style.cssText='font-size:9px;color:var(--text-dim);text-align:center;margin-top:8px;';
      note.textContent='LAST 7 DAYS'; body.appendChild(note);
      try {
        var cr=await fetch(API+'/api/analytics/chat?days=7');
        if(cr.ok){
          var cd=await cr.json();
          var bi=cd.by_intent||cd.intents||{};
          var top=Object.entries(bi).sort(function(a,b){return (b[1].count||b[1])-(a[1].count||a[1]);}).slice(0,5);
          if(top.length){
            var hdr=document.createElement('div'); hdr.style.cssText='font-size:9px;color:var(--cyan);letter-spacing:.1em;margin:10px 0 4px;border-top:1px solid var(--border);padding-top:8px;'; hdr.textContent='TOP INTENTS'; body.appendChild(hdr);
            body.appendChild(makeTable(top.map(function(kv){return [kv[0].toUpperCase(), String(typeof kv[1]==='object'?kv[1].count||0:kv[1])];})));
          }
        }
      } catch(e){}
    } catch(e) { body.innerHTML='<div class="feed-loading">[ERROR] '+e.message+'</div>'; }
  }

  // ── Cost & Usage Dashboard (Phase 34) ──────────────────────────────────────
  async function renderCostDashboard() {
    var body=$('card-body');
    body.innerHTML='<div class="feed-loading">LOADING COST DATA...</div>';
    try {
      var r=await fetch(API+'/api/analytics/dashboard?period=7d');
      if(!r.ok) throw new Error('HTTP '+r.status);
      var d=await r.json();
      body.innerHTML='';

      var big=document.createElement('div'); big.className='card-big-val';
      big.textContent='$'+(d.total_cost_usd!=null?d.total_cost_usd.toFixed(4):'0.0000');
      var lbl=document.createElement('div'); lbl.className='card-big-label';
      lbl.textContent='ESTIMATED COST -- '+(d.period||'7D').toUpperCase();
      body.appendChild(big); body.appendChild(lbl);

      body.appendChild(makeTable([
        ['Total Tokens',   d.total_tokens!=null?fmtNum(d.total_tokens):'--'],
        ['Total Calls',    d.total_calls!=null?String(d.total_calls):'--'],
        ['Cache Hit Rate', d.cache_hit_rate!=null?(d.cache_hit_rate*100).toFixed(1)+'%':'N/A'],
      ]));

      var providers=Object.entries(d.by_provider||{}).sort(function(a,b){return b[1].cost_usd-a[1].cost_usd;});
      if(providers.length){
        var hdr=document.createElement('div');
        hdr.style.cssText='font-size:9px;color:var(--cyan);letter-spacing:.1em;margin:10px 0 4px;border-top:1px solid var(--border);padding-top:8px;';
        hdr.textContent='BY PROVIDER'; body.appendChild(hdr);
        body.appendChild(makeTable(providers.map(function(kv){
          var p=kv[1];
          return [kv[0].toUpperCase()+(p.free_tier?' (free)':''), '$'+p.cost_usd.toFixed(4)];
        })));
      }

      var models=Object.entries(d.by_model||{}).sort(function(a,b){return b[1].cost_usd-a[1].cost_usd;}).slice(0,5);
      if(models.length){
        var hdr2=document.createElement('div');
        hdr2.style.cssText='font-size:9px;color:var(--cyan);letter-spacing:.1em;margin:10px 0 4px;border-top:1px solid var(--border);padding-top:8px;';
        hdr2.textContent='TOP MODELS'; body.appendChild(hdr2);
        body.appendChild(makeTable(models.map(function(kv){
          return [kv[0], '$'+kv[1].cost_usd.toFixed(4)+' ('+kv[1].calls+'x)'];
        })));
      }

      var cbEvents=d.circuit_breaker_events||[];
      if(cbEvents.length){
        var hdr3=document.createElement('div');
        hdr3.style.cssText='font-size:9px;color:var(--red);letter-spacing:.1em;margin:10px 0 4px;border-top:1px solid var(--border);padding-top:8px;';
        hdr3.textContent='CIRCUIT BREAKER EVENTS'; body.appendChild(hdr3);
        cbEvents.slice(-3).forEach(function(ev){
          var el=document.createElement('div');
          el.style.cssText='font-size:9px;color:var(--text);padding:3px 0;border-bottom:1px solid var(--border);';
          el.textContent=new Date(ev.ts*1000).toLocaleTimeString()+' -- '+(ev.tripped_models||[]).join(', ');
          body.appendChild(el);
        });
      }

      if(d.unpriced_models && d.unpriced_models.length){
        var warn=document.createElement('div');
        warn.style.cssText='font-size:9px;color:var(--text-dim);margin-top:8px;word-break:break-all;';
        warn.textContent='Unpriced (counted as $0): '+d.unpriced_models.join(', ');
        body.appendChild(warn);
      }

      var note=document.createElement('div');
      note.style.cssText='font-size:9px;color:var(--text-dim);text-align:center;margin-top:8px;';
      note.textContent='Estimates only -- pricing as of '+(d.pricing_last_updated||'unknown');
      body.appendChild(note);
    } catch(e) { body.innerHTML='<div class="feed-loading">[ERROR] '+e.message+'</div>'; }
  }

  // ── History card ──────────────────────────────────────────────────────────
  async function renderHistoryCard() {
    var body=$('card-body');
    body.innerHTML='<div class="feed-loading">LOADING HISTORY...</div>';
    try {
      var r=await fetch(API+'/api/memory/episodic/episodes?days=30&limit=100');
      if(!r.ok) throw new Error('HTTP '+r.status);
      var d=await r.json(); var episodes=d.episodes||[];
      body.innerHTML='';
      if(!episodes.length){body.innerHTML='<div class="feed-loading">NO HISTORY YET</div>';return;}
      var byDate={};
      episodes.forEach(function(ep){
        var date=ep.date||ep.created_at||'Unknown';
        if(typeof date==='number') date=new Date(date*1000).toISOString().slice(0,10);
        if(!byDate[date]) byDate[date]=[];
        byDate[date].push(ep);
      });
      Object.keys(byDate).sort().reverse().forEach(function(date){
        var hdr=document.createElement('div'); hdr.className='hist-date-hdr'; hdr.textContent=date; body.appendChild(hdr);
        byDate[date].forEach(function(ep){
          var item=document.createElement('div'); item.className='hist-item'; item.setAttribute('data-expanded','false');
          var top=document.createElement('div'); top.className='hist-item-top';
          var tp=document.createElement('span'); tp.className='hist-topic'; tp.textContent=(ep.topic||'general').toUpperCase();
          var sm=document.createElement('span'); sm.className='hist-summary'; sm.textContent=ep.summary||ep.user_msg||'(no summary)';
          var emoMap={positive:'+',negative:'-',neutral:'~'};
          var emo=document.createElement('span'); emo.className='hist-emotion hist-emo-'+(ep.emotion||'neutral'); emo.textContent=emoMap[ep.emotion]||'~';
          top.appendChild(tp); top.appendChild(sm); top.appendChild(emo); item.appendChild(top);
          var det=document.createElement('div'); det.className='hist-detail'; det.style.display='none';
          if(ep.user_msg){var u=document.createElement('div');u.className='hist-you';u.innerHTML='<span class="hist-role">YOU</span> '+ep.user_msg.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');det.appendChild(u);}
          var rep=ep.assistant||ep.assistant_reply||'';
          if(rep){var a=document.createElement('div');a.className='hist-ai';a.innerHTML='<span class="hist-role">AI</span> '+rep.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');det.appendChild(a);}
          item.appendChild(det);
          item.addEventListener('click',function(){var ex=item.getAttribute('data-expanded')==='true';det.style.display=ex?'none':'block';item.setAttribute('data-expanded',ex?'false':'true');});
          body.appendChild(item);
        });
      });
    } catch(e){ body.innerHTML='<div class="feed-loading">[ERROR] '+e.message+'</div>'; }
  }

  // ── Intent detection ──────────────────────────────────────────────────────
  var CRYPTO_MAP={bitcoin:'bitcoin',btc:'bitcoin',ethereum:'ethereum',eth:'ethereum',
    dogecoin:'dogecoin',doge:'dogecoin',solana:'solana',sol:'solana',bnb:'binancecoin',
    xrp:'ripple',cardano:'cardano',ada:'cardano',litecoin:'litecoin',ltc:'litecoin',
    polkadot:'polkadot',dot:'polkadot',avalanche:'avalanche-2',avax:'avalanche-2'};
  var SHORT_C=['btc','eth','sol','bnb','ada','xrp','dot','ltc','avax'];
  var LONG_C =['bitcoin','ethereum','dogecoin','solana','cardano','litecoin','polkadot','ripple','binancecoin','avalanche'];
  var TBL={AI:1,HUD:1,SSE:1,API:1,URL:1,PTT:1,CPU:1,RAM:1,GPU:1,OK:1,NO:1,TV:1,PC:1,UI:1,THE:1,AT:1,IS:1,IN:1,OR:1,IT:1};
  function wbt(w,t){var i=t.indexOf(w);while(i!==-1){var b=i===0||!/[a-z]/.test(t[i-1]),a=i+w.length>=t.length||!/[a-z]/.test(t[i+w.length]);if(b&&a)return true;i=t.indexOf(w,i+1);}return false;}

  // A "clear ask" — the user is requesting something, not just mentioning a
  // word in passing. Used to gate the weather/stocks/news popup cards so they
  // don't pop up for every casual mention of those keywords.
  var ASK_RE = /\b(what'?s|whats|how'?s|hows|how is|how are|check|show( me)?|get( me)?|give me|tell me|fetch|find|search|display|pull up|look up|current|latest|today'?s|update on)\b/;
  function isClearAsk(l) { return ASK_RE.test(l) || /\?\s*$/.test(l.trim()); }

  function detectIntent(msg) {
    var l=msg.toLowerCase();
    if(/\b(weather|forecast|temperature)\b/.test(l)){
      var asksWeather = isClearAsk(l)
        || /\b(weather|forecast|temperature)\s+(in|at|for|like|today|now|tomorrow)\b/.test(l)
        || /\b[a-z][a-z\s]{1,15}?\s+weather\b/.test(l);
      if (asksWeather) {
        var loc='London';
        var m1=l.match(/(?:weather|forecast|temperature)(?:\s+in|\s+at|\s+for)\s+([a-z][a-z\s]{1,20}?)(?:\?|$|,|\.)/);
        var m2=l.match(/([a-z][a-z\s]{1,15}?)\s+weather/);
        if(m1) loc=m1[1].trim(); else if(m2) loc=m2[1].trim();
        return {type:'weather',query:loc};
      }
    }
    for(var i=0;i<SHORT_C.length;i++) if(wbt(SHORT_C[i],l)) return {type:'crypto',query:CRYPTO_MAP[SHORT_C[i]]};
    for(var j=0;j<LONG_C.length;j++) if(l.indexOf(LONG_C[j])!==-1) return {type:'crypto',query:CRYPTO_MAP[LONG_C[j]]||LONG_C[j]};
    if(/\bcost(s|ing)?\b|\bspending\b|\bexpenditure(s)?\b|\bexpense(s)?\b|\bbilling\b|\bbudget(s|ing)?\b|how much.*(spen|cost)|token.*(cost|spend|usage)|api.*(cost|spend|usage)|usage.*cost|money.*(spent|used|spending)/.test(l)) return {type:'cost'};
    if(/crypto|coin|altcoin|blockchain|token|defi|nft/.test(l)) return {type:'crypto',query:'bitcoin'};
    if(/\b(stock price|share price|stock quote|quote for|stocks? of|shares of|stock for)\b/.test(l)
       || (/\bstocks?\b/.test(l) && isClearAsk(l))) {
      var tm=msg.match(/\b([A-Z]{1,5})\b/);
      var tk=tm&&!TBL[tm[1]]?tm[1]:null;
      return {type:'stocks',query:tk||'AAPL'};
    }
    if(/\bnews\b|headlines\b/.test(l) && (isClearAsk(l) || /news\s+(?:about|on|regarding|for)\b/.test(l))){
      var nm=l.match(/news\s+(?:about|on|regarding|for)\s+([a-z][a-z\s]{1,20}?)(?:\?|$|,|\.)/);
      return {type:'news',query:nm?nm[1].trim():null};
    }
    if(/self.?heal|diagnostic|circuit breaker|degraded model|health check|system health/.test(l)) return {type:'health'};
    if(/\bcpu\b|\bram\b|\bmemory\b|\bdisk\b|system stats|performance|\bprocessor\b|\busage\b/.test(l)) return {type:'system'};
    if(/\blight|lights|lamp|thermostat|heat|cool|fan|plug|switch|smart home|home assistant|turn on|turn off|toggle/.test(l)) return {type:'iot'};
    if(/\banalytics|dashboard|stats\b|statistics|how many request|api stats/.test(l)) return {type:'analytics'};
    if(/\bhistory\b|previous chat|past conversation|what did i say|chat log|recall|episode/.test(l)) return {type:'history'};
    return null;
  }

  function triggerCard(intent) {
    if (!intent) return;
    var q=(intent.query||'').toUpperCase();
    switch(intent.type){
      case 'weather':   showCard('WEATHER / '+(q||'LOCATION'),   function(){renderWeather(intent.query);});         break;
      case 'crypto':    showCard('CRYPTO / '+q,                   function(){renderCrypto(intent.query);},  30000);  break;
      case 'stocks':    showCard('STOCKS / '+q,                   function(){renderStocks(intent.query);},  60000);  break;
      case 'news':      showCard('NEWS / '+(q||'HEADLINES'),      function(){renderNews(intent.query);});            break;
      case 'system':    showCard('SYSTEM MONITOR',                function(){renderSystem();},              4000);   break;
      case 'health':    showCard('SELF-HEALING',                  function(){renderHealth();},              5000);   break;
      case 'iot':       showCard('SMART HOME',                    function(){renderIoT();},                 10000);  break;
      case 'analytics': showCard('ANALYTICS / 7D',               function(){renderAnalytics();});                   break;
      case 'cost':      showCard('COST & USAGE',                   function(){renderCostDashboard();},       60000);  break;
      case 'history':   showCard('CHAT HISTORY',                  function(){renderHistoryCard();});                 break;
    }
  }

  // Topbar history button (if exists)
  var histBtn=$('history-btn');
  if(histBtn) histBtn.addEventListener('click',function(){showCard('CHAT HISTORY',function(){renderHistoryCard();});});

  // ════════════════════════════════════════════════════════════════════════════
  // PUSH-TO-TALK
  // ════════════════════════════════════════════════════════════════════════════

  async function startPTT() {
    try {
      getAudioCtx();  // wake AudioContext before recording so first chunk plays
      var stream=await navigator.mediaDevices.getUserMedia({audio:true});
      _audioChunks=[];
      _mediaRec=new MediaRecorder(stream);
      _mediaRec.ondataavailable=function(e){if(e.data.size)_audioChunks.push(e.data);};
      _mediaRec.onstop=async function(){
        stream.getTracks().forEach(function(t){t.stop();});
        var blob=new Blob(_audioChunks,{type:'audio/webm'});
        var form=new FormData();
        form.append('file',blob,'ptt.webm');
        form.append('persona',$('chat-persona').value);
        if(chatSession) form.append('session_id',chatSession);

        showWelcome(false);
        var bubbleEl=appendMsg('ai','...');
        try {
          var resp=await fetch(API+'/api/voice/stream',{method:'POST',body:form});
          if(!resp.ok){bubbleEl.innerHTML='<span style="color:var(--red)">[VOICE ERROR] HTTP '+resp.status+'</span>';return;}
          var reader=resp.body.getReader(),dec=new TextDecoder(),fullText='';
          var streamDone=false;
          while(!streamDone){
            var ch=await reader.read(); if(ch.done) break;
            var lines=dec.decode(ch.value).split('\n');
            for(var i=0;i<lines.length;i++){
              var line=lines[i]; if(!line.startsWith('data:')) continue;
              var raw=line.slice(5).trim(); if(raw==='[DONE]') {streamDone=true; break;}
              try{
                var ev=JSON.parse(raw);
                if(ev.type==='transcript'){_lastTranscript=ev.text||'';appendMsg('user',_lastTranscript);triggerCard(detectIntent(_lastTranscript));}
                else if(ev.type==='text_chunk'){fullText+=ev.text||'';renderMessageMarkup(bubbleEl, fullText);scrollToBottom();}
                else if(ev.type==='audio_chunk'&&ev.audio_b64 && _audioEnabled) playChunk(ev.audio_b64, false);
                else if(ev.type==='done'){var rep=ev.reply||'';if(rep&&!fullText){renderMessageMarkup(bubbleEl, rep);fullText=rep;} streamDone=true;}
                else if(ev.type==='error'){bubbleEl.innerHTML='<span style="color:var(--red)">[VOICE ERROR] '+(ev.message||'Unknown')+'</span>'; streamDone=true;}
                else if(ev.type==='meta'&&ev.session_id) chatSession=ev.session_id;
              }catch(e){}
            }
          }
          if(bubbleEl.innerHTML.includes('typing-dot')) bubbleEl.textContent='(no response)';
          saveEpisode(_lastTranscript,fullText,chatSession);
          await loadSessions();
        } catch(e){ bubbleEl.innerHTML='<span style="color:var(--red)">[VOICE ERROR] '+e.message+'</span>'; }
      };
      _mediaRec.start();
      $('chat-ptt').classList.add('recording');
    } catch(e){ alert('Microphone access denied: '+e.message); }
  }

  function stopPTT(){
    if(_mediaRec&&_mediaRec.state!=='inactive') _mediaRec.stop();
    $('chat-ptt').classList.remove('recording');
  }

  $('chat-ptt').addEventListener('click',function(){
    _pttActive=!_pttActive;
    if(_pttActive) startPTT(); else stopPTT();
  });

  // ════════════════════════════════════════════════════════════════════════════
  // INIT
  // ════════════════════════════════════════════════════════════════════════════

  fetch(API + '/api/voice/mode')
    .then(function(r){ return r.json(); })
    .then(function(mode){
      _voiceModeState = mode || {};
      if (typeof mode.voice_output_enabled === 'boolean') _audioEnabled = mode.voice_output_enabled;
      syncAudioButton();
    })
    .catch(function(){});

  loadSessions();

})();
