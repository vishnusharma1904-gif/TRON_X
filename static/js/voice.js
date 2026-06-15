/**
 * TRON-X Voice Module  v3
 * ─────────────────────────
 * speakAndReveal(text, persona, onStart, onEnd)
 *   → synthesises full response, fires onStart the moment audio begins,
 *     fires onEnd when audio finishes. Long text is chunked into paragraphs
 *     so TTS synthesis stays fast.
 */

const Voice = (() => {
  let mediaRecorder  = null;
  let audioChunks    = [];
  let isRecording    = false;
  let stream         = null;
  let audioCtx2      = null;   // waveform analyser context
  let waveAnalyser   = null;
  let animFrame      = null;
  let currentAudio   = null;
  // Phase 38: voice output is OPT-IN — muted unless the user enabled it.
  let _muted         = localStorage.getItem('tronx_muted') !== 'false';
  let _sharedCtx     = null;
  let _ctxUnlocked   = false;

  // ── AudioContext helpers ──────────────────────────────────────────────────

  function _ensureCtx() {
    if (_sharedCtx && _sharedCtx.state !== 'closed') {
      if (_sharedCtx.state === 'suspended') _sharedCtx.resume().catch(() => {});
      return _sharedCtx;
    }
    _sharedCtx = new (window.AudioContext || window.webkitAudioContext)();
    return _sharedCtx;
  }

  function _unlockAudio() {
    if (_ctxUnlocked) return;
    try {
      const ctx = _ensureCtx();
      const buf = ctx.createBuffer(1, 1, 22050);
      const src = ctx.createBufferSource();
      src.buffer = buf; src.connect(ctx.destination); src.start(0);
      _ctxUnlocked = true;
    } catch (e) {}
  }

  // ── DOM helpers ───────────────────────────────────────────────────────────
  const $voice      = () => document.getElementById('voice-btn');
  const $waveBar    = () => document.getElementById('waveform-bar');
  const $waveCanvas = () => document.getElementById('waveform-canvas');
  const $muteBtn    = () => document.getElementById('mute-btn');

  // ── Mute toggle ───────────────────────────────────────────────────────────

  function toggleMute() {
    _muted = !_muted;
    localStorage.setItem('tronx_muted', _muted);
    if (_muted && currentAudio) {
      try { currentAudio.stop(); } catch(e) {}
      currentAudio = null;
      Scene.setState('idle');
      _updateSpeakingStatus(false);
    }
    _syncMuteUI();
    return _muted;
  }

  function isMuted() { return _muted; }

  function _syncMuteUI() {
    const btn = $muteBtn();
    if (!btn) return;
    if (_muted) {
      btn.innerHTML =
        '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
        '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>' +
        '<line x1="23" y1="9" x2="17" y2="15"/><line x1="17" y1="9" x2="23" y2="15"/>' +
        '</svg> MUTED';
      btn.classList.add('muted');
    } else {
      btn.innerHTML =
        '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
        '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>' +
        '<path d="M19.07 4.93a10 10 0 0 1 0 14.14M15.54 8.46a5 5 0 0 1 0 7.07"/>' +
        '</svg> VOICE';
      btn.classList.remove('muted');
    }
  }

  // ── Synthesise helper — returns { audio_b64, audio_format } or null ───────

  async function _fetchTTS(text, persona) {
    try {
      const resp = await fetch('/api/voice/tts', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ text, persona, format: 'mp3' }),
      });
      if (!resp.ok) { console.error('[voice] TTS HTTP', resp.status); return null; }
      const data = await resp.json();
      if (data.audio_b64 && data.audio_b64.length > 20) return data;
      console.warn('[voice] TTS empty audio');
      return null;
    } catch (e) {
      console.error('[voice] TTS fetch failed:', e);
      return null;
    }
  }

  // ── Chunk long text into paragraphs / sentence groups ────────────────────
  // Edge-TTS handles up to ~3 000 chars cleanly; chunk at 800 chars on
  // sentence boundaries so each chunk synthesises fast.

  function _chunkText(text, maxChars = 800) {
    if (text.length <= maxChars) return [text];
    const sentences = text.match(/[^.!?]+[.!?]+\s*/g) || [text];
    const chunks = [];
    let cur = '';
    for (const s of sentences) {
      if (cur.length + s.length > maxChars && cur) {
        chunks.push(cur.trim());
        cur = '';
      }
      cur += s;
    }
    if (cur.trim()) chunks.push(cur.trim());
    return chunks.length ? chunks : [text];
  }

  // ── speakAndReveal ────────────────────────────────────────────────────────
  // Primary API called by Chat.send().
  // onStart  → fires when audio begins playing (reveal text here)
  // onEnd    → fires when all audio finishes (reset scene here)

  async function speakAndReveal(text, persona = 'jarvis', onStart, onEnd) {
    if (_muted) {
      // Muted: just reveal immediately
      if (onStart) onStart();
      if (onEnd)   onEnd();
      return;
    }

    const chunks = _chunkText(text.trim());
    console.log('[voice] speakAndReveal:', chunks.length, 'chunk(s), total', text.length, 'chars');

    // Pre-fetch all chunks in parallel immediately
    const fetches = chunks.map(c => _fetchTTS(c, persona));

    let startFired = false;
    for (let i = 0; i < fetches.length; i++) {
      const data = await fetches[i];
      if (!data) continue;

      // Fire onStart before the first chunk plays
      if (!startFired) {
        startFired = true;
        if (onStart) onStart();
      }

      await _playAudioData(data, i === fetches.length - 1 ? onEnd : null);
    }

    // Safety: if all fetches returned null, still fire callbacks
    if (!startFired) {
      if (onStart) onStart();
      if (onEnd)   onEnd();
    }
  }

  // ── Legacy speak() — kept for voice round-trip after mic recording ────────

  async function speak(text, persona = 'jarvis') {
    if (_muted || !text?.trim()) return;
    const data = await _fetchTTS(text, persona);
    if (data) await _playAudioData(data, null);
  }

  // ── Core audio playback ───────────────────────────────────────────────────

  async function _playAudioData(data, onFinished) {
    const { audio_b64, audio_format } = data;

    // Stop any currently playing audio
    if (currentAudio) {
      try { currentAudio.stop(); } catch(e) {}
      currentAudio = null;
    }

    _updateSpeakingStatus(true);

    return new Promise(resolve => {
      (async () => {
        try {
          const ctx = _ensureCtx();
          if (ctx.state === 'suspended') await ctx.resume();

          const arrayBuf = Uint8Array.from(atob(audio_b64), c => c.charCodeAt(0)).buffer;
          const audioBuf = await ctx.decodeAudioData(arrayBuf);

          const source      = ctx.createBufferSource();
          const analyserNode = ctx.createAnalyser();
          analyserNode.fftSize = 64;
          source.buffer = audioBuf;
          source.connect(analyserNode);
          analyserNode.connect(ctx.destination);
          currentAudio = source;

          const freqData = new Uint8Array(analyserNode.frequencyBinCount);
          let animId = null;
          function tick() {
            analyserNode.getByteFrequencyData(freqData);
            const avg = freqData.reduce((a, b) => a + b, 0) / freqData.length;
            Scene.setVoiceAmplitude(avg / 128);
            animId = requestAnimationFrame(tick);
          }
          tick();

          source.onended = () => {
            cancelAnimationFrame(animId);
            Scene.setVoiceAmplitude(0);
            _updateSpeakingStatus(false);
            currentAudio = null;
            if (onFinished) onFinished();
            resolve();
          };

          source.start(0);

        } catch (e) {
          console.error('[voice] _playAudioData failed:', e);
          _updateSpeakingStatus(false);
          currentAudio = null;
          if (onFinished) onFinished();
          resolve();
        }
      })();
    });
  }

  function _updateSpeakingStatus(speaking) {
    const el = document.getElementById('speaking-status');
    if (el) el.style.display = speaking ? 'flex' : 'none';
  }

  // ── Waveform visualizer (microphone) ─────────────────────────────────────

  function _startWaveform(srcStream) {
    audioCtx2    = new (window.AudioContext || window.webkitAudioContext)();
    waveAnalyser = audioCtx2.createAnalyser();
    waveAnalyser.fftSize = 128;
    audioCtx2.createMediaStreamSource(srcStream).connect(waveAnalyser);
    const cvs = $waveCanvas();
    if (!cvs) return;
    const ctx2 = cvs.getContext('2d');
    const W = cvs.width, H = cvs.height;
    const buf = new Uint8Array(waveAnalyser.frequencyBinCount);
    function draw() {
      animFrame = requestAnimationFrame(draw);
      waveAnalyser.getByteFrequencyData(buf);
      ctx2.clearRect(0, 0, W, H);
      const barW = W / buf.length;
      let amp = 0;
      buf.forEach((v, i) => {
        const h = (v / 255) * H;
        ctx2.fillStyle = 'rgba(0,245,255,' + (0.4 + (v/255)*0.6) + ')';
        ctx2.fillRect(i * barW, H - h, Math.max(barW - 1, 1), h);
        amp += v;
      });
      Scene.setVoiceAmplitude((amp / buf.length) / 128);
    }
    draw();
  }

  function _stopWaveform() {
    if (animFrame)  { cancelAnimationFrame(animFrame); animFrame = null; }
    if (audioCtx2)  { audioCtx2.close(); audioCtx2 = null; }
    waveAnalyser = null;
    Scene.setVoiceAmplitude(0);
    const cvs = $waveCanvas();
    if (cvs) cvs.getContext('2d').clearRect(0, 0, cvs.width, cvs.height);
  }

  // ── Push-to-talk recording ────────────────────────────────────────────────

  async function startRecording() {
    if (isRecording) return;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) { console.error('[voice] Mic denied:', e); return; }
    audioChunks   = [];
    mediaRecorder = new MediaRecorder(stream);
    mediaRecorder.ondataavailable = e => { if (e.data.size > 0) audioChunks.push(e.data); };
    mediaRecorder.start(100);
    isRecording = true;
    $voice()?.classList.add('recording');
    $waveBar()?.classList.remove('hidden');
    _startWaveform(stream);
    Scene.setState('listening');
  }

  async function stopRecording() {
    if (!isRecording || !mediaRecorder) return;
    isRecording = false;
    await new Promise(res => { mediaRecorder.onstop = res; mediaRecorder.stop(); });
    stream.getTracks().forEach(t => t.stop());
    _stopWaveform();
    $voice()?.classList.remove('recording');
    $waveBar()?.classList.add('hidden');
    const blob = new Blob(audioChunks, { type: 'audio/webm' });
    if (blob.size < 1000) { Scene.setState('idle'); return; }
    await _sendVoice(blob);
  }

  async function _sendVoice(blob) {
    Scene.setState('thinking');
    const persona = document.getElementById('persona-select')?.value || 'jarvis';
    const form = new FormData();
    form.append('file', blob, 'recording.webm');
    form.append('persona', persona);
    const sid = localStorage.getItem('tronx_session');
    if (sid) form.append('session_id', sid);
    try {
      const resp = await fetch('/api/voice', { method: 'POST', body: form });
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      const data = await resp.json();
      localStorage.setItem('tronx_session', data.session_id);
      Chat.addMsg('user',      data.transcript, { tag: 'YOU · VOICE' });
      Chat.addMsg('assistant', data.reply,       { intent: data.intent, model: data.model });
      Chat.updateInfo(data);
      if (data.audio_b64 && !_muted) {
        await _playAudioData({ audio_b64: data.audio_b64, audio_format: data.audio_format }, null);
      }
      Chat.loadMemoryStats();
      Chat.refreshHistory();
    } catch (err) {
      console.error('[voice] round-trip failed:', err);
      Chat.addMsg('assistant', '⚠ Voice error: ' + err.message);
      Scene.setState('error');
      setTimeout(() => Scene.setState('idle'), 2000);
    }
  }

  // ── Init ──────────────────────────────────────────────────────────────────

  function init() {
    const _first = () => {
      _unlockAudio();
      document.removeEventListener('click',   _first);
      document.removeEventListener('keydown', _first);
    };
    document.addEventListener('click',   _first, { once: true });
    document.addEventListener('keydown', _first, { once: true });

    const vBtn = $voice();
    if (vBtn) {
      vBtn.addEventListener('mousedown',  e => { e.preventDefault(); _unlockAudio(); startRecording(); });
      vBtn.addEventListener('mouseup',    e => { e.preventDefault(); stopRecording(); });
      vBtn.addEventListener('mouseleave', e => { if (isRecording) stopRecording(); });
      vBtn.addEventListener('touchstart', e => { e.preventDefault(); _unlockAudio(); startRecording(); }, { passive: false });
      vBtn.addEventListener('touchend',   e => { e.preventDefault(); stopRecording(); }, { passive: false });
    }

    const mBtn = $muteBtn();
    if (mBtn) mBtn.addEventListener('click', () => { _unlockAudio(); toggleMute(); });

    _syncMuteUI();

    fetch('/api/voice/status').then(r => r.json()).then(d => {
      const el = document.getElementById('info-voice');
      if (el) {
        el.textContent = d.tts && d.tts.kokoro ? 'Kokoro ✓' : 'edge-tts ✓';
        el.className = 'val ok';
      }
    }).catch(() => {});
  }

  return { init, speak, speakAndReveal, toggleMute, isMuted };
})();
