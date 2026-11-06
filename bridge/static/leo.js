/* ══════════════════════════════════════════════════════════════════════════
   Leo — dashboard behaviour
   Loaded after the console script, so the global `bus`, `wsClient`,
   `audioPlayback`, `micBtn`, `cancelBtn` and `micActive` are already live.
   Plain ES2020, no build step, no network except this origin.
   ══════════════════════════════════════════════════════════════════════════ */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const panel = $('panel-leo');
  if (!panel) return;

  const hero = $('leoHero');
  const stateTitle = $('leoStateTitle');
  const stateLine = $('leoStateLine');
  const micButton = $('leoMicBtn');
  const micLabel = $('leoMicLabel');
  const connPill = $('leoConn');
  const connText = $('leoConnText');
  const transcript = $('leoTranscript');
  const toast = $('leoToast');
  const scratch = document.createElement('div');

  const STATE_COPY = {
    connecting: ['Waking up…', 'Finding my body on the Jetson.'],
    idle: ['I’m here.', 'Tap the mic and say hello.'],
    listening: ['Listening…', 'I’m all ears — take your time.'],
    thinking: ['Thinking…', 'Let me find the words.'],
    speaking: ['Speaking…', 'Say anything and I’ll stop.'],
    offline: ['Reconnecting…', 'The bridge dropped. Hang on a second.'],
    error: ['Something slipped.', 'I lost the connection — trying again.'],
  };

  let state = 'connecting';
  let responseOngoing = false;
  let sawAudioThisResponse = false;
  let currentAssistantEl = null;
  let currentAssistantText = '';
  let historyLoaded = false;
  let factsSignature = '';
  let modelsSignature = '';
  let clearArmed = false;
  let clearTimer = null;

  // ── helpers ─────────────────────────────────────────────────────────────
  const esc = (value) => String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

  function richText(text) {
    scratch.textContent = text || '';
    return esc(scratch.textContent)
      .replace(/\*([^*\n]{1,120})\*/g, '<span class="leo-action">$1</span>');
  }

  function pad(n) { return String(n).padStart(2, '0'); }

  function clock(ts) {
    if (!ts) return '';
    const d = new Date(ts);
    if (Number.isNaN(d.getTime())) return '';
    return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function fmtMs(ms) {
    if (ms == null || Number.isNaN(ms)) return 'n/a';
    if (ms < 1000) return `${Math.round(ms)} ms`;
    return `${(ms / 1000).toFixed(1)} s`;
  }

  function fmtDuration(seconds) {
    if (!seconds || seconds < 0) return '—';
    const s = Math.round(seconds);
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    if (d) return `${d}d ${h}h`;
    if (h) return `${h}h ${pad(m)}m`;
    return `${m}m ${pad(s % 60)}s`;
  }

  function fmtAgo(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '';
    const diff = Math.max(0, (Date.now() - d.getTime()) / 1000);
    if (diff < 45) return 'just now';
    if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
    return `${Math.round(diff / 86400)}d ago`;
  }

  let toastTimer = null;
  function showToast(message) {
    if (!toast) return;
    toast.textContent = message;
    toast.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove('show'), 2600);
  }

  async function getJson(url, options) {
    const response = await fetch(url, { cache: 'no-store', ...(options || {}) });
    if (!response.ok) throw new Error(`${url} → HTTP ${response.status}`);
    return response.json();
  }

  // ── hero state machine ──────────────────────────────────────────────────
  function setState(next) {
    if (!STATE_COPY[next]) return;
    state = next;
    hero.dataset.state = next;
    stateTitle.textContent = STATE_COPY[next][0];
    stateLine.textContent = STATE_COPY[next][1];
    syncMicUi();
  }

  function restState() {
    return micActive ? 'listening' : 'idle';
  }

  function playbackDrained() {
    try {
      // If the AudioContext is suspended (autoplay policy, no output device)
      // nothing will ever play, so waiting on sample counts would hang the
      // face on "speaking" forever.
      if (audioPlayback.ctx && audioPlayback.ctx.state !== 'running') return true;
      const queued = audioPlayback._queuedSamples || 0;
      const played = audioPlayback._playedSamples || 0;
      if (!audioPlayback._playing && queued === 0) return true;
      return queued > 0 && played >= queued;
    } catch (_) {
      return true;
    }
  }

  function settle() {
    if (responseOngoing) return;
    if (playbackDrained()) setState(restState());
  }

  let lastStateSync = -1;
  // Small watchdog: the browser knows when the last scheduled sample has
  // played, but `onended` can be late on a background tab.
  setInterval(() => {
    if (state === 'speaking' && !responseOngoing && playbackDrained()) {
      setState(restState());
    }
    syncMicUi();
  }, 400);

  // ── microphone bridge ───────────────────────────────────────────────────
  function micIsSpeaking() {
    return state === 'speaking' || !!(cancelBtn && cancelBtn.classList.contains('visible'));
  }

  function syncMicUi() {
    if (!micButton) return;
    const speaking = micIsSpeaking();
    micButton.classList.toggle('mic-on', !!micActive && !speaking);
    const label = speaking
      ? 'Interrupt Leo'
      : micActive
        ? 'Mic on · tap to stop'
        : 'Tap to talk';
    if (micLabel.textContent !== label) micLabel.textContent = label;
    micButton.title = speaking
      ? 'Interrupt Leo'
      : micActive
        ? 'Stop the microphone (space)'
        : 'Talk to Leo (space)';
  }

  if (micButton) {
    micButton.addEventListener('click', () => {
      if (micIsSpeaking() && cancelBtn) {
        cancelBtn.click();
        showToast('Stopped.');
        return;
      }
      if (micBtn) micBtn.click();
      setTimeout(syncMicUi, 120);
      setTimeout(syncMicUi, 700);
    });
  }

  document.addEventListener('keydown', (event) => {
    if (event.code !== 'Space' || event.defaultPrevented) return;
    const tag = (event.target && event.target.tagName) || '';
    if (/^(INPUT|TEXTAREA|SELECT|BUTTON)$/.test(tag) || event.target.isContentEditable) return;
    if (!panel.classList.contains('active')) return;
    event.preventDefault();
    if (micIsSpeaking() && cancelBtn) cancelBtn.click();
    else if (micBtn) micBtn.click();
    setTimeout(syncMicUi, 150);
    setTimeout(syncMicUi, 700);
  });

  // ── transcript ──────────────────────────────────────────────────────────
  function nearBottom() {
    return transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 80;
  }

  function scrollIfNeeded(force) {
    if (force || nearBottom()) transcript.scrollTop = transcript.scrollHeight;
  }

  function messageEl(role, text, ts, forceScroll) {
    const stick = nearBottom();
    const el = document.createElement('div');
    el.className = `leo-msg ${role}`;
    el.innerHTML =
      `<div class="leo-meta"><span class="leo-role">${role === 'user' ? 'You' : 'Leo'}</span>` +
      `<span class="leo-time">${esc(clock(ts))}</span></div>` +
      `<div class="leo-text">${richText(text || '')}</div>`;
    transcript.appendChild(el);
    if (forceScroll || stick) transcript.scrollTop = transcript.scrollHeight;
    return el;
  }

  function clearTranscriptPlaceholder() {
    const empty = transcript.querySelector('.leo-empty');
    if (empty) empty.remove();
  }

  bus.on('ws:conversation.item.input_audio_transcription.completed', (data) => {
    const text = (data.transcript || '').trim();
    if (!text) return;
    clearTranscriptPlaceholder();
    messageEl('user', text, new Date().toISOString(), true);
    $('leoTurnBadge').textContent = 'heard you';
  });

  bus.on('ws:response.created', () => {
    responseOngoing = true;
    sawAudioThisResponse = false;
    currentAssistantEl = null;
    currentAssistantText = '';
    setState('thinking');
    $('leoTurnBadge').textContent = 'thinking';
  });

  bus.on('ws:response.audio_transcript.delta', (data) => {
    const delta = data.delta || '';
    if (!delta) return;
    if (!currentAssistantEl) {
      clearTranscriptPlaceholder();
      currentAssistantEl = messageEl('assistant', '', new Date().toISOString(), true);
    }
    currentAssistantText += delta;
    const textEl = currentAssistantEl.querySelector('.leo-text');
    if (textEl) textEl.innerHTML = richText(currentAssistantText);
    scrollIfNeeded(false);
  });

  bus.on('ws:response.audio.delta', () => {
    sawAudioThisResponse = true;
    if (state !== 'speaking') setState('speaking');
    $('leoTurnBadge').textContent = 'speaking';
  });

  bus.on('ws:response.done', () => {
    responseOngoing = false;
    currentAssistantEl = null;
    currentAssistantText = '';
    $('leoTurnBadge').textContent = 'idle';
    if (!sawAudioThisResponse) {
      setTimeout(() => settle(), 200);
    } else {
      setTimeout(() => settle(), 120);
    }
  });

  bus.on('audio:playbackDone', () => {
    if (!responseOngoing) setState(restState());
  });

  // ── websocket / service state ───────────────────────────────────────────
  bus.on('ws:connecting', () => {
    connPill.className = 'leo-pill connecting';
    connText.textContent = 'connecting';
    if (state !== 'speaking') setState('connecting');
  });

  bus.on('ws:open', () => {
    connPill.className = 'leo-pill online';
    connText.textContent = 'online';
    if (!responseOngoing) setState(restState());
  });

  bus.on('ws:close', () => {
    connPill.className = 'leo-pill offline';
    connText.textContent = 'offline';
    responseOngoing = false;
    setState('offline');
  });

  bus.on('ws:error', () => {
    connPill.className = 'leo-pill error';
    connText.textContent = 'error';
    setState('error');
  });

  bus.on('ws:input_audio_buffer.speech_started', () => {
    if (!responseOngoing) setState(micActive ? 'listening' : 'thinking');
  });

  bus.on('ws:input_audio_buffer.speech_stopped', () => {
    if (!responseOngoing) setState('thinking');
  });

  bus.on('ws:tau.metrics', () => {
    setTimeout(loadLatency, 700);
  });

  // ── history bootstrap ───────────────────────────────────────────────────
  let historyLoading = false;
  async function loadHistoryOnce() {
    if (historyLoaded || historyLoading) return;
    historyLoading = true;
    try {
      const data = await getJson('/api/history?n=8');
      const turns = data.turns || [];
      if (!turns.length) return;
      clearTranscriptPlaceholder();
      const sep = document.createElement('p');
      sep.className = 'leo-empty';
      sep.textContent = 'Earlier…';
      transcript.appendChild(sep);
      for (const turn of turns) {
        const role = turn.role === 'user' ? 'user' : 'assistant';
        messageEl(role, turn.text || '', turn.ts, false);
      }
      transcript.scrollTop = transcript.scrollHeight;
      historyLoaded = true;
    } catch (_) {
      /* first run: nothing to replay */
    } finally {
      historyLoading = false;
    }
  }

  // ── vitals ──────────────────────────────────────────────────────────────
  const ramHistory = [];
  const gpuHistory = [];

  function setMeter(el, pct) {
    if (!el) return;
    const value = Math.max(0, Math.min(100, Number(pct) || 0));
    el.style.width = `${value}%`;
    el.classList.toggle('hot', value >= 88);
    el.classList.toggle('warn', value >= 70 && value < 88);
  }

  function renderStats(data) {
    const sys = data.system || {};
    const memPct = sys.mem_used_pct;
    $('leoRamValue').textContent = memPct == null ? 'n/a' : `${memPct.toFixed(0)}%`;
    $('leoRamSub').textContent = sys.mem_total_mb
      ? `${(sys.mem_used_mb / 1024).toFixed(1)} / ${(sys.mem_total_mb / 1024).toFixed(1)} GB used`
      : 'n/a';
    setMeter($('leoRamBar'), memPct);

    const swapPct = sys.swap_used_pct;
    const hasSwap = (sys.swap_total_mb || 0) > 0;
    $('leoSwapValue').textContent = hasSwap ? `${(swapPct || 0).toFixed(0)}%` : 'none';
    $('leoSwapSub').textContent = hasSwap
      ? `${(sys.swap_used_mb / 1024).toFixed(2)} / ${(sys.swap_total_mb / 1024).toFixed(1)} GB used`
      : 'no swap configured';
    setMeter($('leoSwapBar'), hasSwap ? swapPct : 0);

    $('leoGpuValue').textContent = sys.gpu_pct == null ? 'n/a' : `${sys.gpu_pct.toFixed(0)}%`;
    $('leoCpuValue').textContent = sys.cpu_pct == null ? 'n/a' : `${sys.cpu_pct.toFixed(0)}%`;
    const load = sys.load || [];
    $('leoLoadValue').textContent = load.length
      ? `load ${load.map((n) => Number(n).toFixed(2)).join(' · ')}`
      : 'load —';

    const temps = sys.temps || {};
    const entries = Object.entries(temps);
    $('leoTempValue').textContent = sys.temp_c == null ? 'n/a' : `${sys.temp_c.toFixed(1)} °C`;
    if (entries.length) {
      const hottest = entries.sort((a, b) => b[1] - a[1])[0];
      $('leoTempSub').textContent = `${hottest[0]} hottest · ${entries.length} zones`;
    } else {
      $('leoTempSub').textContent = 'no sensors';
    }

    $('leoPowerValue').textContent = sys.power_w == null ? 'n/a' : `${sys.power_w.toFixed(1)} W`;
    const mode = sys.nvpmodel || {};
    $('leoModeValue').textContent = mode.name || (mode.mode != null ? `mode ${mode.mode}` : 'n/a');
    $('leoModeSub').textContent = mode.mode != null ? `nvpmodel ${mode.mode}` : 'nvpmodel';

    $('leoHostUptime').textContent = fmtDuration(sys.host_uptime_s);
    $('leoBridgeUptime').textContent = `Leo up ${fmtDuration((data.bridge || {}).uptime_s)}`;
    $('leoVitalsState').textContent = 'live';

    const stamp = new Date();
    $('leoStamp').textContent = `updated ${pad(stamp.getHours())}:${pad(stamp.getMinutes())}:${pad(stamp.getSeconds())}`;

    if (memPct != null) { ramHistory.push(memPct); if (ramHistory.length > 60) ramHistory.shift(); }
    if (sys.gpu_pct != null) { gpuHistory.push(sys.gpu_pct); if (gpuHistory.length > 60) gpuHistory.shift(); }
    $('leoSparkRamVal').textContent = memPct == null ? 'n/a' : `${memPct.toFixed(0)}%`;
    $('leoSparkGpuVal').textContent = sys.gpu_pct == null ? 'n/a' : `${sys.gpu_pct.toFixed(0)}%`;
    drawSpark($('sparkRam'), ramHistory, 0, 100);
    drawSpark($('sparkGpu'), gpuHistory, 0, 100);
  }

  function drawSpark(canvas, points, min, max) {
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    if (!rect.width) return;
    const dpr = window.devicePixelRatio || 1;
    const width = rect.width;
    const height = 46;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    ctx.strokeStyle = 'rgba(245,165,36,0.10)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, height - 0.5);
    ctx.lineTo(width, height - 0.5);
    ctx.stroke();

    if (points.length < 2) {
      ctx.fillStyle = 'rgba(180,163,144,0.45)';
      ctx.font = '9px ui-monospace, monospace';
      ctx.fillText('collecting…', 4, height / 2 + 3);
      return;
    }

    const span = Math.max(1, max - min);
    const step = width / (points.length - 1);
    const y = (value) => height - 3 - ((value - min) / span) * (height - 8);
    ctx.beginPath();
    points.forEach((value, index) => {
      const px = index * step;
      const py = y(value);
      if (index === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    });
    ctx.strokeStyle = '#f5a524';
    ctx.lineWidth = 1.6;
    ctx.lineJoin = 'round';
    ctx.stroke();

    ctx.lineTo(width, height);
    ctx.lineTo(0, height);
    ctx.closePath();
    const grad = ctx.createLinearGradient(0, 0, 0, height);
    grad.addColorStop(0, 'rgba(245,165,36,0.26)');
    grad.addColorStop(1, 'rgba(245,165,36,0.01)');
    ctx.fillStyle = grad;
    ctx.fill();
  }

  async function loadStats() {
    try {
      renderStats(await getJson('/api/stats'));
    } catch (_) {
      $('leoVitalsState').textContent = 'offline';
    }
  }

  // ── services + models ───────────────────────────────────────────────────
  function renderHealth(data) {
    const services = data.services || [];
    const local = services.filter((s) => s.name !== 'Bridge');
    const down = local.filter((s) => !s.ok);
    if (connPill.classList.contains('online') && down.length) {
      connPill.classList.add('connecting');
      connPill.classList.remove('online');
      connText.textContent = 'degraded';
    } else if (connText.textContent === 'degraded' && !down.length && wsClient && wsClient.connected) {
      connPill.className = 'leo-pill online';
      connText.textContent = 'online';
    }
  }

  function renderModels(data) {
    const models = data.models || [];
    const signature = JSON.stringify(models);
    if (signature === modelsSignature) return;
    modelsSignature = signature;
    const wrap = $('leoModels');
    $('leoModelBadge').textContent = `${models.filter((m) => m.ok).length}/${models.length} up`;
    if (!models.length) {
      wrap.innerHTML = '<p class="leo-empty">No services reported.</p>';
      return;
    }
    wrap.innerHTML = models.map((model) => {
      const stateClass = model.ok ? 'ok' : 'bad';
      return `<div class="leo-model-row">
        <span class="leo-model-dot ${stateClass}"></span>
        <div class="leo-model-info">
          <div class="leo-model-top">
            <span class="leo-model-role">${esc(model.role || '')}</span>
            <span class="leo-model-name">${esc(model.name || 'n/a')}</span>
          </div>
          <div class="leo-model-detail">${esc(model.detail || '')}</div>
        </div>
      </div>`;
    }).join('');
  }

  async function loadHealth() {
    try {
      const [health, models] = await Promise.all([
        getJson('/api/health'),
        getJson('/api/models'),
      ]);
      renderHealth(health);
      renderModels(models);
    } catch (_) {
      $('leoModelBadge').textContent = 'n/a';
    }
  }

  // ── latency ─────────────────────────────────────────────────────────────
  function segment(value, max, color) {
    if (!value || value <= 0) return '';
    const pct = Math.max((value / max) * 100, 0.6);
    return `<span class="leo-bar-seg" style="width:${pct}%;background:${color}" title="${Math.round(value)} ms"></span>`;
  }

  function renderLatency(data) {
    const medians = data.medians || {};
    const stages = data.stages || {};
    $('leoMedianTotal').textContent = fmtMs(medians.total_ms);
    $('leoMedianStt').textContent = fmtMs((stages.stt || {}).median_ms);
    $('leoMedianTps').textContent = medians.tokens_per_s == null
      ? 'n/a'
      : `${medians.tokens_per_s.toFixed(1)} tok/s`;
    $('leoLatencyBadge').textContent = `${data.turns || 0} turns`;

    const rows = (data.recent || []).slice(-5).reverse();
    const bars = $('leoBars');
    if (!rows.length) {
      bars.innerHTML = '<p class="leo-empty">No measured turns yet. Say one sentence.</p>';
    } else {
      const max = Math.max(800, ...rows.map((row) => row.total_ms || 0)) * 1.02;
      bars.innerHTML = rows.map((row) => {
        const total = row.total_ms || 0;
        const stt = row.stt_ms || 0;
        const llm = row.llm_first_token_ms || 0;
        const tts = row.tts_ms || 0;
        const tail = Math.max(total - stt - llm - tts, 0);
        const width = Math.max((total / max) * 100, 2);
        return `<div class="leo-bar-row" title="${row.status || ''}">
          <span class="leo-bar-time">${esc(clock(row.ts))}</span>
          <div class="leo-bar-track"><div style="display:flex;height:100%;width:${width}%">
            ${segment(stt, total, '#5fd3a4')}
            ${segment(llm, total, '#f5a524')}
            ${segment(tts, total, '#ff8f7a')}
            ${segment(tail, total, '#4b4036')}
          </div></div>
          <span class="leo-bar-total">${fmtMs(total)}</span>
        </div>`;
      }).join('');
    }

    const body = $('leoRecentTurns');
    const tableRows = (data.recent || []).slice(-10).reverse();
    if (!tableRows.length) {
      body.innerHTML = '<tr><td colspan="7" class="leo-empty">No rows</td></tr>';
    } else {
      body.innerHTML = tableRows.map((row) => {
        const statusClass = row.status === 'completed' ? 'ok' : row.status === 'cancelled' ? 'cancel' : '';
        const tokens = row.llm_tokens == null
          ? '—'
          : `${row.llm_tokens}${row.tokens_per_s ? ` · ${row.tokens_per_s.toFixed(1)}/s` : ''}`;
        return `<tr>
          <td>${esc(clock(row.ts))}</td>
          <td>${fmtMs(row.stt_ms)}</td>
          <td>${fmtMs(row.llm_total_ms == null ? row.llm_first_token_ms : row.llm_total_ms)}</td>
          <td>${fmtMs(row.tts_ms)}</td>
          <td>${esc(tokens)}</td>
          <td>${fmtMs(row.total_ms)}</td>
          <td class="${statusClass}">${esc(row.status || '')}</td>
        </tr>`;
      }).join('');
    }
  }

  async function loadLatency() {
    try {
      renderLatency(await getJson('/api/latency?n=20'));
    } catch (_) {
      $('leoLatencyBadge').textContent = 'n/a';
    }
  }

  // ── memory ──────────────────────────────────────────────────────────────
  function renderFacts(data) {
    const items = data.items || [];
    const signature = JSON.stringify(items);
    const count = items.length;
    $('leoFactCount').textContent = String(count);
    if (signature === factsSignature) return;
    factsSignature = signature;

    const list = $('leoFacts');
    if (!items.length) {
      list.innerHTML = '<li class="leo-empty">Nothing yet. Tell me something about yourself.</li>';
      return;
    }
    list.innerHTML = items.map((item) => `<li class="leo-fact">
      <span class="leo-fact-text">${esc(item.text)}</span>
      <button class="leo-fact-del" type="button" data-id="${esc(item.id)}" title="Forget this" aria-label="Forget: ${esc(item.text)}">&times;</button>
    </li>`).join('');
  }

  async function loadFacts() {
    try {
      renderFacts(await getJson('/memory'));
    } catch (_) {
      $('leoFactCount').textContent = 'n/a';
    }
  }

  $('leoFacts').addEventListener('click', async (event) => {
    const button = event.target.closest('.leo-fact-del');
    if (!button) return;
    const id = button.dataset.id;
    if (!id) return;
    button.disabled = true;
    try {
      await getJson(`/memory/${encodeURIComponent(id)}`, { method: 'DELETE' });
      factsSignature = '';
      await loadFacts();
      showToast('Forgotten.');
    } catch (err) {
      showToast('Could not forget that one.');
      button.disabled = false;
    }
  });

  const clearButton = $('leoClearFacts');
  clearButton.addEventListener('click', async () => {
    if (!clearArmed) {
      clearArmed = true;
      clearButton.classList.add('armed');
      clearButton.textContent = 'Click again to erase';
      clearTimeout(clearTimer);
      clearTimer = setTimeout(() => {
        clearArmed = false;
        clearButton.classList.remove('armed');
        clearButton.textContent = 'Clear all';
      }, 4000);
      return;
    }
    clearTimeout(clearTimer);
    clearArmed = false;
    clearButton.classList.remove('armed');
    clearButton.textContent = 'Clear all';
    try {
      const result = await getJson('/memory', { method: 'DELETE' });
      factsSignature = '';
      await loadFacts();
      showToast(`Forgot ${result.removed || 0} thing${result.removed === 1 ? '' : 's'}.`);
    } catch (_) {
      showToast('Could not clear memory.');
    }
  });

  // ── intro banner ────────────────────────────────────────────────────────
  try {
    // The markup ships hidden so the banner never flashes; show it until the
    // person has acknowledged it once.
    $('leoIntro').hidden = localStorage.getItem('leoIntroDismissed') === '1';
  } catch (_) {
    $('leoIntro').hidden = false;
  }

  $('leoIntroDismiss').addEventListener('click', () => {
    $('leoIntro').hidden = true;
    try { localStorage.setItem('leoIntroDismissed', '1'); } catch (_) { /* ignore */ }
  });

  // ── tab activation: refresh everything, replay history once ─────────────
  document.querySelectorAll('.tab-btn').forEach((button) => {
    button.addEventListener('click', () => {
      if (button.dataset.tab !== 'leo') return;
      loadHistoryOnce();
      loadStats();
      loadHealth();
      loadLatency();
      loadFacts();
      drawSpark($('sparkRam'), ramHistory, 0, 100);
      drawSpark($('sparkGpu'), gpuHistory, 0, 100);
    });
  });

  window.addEventListener('resize', () => {
    drawSpark($('sparkRam'), ramHistory, 0, 100);
    drawSpark($('sparkGpu'), gpuHistory, 0, 100);
  });

  // ── polling ─────────────────────────────────────────────────────────────
  let statsBusy = false;
  setInterval(async () => {
    if (statsBusy) return;
    statsBusy = true;
    await loadStats();
    statsBusy = false;
  }, 1000);

  setInterval(loadHealth, 5000);
  setInterval(loadLatency, 6000);
  setInterval(loadFacts, 8000);
  setInterval(loadHistoryOnce, 15000);

  // ── read-only debug handle (console + automated UI checks) ──────────────
  try {
    window.leoDebug = {
      bus,
      audioPlayback,
      wsClient,
      get micActive() { return micActive; },
      get state() { return state; },
    };
  } catch (_) { /* ignore */ }

  // ── boot ────────────────────────────────────────────────────────────────
  setState('connecting');
  loadStats();
  loadHealth();
  loadLatency();
  loadFacts();
  loadHistoryOnce();
  syncMicUi();
})();
