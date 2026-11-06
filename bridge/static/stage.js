(() => {
  const canvas = document.querySelector('#sky');
  const ctx = canvas.getContext('2d');
  const stateSelect = document.querySelector('#state');
  const stateLabel = document.querySelector('#state-label');
  const caption = document.querySelector('#caption');
  const controls = ['level', 'energy', 'warmth', 'curiosity'];
  let width = 0, height = 0, dpr = 1, raf = 0, inputCtx = null, outputCtx = null;
  let inputStream = null, inputNode = null, micMuted = false, connected = false;
  const RAY_DIRS = (() => { const n = 240, out = [], g = Math.PI * (3 - Math.sqrt(5));
    for (let i = 0; i < n; i++) { const y = 1 - (i + .5) / n * 2, r = Math.sqrt(1 - y * y), t = i * g;
      out.push({ x: Math.cos(t) * r, y, z: Math.sin(t) * r, k: (Math.sin(i * 7.31) + 1) / 2 }); } return out; })();
  let micPending = [], micPendingLen = 0;
  let tiltX = 0, tiltY = 0, pointerX = 0, pointerY = 0, drawTimer = 0;
  // Frame budget per state: a calm sun needs few frames; spend them while Leo is interacting.
  const FPS = { Asleep: 8, Offline: 8, Idle: 15, Thinking: 24, Listening: 30, Speaking: 30, Recalling: 30, "Didn't catch it": 24 };
  addEventListener('pointermove', e => { pointerX = (e.clientX / innerWidth - .5) * 2; pointerY = (e.clientY / innerHeight - .5) * 2; }, { passive: true });
  let state = 'Idle', smoothLevel = 0, micLevel = 0, voiceRms = 0, ttsLevel = 0, lastDraw = 0, stars = [];
  let memoryLoaded = false, selectedFactId = null, pinnedMemory = false;
  let mood = null, moodPresence = null, moodTurns = [], presenceReceivedAt = 0;
  let ws = null, reconnectTimer = 0, reconnectDelay = 1000, noTranscriptTimer = 0;
  let lastActivityAt = Date.now();
  let playbackAt = 0, lastSource = null, responseDone = false, resampler = null;
  let assistantCaption = '';
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  class StreamResampler {
    constructor(fromRate, toRate) {
      this.same = fromRate === toRate; this.step = fromRate / toRate;
      this.cut = Math.min(1, toRate / fromRate); this.half = 16;
      this.buf = new Float32Array(this.half); this.pos = this.half;
    }
    process(input) {
      if (this.same) return input;
      const {half, cut} = this;
      const buffer = new Float32Array(this.buf.length + input.length);
      buffer.set(this.buf); buffer.set(input, this.buf.length);
      const output = [];
      while (this.pos + half < buffer.length) {
        const center = this.pos, start = Math.floor(center) - half + 1;
        let value = 0;
        for (let k = start; k < start + 2 * half; k++) {
          const distance = k - center, scaled = distance * cut;
          const sinc = scaled === 0 ? 1 : Math.sin(Math.PI * scaled) / (Math.PI * scaled);
          const window = 0.5 + 0.5 * Math.cos(Math.PI * distance / half);
          value += buffer[k] * sinc * window * cut;
        }
        output.push(value); this.pos += this.step;
      }
      const drop = Math.max(0, Math.floor(this.pos) - half);
      this.buf = buffer.slice(drop); this.pos -= drop;
      return Float32Array.from(output);
    }
  }
  function stableHash(value, seed) {
    let hash = seed >>> 0;
    for (let i = 0; i < value.length; i++) { hash ^= value.charCodeAt(i); hash = Math.imul(hash, 16777619); }
    return hash >>> 0;
  }
  function sunCenter() { return width < 760 ? { x: width / 2, y: height * .3 } : { x: width * .7, y: height * .5 }; }
  function layoutStars() {
    const span = Math.min(width, height), c = sunCenter(), minX = width < 760 ? 24 : width * .52;
    stars.forEach(star => {
      const angle = stableHash(star.id, 2166136261) / 4294967296 * Math.PI * 2;
      const distance = span * (.27 + stableHash(star.id, 3339675911) / 4294967296 * .17);
      star.x = Math.max(minX, Math.min(width - 24, c.x + Math.cos(angle) * distance));
      star.y = Math.max(96, Math.min(height - 24, c.y + Math.sin(angle) * distance));
    });
  }
  function drawMemoryStars(now, cx, cy) {
    for (const star of stars) {
      const age = star.bornAt ? Math.min(1, (now - star.bornAt) / 900) : 1;
      const recalled = star.recallUntil > now;
      const asleep = state === 'Asleep';
      const r = (1.8 + age * 1.2) * (recalled ? 1.8 : 1);
      if (recalled) {
        ctx.strokeStyle = `rgba(240,165,58,${.75 * (star.recallUntil - now) / 1800})`; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(star.x, star.y); ctx.stroke();
      }
      ctx.beginPath(); ctx.arc(star.x, star.y, r, 0, Math.PI * 2);
      ctx.fillStyle = recalled ? '#F0A53A' : `rgba(236,229,214,${asleep ? .2 : .35 + age * .35})`; ctx.fill();
    }
  }
  async function refreshMemory() {
    try {
      const response = await fetch('/memory', {cache:'no-store'});
      if (!response.ok) throw new Error(`GET /memory returned ${response.status}`);
      const snapshot = await response.json();
      if (!Array.isArray(snapshot.items)) throw new Error('Memory response has no items list');
      const old = new Map(stars.map(star => [star.id, star]));
      const now = performance.now();
      stars = snapshot.items.filter(item => item && typeof item.id === 'string' && typeof item.text === 'string')
        .map(item => {
          const prior = old.get(item.id);
          return {id:item.id, text:item.text, bornAt:prior ? prior.bornAt : (memoryLoaded ? now : 0),
            recallUntil:prior ? prior.recallUntil : 0};
        });
      memoryLoaded = true;
      memoryCount.textContent = snapshot.count ? `Memories · ${snapshot.count}` : '';
      memoryCount.setAttribute('aria-label', snapshot.count ? `${snapshot.count} saved memories` : 'No saved memories');
      if (selectedFactId && !stars.some(star => star.id === selectedFactId)) hideMemoryInfo();
      layoutStars();
      if (!raf && !document.hidden) raf = requestAnimationFrame(draw);
    } catch (error) {
      console.warn('Memory constellation unavailable', error);
    }
  }
  function showMemoryInfo(star, recalled) {
    selectedFactId = star.id;
    memoryText.textContent = star.text;
    memoryNote.textContent = recalled ? 'Recall match · word overlap heuristic' : 'Saved memory';
    memoryInfo.hidden = false;
  }
  function hideMemoryInfo() { selectedFactId = null; pinnedMemory = false; memoryInfo.hidden = true; }
  function handleRecall(text) {
    const stop = new Set(['the','and','for','that','this','with','from','have','has','are','was','were','you','your','his','her','their','about','what','when','where','will','would','could','should','into','onto','then','than','they','them','its','him','she','who','how','not','but','our','out','all','any','can','did','does','had','may','might','more','most','some','such','very']);
    const words = new Set((text.toLowerCase().match(/[a-z0-9']+/g) || []).filter(word => word.length > 2 && !stop.has(word)));
    if (!words.size) return;
    const now = performance.now();
    let matched = false;
    for (const star of stars) {
      const factWords = (star.text.toLowerCase().match(/[a-z0-9']+/g) || []).filter(word => word.length > 2 && !stop.has(word));
      const overlap = factWords.filter(word => words.has(word)).length;
      if (overlap >= 2) {
        matched = true;
        star.recallUntil = now + 1800;
        if (selectedFactId === star.id) showMemoryInfo(star, true);
      }
    }
    if (matched && state === 'Thinking') setState('Recalling');
    if (!raf && !document.hidden) raf = requestAnimationFrame(draw);
  }
  // Mood is a shallow transform of measured time, turn, RMS, question, and word signals.
  function updateMood() {
    const positiveWords = new Set(['glad','happy','love','great','good','thanks','thank','excited','wonderful','nice','enjoy']);
    const negativeWords = new Set(['sad','angry','hate','bad','frustrated','worry','worried','afraid','upset','tired','lonely']);
    const userTurns = moodTurns.filter(turn => turn.role === 'user' && turn.text);
    const sentimentCounts = {positive:0, negative:0};
    for (const turn of userTurns) {
      const words = turn.text.toLowerCase().match(/[a-z']+/g) || [];
      for (const word of words) {
        if (positiveWords.has(word)) sentimentCounts.positive++;
        if (negativeWords.has(word)) sentimentCounts.negative++;
      }
    }
    const now = Date.now();
    const recentUsers = userTurns.filter(turn => {
      const stamp = typeof turn.ts === 'number' ? turn.ts : Date.parse(turn.ts || '');
      return Number.isFinite(stamp) && now - stamp <= 600000;
    });
    const values = [];
    if (moodPresence && moodPresence.time_of_day) {
      const dayEnergy = {morning:.65, afternoon:1, evening:.55, night:.18}[moodPresence.time_of_day];
      if (Number.isFinite(dayEnergy)) values.push(dayEnergy);
    }
    if (moodPresence && Number.isFinite(moodPresence.gap_seconds)) {
      const liveGap = moodPresence.gap_seconds + (now - presenceReceivedAt) / 1000;
      values.push(Math.exp(-liveGap / 21600));
    }
    if (recentUsers.length) values.push(Math.min(1, recentUsers.length / 3));
    const energy = values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
    const sentimentTotal = sentimentCounts.positive + sentimentCounts.negative;
    const warmth = sentimentTotal ? sentimentCounts.positive / sentimentTotal : null;
    const questionTurns = userTurns.filter(turn => /\?|\b(?:why|how|what|when|where|who)\b/i.test(turn.text)).length;
    const curiosity = userTurns.length ? questionTurns / userTurns.length : null;
    mood = {energy, warmth, curiosity};
    if (!raf && !document.hidden) raf = requestAnimationFrame(draw);
  }
  async function loadMoodContext() {
    const [presenceResult, historyResult] = await Promise.allSettled([
      fetchJson('/api/presence'), fetchJson('/api/history?n=20')
    ]);
    if (presenceResult.status === 'fulfilled') { moodPresence = presenceResult.value; presenceReceivedAt = Date.now(); }
    if (historyResult.status === 'fulfilled') moodTurns = historyResult.value.turns || [];
    updateMood();
  }
  const el = { calm: document.querySelector('#calm'), energy: document.querySelector('#energy'), warmth: document.querySelector('#warmth'),
    curiosity: document.querySelector('#curiosity'), level: document.querySelector('#level') };
  function resize() {
    width = innerWidth; height = innerHeight;
    dpr = Math.min(devicePixelRatio || 1, width * height > 1.2e6 ? 1 : 1.25);
    canvas.width = Math.round(width * dpr); canvas.height = Math.round(height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    layoutStars();
  }
  function draw(now) {
    raf = 0;
    if (document.hidden) return;
    const gap = 1000 / (FPS[state] || 30);
    if (!reduced.matches && now - lastDraw < gap - 2) {
      if (!drawTimer) drawTimer = setTimeout(() => { drawTimer = 0; if (!raf && !document.hidden) raf = requestAnimationFrame(draw); }, Math.max(1, gap - (now - lastDraw) - 3));
      return;
    }
    lastDraw = now;
    const calm = el.calm.checked;
    const measuredEnergy = connected && mood ? mood.energy : null;
    const liveVoiceEnergy = inputStream ? Math.min(1, voiceRms * 5) : null;
    const energy = measuredEnergy !== null ? (liveVoiceEnergy !== null ? measuredEnergy * .8 + liveVoiceEnergy * .2 : measuredEnergy) :
      liveVoiceEnergy !== null ? liveVoiceEnergy : connected ? 0 : +el.energy.value;
    const warmth = connected ? (mood && mood.warmth !== null ? mood.warmth : 0) : +el.warmth.value;
    const curiosity = connected ? (mood && mood.curiosity !== null ? mood.curiosity : 0) : +el.curiosity.value;
    const level = connected ? (state === 'Speaking' ? ttsLevel : micLevel) : +el.level.value;
    smoothLevel += (level - smoothLevel) * .12;
    ctx.clearRect(0, 0, width, height);
    const asleep = state === 'Asleep', offline = state === 'Offline';
    const sc = sunCenter(), cx = sc.x, cy = sc.y, time = now / 1000;
    const pulse = 1 + Math.sin(time * (asleep ? .45 : .9 + energy) * Math.PI) * .025;
    const radius = Math.min(width, height) * (width < 760 ? .12 : .15) * pulse * (1 + smoothLevel * .06);
    // ---- flat sun: crisp rays on a rotating sphere (still 3D), no glow ----
    const rays = calm ? 60 : 150, stride = (RAY_DIRS.length / rays) | 0 || 1;
    tiltX += (pointerX - tiltX) * .06; tiltY += (pointerY - tiltY) * .06;
    const yaw = time * (state === 'Thinking' ? .5 : .14 + energy * .16) + tiltX * .6;
    const pitch = -.22 + tiltY * .4 + Math.sin(time * .3) * .04;
    const cyaw = Math.cos(yaw), syaw = Math.sin(yaw), cp = Math.cos(pitch), sp = Math.sin(pitch);
    const D = 4.2, ccy = cy, dim = offline || asleep ? .4 : 1;
    const cr = radius * .8, back = new Path2D(), front = new Path2D();
    for (let i = 0; i < rays; i++) {
      const d = RAY_DIRS[i * stride];
      const wobble = Math.sin(i * 12.9898 * stride + time * .34) * .5 + Math.sin(i * 3.17 * stride - time * .19) * .5;
      const reach = (10 + 16 * warmth + wobble * 4 + smoothLevel * 30 + curiosity * 4) * (state === 'Thinking' ? .6 : 1);
      const r0 = cr * 1.22, r1 = r0 + reach * (1 + d.k * .5);
      const x = d.x * cyaw + d.z * syaw, z = -d.x * syaw + d.z * cyaw;
      const y2 = d.y * cp - z * sp, z2 = d.y * sp + z * cp;
      const s0 = D / (D - z2 * r0 / radius), s1 = D / (D - z2 * r1 / radius);
      const lean = state === 'Listening' ? smoothLevel * 8 * z2 : 0;
      const px0 = cx + x * r0 * s0, py0 = ccy + y2 * r0 * s0;
      if (Math.hypot(px0 - cx, py0 - ccy) < cr * 1.06) continue; // would sit on the disc: skip, keeps a clean silhouette
      const path = z2 < 0 ? back : front;
      path.moveTo(px0, py0);
      path.lineTo(cx + x * r1 * s1 + lean, ccy + y2 * r1 * s1);
    }
    ctx.lineCap = 'round';
    ctx.strokeStyle = `rgba(240,165,58,${.3 * dim})`; ctx.lineWidth = 1.2; ctx.stroke(back);
    // disc with one hard crescent shadow (flat illustration, not a glow)
    ctx.save(); ctx.beginPath(); ctx.arc(cx, ccy, cr, 0, Math.PI * 2); ctx.clip();
    ctx.globalAlpha = dim; ctx.fillStyle = '#B86F18'; ctx.fillRect(cx - cr, ccy - cr, cr * 2, cr * 2);
    ctx.fillStyle = '#F0A53A'; ctx.beginPath();
    ctx.arc(cx - cr * (.2 + tiltX * .08), ccy - cr * (.16 + tiltY * .08), cr * 1.02, 0, Math.PI * 2); ctx.fill();
    ctx.restore();
    ctx.strokeStyle = `rgba(240,165,58,${.95 * dim})`; ctx.lineWidth = 1.8; ctx.stroke(front);
    drawMemoryStars(now, cx, cy);
    if (!reduced.matches) raf = requestAnimationFrame(draw);
  }
  function render() {
    state = stateSelect.value;
    const preview = !connected && state !== 'Offline';
    stateLabel.textContent = `${state.toUpperCase()}${preview ? ' · PREVIEW' : ''}`;
    if (!connected) caption.textContent = ({Idle:'A quiet place to begin.',Asleep:'Resting beneath the stars.',Listening:'I’m listening.',Thinking:'Let me gather my thoughts.',Speaking:'I’m here with you.',Recalling:'I remember.',"Didn't catch it":"I didn't quite catch that.",Offline:'Connection is resting.'})[state];
    if (!raf && !document.hidden) raf = requestAnimationFrame(draw);
  }
  stateSelect.onchange = render; controls.forEach(id => document.querySelector(`#${id}`).oninput = render);
  const micButton = document.querySelector('#mic-toggle');
  const connection = document.querySelector('#connection');
  const memoryCount = document.querySelector('#memory-count');
  const memoryInfo = document.querySelector('#memory-info');
  const memoryText = document.querySelector('#memory-text');
  const memoryNote = document.querySelector('#memory-note');
  const forgetButton = document.querySelector('#forget-memory');
  const systemsPanel = document.querySelector('#systems');
  const conversationPanel = document.querySelector('#conversation');
  const conversationList = document.querySelector('#conversation-list');
  const systemVitals = document.querySelector('#system-vitals');
  const systemHealth = document.querySelector('#system-health');
  const systemModels = document.querySelector('#system-models');
  const systemLatency = document.querySelector('#system-latency');
  const systemFields = (data, fields) => fields.filter(([,key]) => Number.isFinite(data[key]))
    .map(([label,key,unit]) => `${label}  ${data[key]}${unit || ''}`).join('\n');
  async function fetchJson(path) {
    const response = await fetch(path, {cache:'no-store'});
    if (!response.ok) throw new Error(`${path} returned ${response.status}`);
    return response.json();
  }
  async function refreshSystems() {
    if (systemsPanel.hidden || document.hidden) return;
    const [statsResult, healthResult, modelsResult, latencyResult] = await Promise.allSettled([
      fetchJson('/api/stats'), fetchJson('/api/health'), fetchJson('/api/models'), fetchJson('/api/latency?n=20')
    ]);
    if (statsResult.status === 'fulfilled') {
      const system = statsResult.value.system || {};
      const rows = systemFields(system, [['CPU','cpu_pct','%'],['GPU','gpu_pct','%'],['Temperature','temp_c',' °C'],['Power','power_w',' W'],['RAM available','mem_available_mb',' MB']]);
      systemVitals.textContent = rows || 'No live vitals available';
    } else systemVitals.textContent = 'Vitals unavailable';
    if (healthResult.status === 'fulfilled') {
      systemHealth.textContent = (healthResult.value.services || []).map(service =>
        `${service.name}: ${service.state}${Number.isFinite(service.latency_ms) ? ` · ${service.latency_ms} ms` : ''}`
      ).join('\n') || 'No service health data';
    } else systemHealth.textContent = 'Health checks unavailable';
    if (modelsResult.status === 'fulfilled') {
      systemModels.textContent = (modelsResult.value.models || []).map(model =>
        `${model.role}: ${model.name}${model.detail ? ` · ${model.detail}` : ''}`
      ).join('\n') || 'No model data';
    } else systemModels.textContent = 'Model status unavailable';
    if (latencyResult.status === 'fulfilled') {
      const stages = latencyResult.value.stages || {};
      const rows = Object.values(stages).filter(stage => Number.isFinite(stage.median_ms))
        .map(stage => `${stage.label}: ${stage.median_ms} ms median · ${stage.samples} turns`);
      systemLatency.textContent = rows.join('\n') || 'No measured turns yet';
    } else systemLatency.textContent = 'Latency data unavailable';
  }
  async function refreshConversation() {
    if (conversationPanel.hidden || document.hidden) return;
    try {
      const result = await fetchJson('/api/history?n=50');
      moodTurns = result.turns || []; updateMood();
      conversationList.replaceChildren();
      for (const turn of result.turns || []) {
        const item = document.createElement('article'); item.className = 'conversation-entry';
        const role = document.createElement('strong'); role.textContent = turn.role || 'Turn'; item.appendChild(role);
        if (turn.ts) {
          const time = document.createElement('time');
          const parsed = Date.parse(turn.ts); time.textContent = Number.isFinite(parsed) ? new Date(parsed).toLocaleString() : turn.ts;
          item.appendChild(time);
        }
        const text = document.createElement('p'); text.textContent = turn.text || ''; item.appendChild(text);
        conversationList.appendChild(item);
      }
    } catch (error) { conversationList.textContent = 'Conversation history unavailable'; console.warn(error); }
  }
  document.querySelector('#systems-toggle').onclick = () => {
    systemsPanel.hidden = false; conversationPanel.hidden = true;
    document.querySelector('#systems-toggle').setAttribute('aria-expanded','true'); refreshSystems();
  };
  document.querySelector('#close-systems').onclick = () => {
    systemsPanel.hidden = true; document.querySelector('#systems-toggle').setAttribute('aria-expanded','false');
  };
  document.querySelector('#conversation-toggle').onclick = () => {
    conversationPanel.hidden = false; systemsPanel.hidden = true; refreshConversation();
  };
  document.querySelector('#close-conversation').onclick = () => { conversationPanel.hidden = true; };
  setInterval(() => { if (!document.hidden) { refreshSystems(); refreshConversation(); } }, 4000);
  document.querySelector('#close-memory').onclick = hideMemoryInfo;
  canvas.addEventListener('pointermove', event => {
    const rect = canvas.getBoundingClientRect();
    const x = event.clientX - rect.left, y = event.clientY - rect.top;
    let found = null, nearest = 16;
    for (const star of stars) {
      const distance = Math.hypot(star.x - x, star.y - y);
      if (distance < nearest) { found = star; nearest = distance; }
    }
    canvas.style.cursor = found ? 'pointer' : 'default';
    if (found) showMemoryInfo(found, found.recallUntil > performance.now());
    else if (!pinnedMemory) hideMemoryInfo();
  });
  canvas.addEventListener('pointerdown', event => {
    const rect = canvas.getBoundingClientRect();
    const x = event.clientX - rect.left, y = event.clientY - rect.top;
    const found = stars.find(star => Math.hypot(star.x - x, star.y - y) < 22);
    if (found) { pinnedMemory = selectedFactId === found.id ? !pinnedMemory : true; showMemoryInfo(found, found.recallUntil > performance.now()); }
  });
  canvas.addEventListener('pointerleave', () => { canvas.style.cursor = 'default'; if (!pinnedMemory) hideMemoryInfo(); });
  forgetButton.onclick = async () => {
    if (!selectedFactId) return;
    const id = selectedFactId;
    forgetButton.disabled = true; memoryNote.textContent = 'Forgetting…';
    try {
      const response = await fetch(`/memory/${encodeURIComponent(id)}`, {method:'DELETE'});
      if (!response.ok) throw new Error(`DELETE /memory/{id} returned ${response.status}`);
      pinnedMemory = false; hideMemoryInfo(); await refreshMemory();
    } catch (error) {
      memoryNote.textContent = 'Could not delete this memory. Try again.';
      console.error(error);
    } finally { forgetButton.disabled = false; }
  };
  const setState = value => { stateSelect.value = value; render(); };
  function connect() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
    const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${scheme}//${location.host}/v1/realtime?presence=1`, ['realtime']);
    connection.textContent = 'Connecting to local Leo…'; connection.className = '';
    ws.onopen = () => { connected = true; micMuted = false; lastActivityAt = Date.now(); reconnectDelay = 1000; connection.textContent = 'Connected locally'; connection.className = 'online'; setState('Idle'); loadMoodContext(); };
    ws.onclose = () => { connected = false; micMuted = true; connection.textContent = 'Offline · reconnecting'; connection.className = 'offline'; setState('Offline'); clearTimeout(reconnectTimer); reconnectTimer = setTimeout(connect, reconnectDelay); reconnectDelay = Math.min(reconnectDelay * 1.5, 10000); };
    ws.onerror = () => { connection.textContent = 'Connection interrupted'; connection.className = 'offline'; };
    ws.onmessage = event => { try { handleEvent(JSON.parse(event.data)); } catch (error) { console.error('Leo event error', error); } };
  }
  function send(data) { if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(data)); }
  function handleEvent(event) {
    if (event.type === 'input_audio_buffer.speech_started') { lastActivityAt = Date.now(); clearTimeout(noTranscriptTimer); micMuted = false; micLevel = .1; setState('Listening'); caption.textContent = 'I’m listening.'; }
    if (event.type === 'input_audio_buffer.speech_stopped') { lastActivityAt = Date.now(); micLevel = 0; micMuted = true; setState('Thinking'); caption.textContent = 'Let me gather my thoughts.'; clearTimeout(noTranscriptTimer); noTranscriptTimer = setTimeout(() => { if (state === 'Thinking') { micMuted = false; setState("Didn't catch it"); caption.textContent = "I didn't catch that. Would you say it again?"; } }, 2200); }
    if (event.type === 'conversation.item.input_audio_transcription.completed') {
      clearTimeout(noTranscriptTimer);
      if (event.transcript && event.transcript.trim()) {
        caption.textContent = `I heard: “${event.transcript.trim()}”`; setState('Thinking');
        moodTurns.push({role:'user', text:event.transcript.trim(), ts:Date.now()});
        if (moodPresence) { moodPresence.gap_seconds = 0; presenceReceivedAt = Date.now(); }
        updateMood();
      }
      else { micMuted = false; setState("Didn't catch it"); caption.textContent = "I didn't catch that. Would you say it again?"; }
    }
    if (event.type === 'response.created') { lastActivityAt = Date.now(); responseDone = false; assistantCaption = ''; micMuted = true; setState('Thinking'); }
    if (event.type === 'response.audio_transcript.delta' && event.delta) { assistantCaption += event.delta; caption.textContent = assistantCaption; handleRecall(assistantCaption); }
    if (event.type === 'response.audio_transcript.done') handleRecall(event.transcript || assistantCaption);
    if (event.type === 'response.audio.delta' && event.delta) { lastActivityAt = Date.now(); queueAudio(event.delta); setState('Speaking'); }
    if (event.type === 'response.done') { lastActivityAt = Date.now(); responseDone = true; if (!lastSource) { micMuted = false; setState('Idle'); } }
    if (event.type === 'error') { connection.textContent = (event.error && event.error.message) || 'Local voice error'; connection.className = 'offline'; }
  }
  function queueAudio(encoded) {
    if (!outputCtx || outputCtx.state === 'closed') outputCtx = new AudioContext({sampleRate: 24000});
    if (outputCtx.state === 'suspended') outputCtx.resume();
    const raw = atob(encoded), bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    const pcm = new Int16Array(bytes.buffer, bytes.byteOffset, Math.floor(bytes.byteLength / 2));
    const samples = new Float32Array(pcm.length);
    let sum = 0;
    for (let i = 0; i < pcm.length; i++) { samples[i] = pcm[i] / 32768; sum += samples[i] * samples[i]; }
    ttsLevel = Math.min(1, Math.sqrt(sum / Math.max(1, pcm.length)) * 4);
    const buffer = outputCtx.createBuffer(1, samples.length, 24000); buffer.copyToChannel(samples, 0);
    const source = outputCtx.createBufferSource(); source.buffer = buffer; source.connect(outputCtx.destination);
    const startAt = Math.max(outputCtx.currentTime, playbackAt); source.start(startAt);
    playbackAt = startAt + samples.length / 24000; lastSource = source;
    source.onended = () => { if (source === lastSource) { lastSource = null; ttsLevel = 0; micMuted = false; if (responseDone) setState('Idle'); } };
  }
  async function startMic() {
    if (inputStream) return;
    try {
      if (!outputCtx || outputCtx.state === 'closed') outputCtx = new AudioContext({sampleRate:24000});
      await outputCtx.resume();
      inputStream = await navigator.mediaDevices.getUserMedia({audio:{sampleRate:24000,channelCount:1,echoCancellation:true,noiseSuppression:true,autoGainControl:true}});
      inputCtx = new AudioContext({sampleRate:inputStream.getAudioTracks()[0].getSettings().sampleRate || 44100});
      await inputCtx.resume();
      resampler = new StreamResampler(inputCtx.sampleRate, 24000);
      const source = inputCtx.createMediaStreamSource(inputStream);
      const analyser = inputCtx.createAnalyser(); analyser.fftSize = 256; source.connect(analyser);
      const code = `class LeoPCM extends AudioWorkletProcessor { process(inputs) { const input=inputs[0]; const x=input && input[0]; if(x && x.length) this.port.postMessage(x); return true; } } registerProcessor('leo-pcm',LeoPCM);`;
      const blobUrl = URL.createObjectURL(new Blob([code],{type:'application/javascript'}));
      await inputCtx.audioWorklet.addModule(blobUrl); URL.revokeObjectURL(blobUrl);
      inputNode = new AudioWorkletNode(inputCtx,'leo-pcm'); analyser.connect(inputNode); inputNode.connect(inputCtx.destination);
      inputNode.port.onmessage = message => {
        if (micMuted || !connected) { micPending = []; micPendingLen = 0; return; }
        const chunk = message.data; let power = 0;
        for (const value of chunk) power += value * value;
        voiceRms = Math.sqrt(power / Math.max(1, chunk.length));
        micLevel = Math.min(1, voiceRms * 4);
        micPending.push(chunk); micPendingLen += chunk.length;
        if (micPendingLen < 1920) return;               // batch: about 40 ms at 48 kHz
        const joined = new Float32Array(micPendingLen); let at = 0;
        for (const part of micPending) { joined.set(part, at); at += part.length; }
        micPending = []; micPendingLen = 0;
        const floats = resampler.process(joined);
        if (!floats.length) return;
        const pcm = new Int16Array(floats.length);
        for (let i = 0; i < floats.length; i++) { const x = Math.max(-1, Math.min(1, floats[i])); pcm[i] = x < 0 ? x * 32768 : x * 32767; }
        const view = new Uint8Array(pcm.buffer); let binary = '';
        for (let i = 0; i < view.length; i += 0x8000) binary += String.fromCharCode(...view.subarray(i, i + 0x8000));
        send({type:'input_audio_buffer.append',audio:btoa(binary)});
      };
      micMuted = false; micButton.classList.add('active'); micButton.setAttribute('aria-label','Stop microphone');
      connection.textContent = 'Microphone on · local processing';
      playEarcon();
    } catch (error) {
      console.error('Microphone start failed',error); connection.textContent = `Microphone unavailable: ${error.message}`; connection.className = 'offline';
      stopMic();
    }
  }
  function playEarcon() {
    if (!outputCtx || !connected) return;
    micMuted = true;
    const oscillator = outputCtx.createOscillator(), gain = outputCtx.createGain();
    const start = outputCtx.currentTime;
    oscillator.type = 'sine'; oscillator.frequency.setValueAtTime(280, start);
    oscillator.frequency.exponentialRampToValueAtTime(420, start + .16);
    gain.gain.setValueAtTime(.0001, start); gain.gain.linearRampToValueAtTime(.018, start + .025);
    gain.gain.exponentialRampToValueAtTime(.0001, start + .18);
    oscillator.connect(gain).connect(outputCtx.destination);
    oscillator.onended = () => { oscillator.disconnect(); gain.disconnect(); if (connected && inputStream) micMuted = false; };
    oscillator.start(start); oscillator.stop(start + .19);
  }
  function stopMic() {
    micMuted = true; micLevel = 0; voiceRms = 0; micPending = []; micPendingLen = 0;
    if (inputNode) inputNode.disconnect(); inputNode = null;
    if (inputCtx) inputCtx.close(); inputCtx = null;
    if (inputStream) inputStream.getTracks().forEach(track => track.stop()); inputStream = null;
    micButton.classList.remove('active'); micButton.setAttribute('aria-label','Start microphone');
  }
  micButton.onclick = () => inputStream ? stopMic() : startMic();
  connect();
  refreshMemory();
  setInterval(() => {
    if (connected && state === 'Idle' && Date.now() - lastActivityAt >= 600000) setState('Asleep');
  }, 1000);
  setInterval(() => { if (!document.hidden) refreshMemory(); }, 15000);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden && raf) { cancelAnimationFrame(raf); raf = 0; }
    else { render(); refreshMemory(); }
  });
  addEventListener('resize', resize); resize(); render();
})();
