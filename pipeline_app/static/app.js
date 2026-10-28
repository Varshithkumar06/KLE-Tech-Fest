/* Estimo front-end logic.
   Works with the Estimo index.html and style.css, and talks to the same
   backend: POST /api/estimate (SSE stream) and POST /api/chat. */
(() => {
  'use strict';

  /* =========================================================
     Config
     ========================================================= */
  const CONFIG = {
    endpoints: { estimate: '/api/estimate', chat: '/api/chat' },
    maxBytes: 20 * 1024 * 1024,
    mobileBreakpoint: 960,
    files: {
      image: { types: ['image/png', 'image/jpeg', 'image/webp'], label: 'PNG, JPG or WEBP image' },
      pdf: { types: ['application/pdf'], label: 'PDF document' },
    },
    stageIdle: {
      gemini: 'Starting analysis...',
      hardware: 'Waits for the brief',
      software: 'Waits for the brief',
      prediction: 'Waits for both agents',
    },
    stageAlias: { gemini_pred: 'prediction', nemotron: 'prediction' },
    chatGreeting:
      'Run an estimate first, then ask about component prices, team cost, the timeline or any line in the breakdown.',
    chatReady: 'Your estimate is ready. Ask about components, prices, sources, the team or the timeline.',
  };

  /* =========================================================
     State and element lookup
     ========================================================= */
  const $ = (id) => document.getElementById(id);
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  const state = {
    mode: 'text',
    files: { image: null, pdf: null },
    previewUrl: null,
    fileError: '',
    sessionId: null,
    hw: null,
    sw: null,
    outputs: { gemini: null, hardware: null, software: null, prediction: null },
    running: false,
    gotDone: false,
    chatBusy: false,
  };

  const els = {
    form: $('composer'),
    text: $('project-text'),
    run: $('analyse-btn'),
    hint: $('input-hint'),
    error: $('run-error'),
    results: $('results'),
    empty: $('empty'),
    dialog: $('sources-dialog'),
    chat: $('chat'),
    chatLog: $('chat-log'),
    chatInput: $('chat-input'),
    chatSend: $('chat-send'),
    chatSuggest: $('chat-suggest'),
    scrim: $('scrim'),
    askBtn: $('ask-btn'),
    routeList: document.querySelector('.route-list'),
  };

  /* =========================================================
     Helpers
     ========================================================= */
  const ESC_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ESC_MAP[c]);

  const plural = (n, one, many) => (n === 1 ? one : many);

  function safeUrl(u) {
    try {
      const x = new URL(u);
      return x.protocol === 'http:' || x.protocol === 'https:' ? x.href : '';
    } catch (e) {
      return '';
    }
  }

  function fmt(n) {
    if ((!n && n !== 0) || Number.isNaN(Number(n))) return '-';
    return '₹' + Number(n).toLocaleString('en-IN', { maximumFractionDigits: 0 });
  }

  function fmtRange(min, max) {
    if (!min && !max) return '-';
    if (!max || min === max) return fmt(min);
    return fmt(min) + ' to ' + fmt(max);
  }

  function fmtSize(bytes) {
    return bytes < 1024 * 1024
      ? Math.max(1, Math.round(bytes / 1024)) + ' KB'
      : (bytes / (1024 * 1024)).toFixed(1) + ' MB';
  }

  function midpoint(range) {
    const a = Number((range || {}).min) || 0;
    const b = Number((range || {}).max) || 0;
    return a && b ? (a + b) / 2 : a || b;
  }

  const sentenceCase = (s) => {
    s = String(s || '');
    return s ? s.charAt(0).toUpperCase() + s.slice(1).toLowerCase() : '';
  };

  const OUTPUT_TITLES = {
    gemini: 'Brief analysis output',
    hardware: 'Hardware pricing output',
    software: 'Software effort output',
    prediction: 'Final cost output',
  };

  // The viewer is for the agent's result, not supplier links or web addresses.
  // Recursively omit both the source arrays and any individual URL/link fields.
  function outputWithoutLinks(value) {
    if (Array.isArray(value)) return value.map(outputWithoutLinks);
    if (!value || typeof value !== 'object') return value;
    return Object.fromEntries(
      Object.entries(value)
        .filter(([key]) => !/(^|_)(sources?|urls?|links?|source_count)$/i.test(key))
        .map(([key, item]) => [key, outputWithoutLinks(item)])
    );
  }

  function setStageOutput(name, data) {
    if (!Object.hasOwn(state.outputs, name)) return;
    state.outputs[name] = data || null;
    const btn = $('btn-output-' + name);
    if (btn) btn.hidden = !data;
  }

  // NEW: Click handlers for report buttons
  function onReportClick(name) {
    return () => {
      const output = state.outputs[name];
      if (!output) return;
      $('modal-title').textContent = OUTPUT_TITLES[name] || 'Pipeline output';
      $('modal-content').innerHTML = `<pre class="stage-output">${esc(JSON.stringify(outputWithoutLinks(output), null, 2))}</pre>`;
      if (!els.dialog.open) els.dialog.showModal();
    };
  }

  function showStageOutput(name) {
    const output = state.outputs[name];
    if (!output) return;
    $('modal-title').textContent = OUTPUT_TITLES[name] || 'Pipeline output';
    $('modal-content').innerHTML = `<pre class="stage-output">${esc(JSON.stringify(outputWithoutLinks(output), null, 2))}</pre>`;
    if (!els.dialog.open) els.dialog.showModal();
  }

  // Wire up report button click handlers
  $('btn-output-gemini').addEventListener('click', onReportClick('gemini'));
  $('btn-output-hardware').addEventListener('click', onReportClick('hardware'));
  $('btn-output-software').addEventListener('click', onReportClick('software'));
  $('btn-output-prediction').addEventListener('click', onReportClick('prediction'));


  /* =========================================================
     Brief input: modes, files, validation
     ========================================================= */
  function setMode(mode) {
    state.mode = mode;
    state.fileError = '';
    document.querySelectorAll('.mode').forEach((b) => {
      const on = b.dataset.mode === mode;
      b.classList.toggle('is-active', on);
      b.setAttribute('aria-selected', String(on));
      b.tabIndex = on ? 0 : -1;
    });
    document.querySelectorAll('.pane').forEach((p) => {
      const on = p.id === 'pane-' + mode;
      p.classList.toggle('is-active', on);
      p.hidden = !on;
    });
    refreshInput();
  }

  function inputIsReady() {
    return state.mode === 'text' ? els.text.value.trim().length > 0 : !!state.files[state.mode];
  }

  function refreshInput() {
    let msg;
    if (state.mode === 'text') {
      const t = els.text.value.trim();
      const words = t ? t.split(/\s+/).length : 0;
      msg = words ? `${words} ${plural(words, 'word', 'words')} ready.` : 'Describe the project to begin.';
    } else {
      const f = state.files[state.mode];
      msg = f ? `${f.name} is ready.` : state.mode === 'image' ? 'Add an image to begin.' : 'Add a PDF to begin.';
    }
    if (state.fileError) msg = state.fileError;
    els.hint.textContent = msg;
    els.hint.classList.toggle('is-error', !!state.fileError);
    els.run.disabled = !inputIsReady() || state.running;
  }

  function rejectFile(kind, message) {
    state.fileError = message;
    $(kind + '-input').value = '';
    refreshInput();
  }

  function setFile(kind, file) {
    const rule = CONFIG.files[kind];
    const typeOk = rule.types.includes(file.type) || (kind === 'pdf' && /\.pdf$/i.test(file.name));
    if (!typeOk) return rejectFile(kind, `That file is not a ${rule.label}.`);
    if (file.size > CONFIG.maxBytes) {
      return rejectFile(kind, `That file is ${fmtSize(file.size)}. The limit is 20 MB.`);
    }

    state.fileError = '';
    state.files[kind] = file;
    $(kind + '-prompt').hidden = true;
    $(kind + '-chip').hidden = false;
    $(kind + '-name').textContent = file.name;
    $(kind + '-size').textContent = fmtSize(file.size);

    if (kind === 'image') {
      if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
      state.previewUrl = URL.createObjectURL(file);
      $('image-preview').src = state.previewUrl;
    }
    refreshInput();
  }

  function clearFile(kind) {
    state.files[kind] = null;
    state.fileError = '';
    $(kind + '-input').value = '';
    $(kind + '-chip').hidden = true;
    $(kind + '-prompt').hidden = false;
    if (kind === 'image' && state.previewUrl) {
      URL.revokeObjectURL(state.previewUrl);
      state.previewUrl = null;
      $('image-preview').removeAttribute('src');
    }
    refreshInput();
  }

  function bindDropzone(kind) {
    const zone = $(kind + '-dropzone');
    const input = $(kind + '-input');
    zone.addEventListener('dragover', (e) => {
      e.preventDefault();
      zone.classList.add('is-over');
    });
    ['dragleave', 'dragend'].forEach((t) => zone.addEventListener(t, () => zone.classList.remove('is-over')));
    zone.addEventListener('drop', (e) => {
      e.preventDefault();
      zone.classList.remove('is-over');
      const f = e.dataTransfer.files[0];
      if (f) setFile(kind, f);
    });
    input.addEventListener('change', () => {
      if (input.files[0]) setFile(kind, input.files[0]);
    });
  }

  /* =========================================================
     Progress route
     ========================================================= */
  function setStage(name, status, msg) {
    const el = $('stage-' + name);
    const m = $('msg-' + name);
    if (!el || !m) return;
    el.className = 'station is-' + status;
    m.textContent = msg || '';
  }

  function resetRoute() {
    Object.keys(CONFIG.stageIdle).forEach((s) => setStage(s, 'waiting', CONFIG.stageIdle[s]));
    Object.keys(state.outputs).forEach((name) => setStageOutput(name, null));
  }

  // Marks whichever step was in flight as failed (falls back to the first step).
  function failActiveStage(message) {
    const active = els.routeList.querySelector('.station.is-running');
    const name = active ? active.id.replace('stage-', '') : 'gemini';
    setStage(name, 'error', message);
  }

  function toggleSources(kind, count) {
    // Sources remain available to the backend for price research, but links are
    // intentionally not shown in this model-led estimate UI.
  }

  function showError(msg) {
    els.error.textContent = msg;
    els.error.hidden = false;
  }

  /* =========================================================
     Estimate run: POST then read the SSE stream
     ========================================================= */
  function buildPayload() {
    const fd = new FormData();
    fd.append('mode', state.mode);
    if (state.mode === 'text') fd.append('text', els.text.value.trim());
    else if (state.mode === 'image') fd.append('image', state.files.image);
    else if (state.mode === 'pdf') fd.append('pdf', state.files.pdf);
    return fd;
  }

  async function startPipeline() {
    if (state.running || !inputIsReady()) return;
    const payload = buildPayload();

    state.running = true;
    state.gotDone = false;
    state.sessionId = null;
    els.run.textContent = 'Estimating...';
    els.error.hidden = true;
    els.results.hidden = true;
    els.empty.hidden = false;
    els.routeList.setAttribute('aria-busy', 'true');
    resetRoute();
    resetChat();
    refreshInput();

    try {
      const res = await fetch(CONFIG.endpoints.estimate, { method: 'POST', body: payload });
      if (!res.ok || !res.body) throw new Error('Server responded with status ' + res.status);

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split('\n\n');
        buffer = parts.pop();
        parts.forEach(handleChunk);
      }
      if (buffer.trim()) handleChunk(buffer);

      if (!state.gotDone) throw new Error('The connection closed before the estimate finished.');
    } catch (err) {
      failActiveStage('Stopped: ' + err.message);
      if (els.error.hidden) showError('The estimate could not be completed. ' + err.message);
    } finally {
      state.running = false;
      els.run.textContent = 'Estimate cost';
      els.routeList.setAttribute('aria-busy', 'false');
      refreshInput();
    }
  }

  function handleChunk(part) {
    let event = 'message';
    const data = [];
    part
      .replace(/\r/g, '')
      .split('\n')
      .forEach((line) => {
        if (line.startsWith('event:')) event = line.slice(6).trim();
        else if (line.startsWith('data:')) data.push(line.slice(5).trim());
      });
    if (!data.length) return;
    try {
      handleEvent(event, JSON.parse(data.join('\n')));
    } catch (e) {
      console.error('Could not handle stream event:', e);
    }
  }

  function handleEvent(event, data) {
    if (event === 'stage') return handleStage(data);
    if (event === 'done') return handleDone(data);
  }

  function handleStage(data) {
    const id = CONFIG.stageAlias[data.stage] || data.stage;
    setStage(id, data.status, data.message);
    const finished = data.status === 'done' && data.data;

    if (data.stage === 'hardware' && finished) {
      state.hw = data.data;
      setStageOutput('hardware', data.data);
      toggleSources('hw', (state.hw.all_sources || []).length);
    }
    if (data.stage === 'software' && finished) {
      state.sw = data.data;
      setStageOutput('software', data.data);
      toggleSources('sw', (state.sw.all_sources || []).length);
    }
    if (data.stage === 'gemini' && finished) setStageOutput('gemini', data.data);
    if (id === 'prediction' && finished) {
      setStageOutput('prediction', data.data);
      renderPrediction(data.data);
    }
  }

  function handleDone(data) {
    state.gotDone = true;
    state.sessionId = data.session_id || state.sessionId;

    if (data.error) {
      showError(typeof data.error === 'string' ? data.error : 'The estimate could not be completed.');
      return;
    }

    state.hw = data.hardware || {};
    state.sw = data.software || {};
    setStageOutput('gemini', data.gemini || state.outputs.gemini);
    setStageOutput('hardware', data.hardware || state.outputs.hardware);
    setStageOutput('software', data.software || state.outputs.software);
    setStageOutput('prediction', data.prediction || state.outputs.prediction);
    renderAll(data);

    els.empty.hidden = true;
    els.results.hidden = false;
    els.chatSuggest.hidden = false;
    appendChat('bot', CONFIG.chatReady);

    if (window.innerWidth <= CONFIG.mobileBreakpoint) {
      els.results.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'start' });
    }
  }

  /* =========================================================
     Rendering
     ========================================================= */
  function renderRange(tc) {
    $('cost-opt').textContent = fmt(tc.optimistic);
    $('cost-likely').textContent = fmt(tc.likely);
    $('cost-pess').textContent = fmt(tc.pessimistic);

    // Position of "likely" between the low and high case, kept inside the bar.
    const lo = Number(tc.optimistic);
    const hi = Number(tc.pessimistic);
    const mid = Number(tc.likely);
    let pct = 50;
    if (hi > lo && mid >= lo) pct = Math.min(96, Math.max(4, ((mid - lo) / (hi - lo)) * 100));
    $('scale-lo').style.width = pct + '%';
    $('scale-mark').style.left = pct + '%';
  }

  function renderBreakdown(p) {
    const hw = p.hardware_cost_inr || {};
    const dev = p.software_dev_cost_inr || {};
    const lic = p.licence_cost_inr || {};
    $('bk-hw').textContent = fmtRange(hw.min, hw.max);
    $('bk-dev').textContent = fmtRange(dev.min, dev.max);
    $('bk-lic').textContent = fmtRange(lic.min, lic.max);

    const parts = [midpoint(hw), midpoint(dev), midpoint(lic)];
    const sum = parts.reduce((a, b) => a + b, 0);
    ['seg-hw', 'seg-dev', 'seg-lic'].forEach((id, i) => {
      $(id).style.width = sum > 0 ? (parts[i] / sum) * 100 + '%' : '0';
    });
  }

  function renderFlags(p) {
    const flags = [...(p.duplicates_flagged || []), ...(p.missing_specs || [])];
    $('flags-section').hidden = flags.length === 0;
    $('flags-wrap').innerHTML = flags.length ? '<ul>' + flags.map((f) => `<li>${esc(f)}</li>`).join('') + '</ul>' : '';
  }

  function renderPrediction(p) {
    renderRange(p.total_cost_inr || {});
    renderBreakdown(p);
    renderFlags(p);
    $('cost-summary').textContent = p.summary || '';

    const eff = p.effort_months || {};
    $('bk-months').textContent = eff.likely ? `${eff.low} to ${eff.high} months, likely about ${eff.likely}` : '-';
    const model = p.model_estimate || {};
    $('bk-engineers').textContent = model.recommended_engineers
      ? `${model.recommended_engineers} engineers for ${model.delivery_months_assumed} months`
      : 'Needs complete project scope';
    $('bk-accuracy').textContent = model.accuracy_percent
      ? `~${model.accuracy_percent}% held-out (${model.validation_metrics?.MAE ?? '-'} hr MAE)`
      : 'Not available';
    $('bk-conf').textContent = sentenceCase(p.confidence) || '-';
  }

  function sourceButton(type, idx, count) {
    return count > 0
      ? `<button type="button" class="src-btn" data-item-sources="${type}:${idx}">${count} ${plural(count, 'link', 'links')}</button>`
      : '<span class="block-note">None</span>';
  }

  const sourceCount = (it) => (it.sources || []).length || it.source_count || 0;

  function renderHardware(hw) {
    const wrap = $('hw-table-wrap');
    const items = hw.items || [];
    toggleSources('hw', (hw.all_sources || []).length);

    if (!items.length) {
      wrap.innerHTML = '<p class="none">No hardware components required.</p>';
      return;
    }

    const rows = items
      .map((it, idx) => {
        const ps = it.price_summary || {};
        const models = (it.suggested_models || [])
          .map((m) => `<span class="tag tag-model">${esc(((m.maker || '') + ' ' + (m.model || '')).trim())}</span>`)
          .join('');
        return `<tr>
          <td><strong>${esc(it.name)}</strong>${it.spec ? `<small>${esc(it.spec)}</small>` : ''}</td>
          <td>${models || '-'}</td>
          <td>${ps.min ? fmtRange(ps.min, ps.max) : '<span class="block-note">Price not found</span>'}</td>
        </tr>`;
      })
      .join('');

    const t = hw.total || {};
    const foot =
      t.min || t.max
        ? `<tfoot><tr><td colspan="2">Total hardware</td><td colspan="2">${fmtRange(t.min, t.max)}</td></tr></tfoot>`
        : '';

    wrap.innerHTML = `<table>
      <thead><tr><th>Component</th><th>Suggested models</th><th>Price range</th></tr></thead>
      <tbody>${rows}</tbody>${foot}
    </table>`;
  }

  function renderSoftware(sw) {
    const wrap = $('sw-table-wrap');
    const items = sw.items || [];
    toggleSources('sw', (sw.all_sources || []).length);

    if (!items.length) {
      wrap.innerHTML = '<p class="none">No software items required.</p>';
      return;
    }

    const maxHours = Math.max(1, ...items.map((it) => Number((it.effort || {}).hours_high) || 0));

    const rows = items
      .map((it, idx) => {
        const eff = it.effort || {};
        const ps = it.price_summary || {};
        const kind = String(it.kind || '');
        const kindClass = kind.includes('buy') ? 'tag-buy' : kind === 'process' ? 'tag-process' : 'tag-build';

        let detail = '-';
        if (eff.hours_likely) {
          const pct = Math.min(100, Math.round((eff.hours_likely / maxHours) * 100));
          detail = `${esc(eff.hours_low)} to ${esc(eff.hours_high)} hrs, likely about ${esc(eff.hours_likely)}
            <div class="meter"><span style="width:${pct}%"></span></div>
            ${eff.source ? `<small>${esc(eff.source)}</small>` : ''}`;
        } else if (ps.min || ps.max) {
          detail = `${fmtRange(ps.min, ps.max)}<small>Licence or SaaS</small>`;
        }

        return `<tr>
          <td><strong>${esc(it.name)}</strong></td>
          <td>${kind ? `<span class="tag ${kindClass}">${esc(kind)}</span>` : ''}${it.discipline ? `<span class="tag tag-plain">${esc(it.discipline)}</span>` : ''}</td>
          <td>${detail}</td>
        </tr>`;
      })
      .join('');

    const e = sw.effort || {};
    const foot = e.months_likely
      ? `<tfoot><tr><td colspan="2">Total development effort</td><td colspan="2">${esc(e.total_hours_low)} to ${esc(e.total_hours_high)} hrs, about ${esc(e.months_likely)} months likely</td></tr></tfoot>`
      : '';

    wrap.innerHTML = `<table>
      <thead><tr><th>Deliverable</th><th>Type</th><th>Effort or cost</th></tr></thead>
      <tbody>${rows}</tbody>${foot}
    </table>`;
  }

  function renderTeam(team) {
    const wrap = $('team-wrap');
    if (!team.length) {
      wrap.innerHTML = '<p class="none">No specific engineering roles suggested.</p>';
      return;
    }
    wrap.innerHTML =
      '<ul class="team">' +
      team.map((r) => `<li><strong>${esc(r.role)}</strong><span>${esc(r.reason)}</span></li>`).join('') +
      '</ul>';
  }

  function renderAll(data) {
    renderHardware(data.hardware || {});
    renderSoftware(data.software || {});
    renderTeam((data.gemini || {}).team || []);

    const p = data.prediction;
    if (p && !p.error) renderPrediction(p);
    else if (p && p.error) $('cost-summary').textContent = 'The total forecast failed: ' + p.error;
  }

  /* =========================================================
     Sources dialog
     ========================================================= */
  function sourceRow(s) {
    const url = safeUrl(s.url);
    let host = '';
    try {
      host = new URL(url).hostname.replace(/^www\./, '');
    } catch (e) {
      /* no valid host */
    }
    const label = esc(s.title || s.url || 'Untitled source');
    const link = url
      ? `<a class="source-link" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${label}</a>`
      : `<span class="source-link">${label}</span>`;

    return `<div class="source">
      <div class="source-meta">
        ${s.item ? `<span class="source-chip item">${esc(s.item)}</span>` : ''}
        ${host ? `<span class="source-chip">${esc(host)}</span>` : ''}
      </div>
      ${link}
      ${s.snippet ? `<p class="source-snippet">${esc(s.snippet)}</p>` : ''}
      ${url ? `<p class="source-url">${esc(url)}</p>` : ''}
    </div>`;
  }

  function openSources(title, list) {
    $('modal-title').textContent = `${title} (${list.length})`;
    $('modal-content').innerHTML = list.length
      ? list.map(sourceRow).join('')
      : '<p class="none" style="padding:24px 0">No external links found for this item.</p>';
    els.dialog.showModal();
  }

  function openAllSources(type) {
    const isHw = type === 'hardware';
    const data = isHw ? state.hw : state.sw;
    openSources(isHw ? 'Hardware sources' : 'Software sources', (data && data.all_sources) || []);
  }

  function openItemSources(type, idx) {
    const data = type === 'hardware' ? state.hw : state.sw;
    const item = data && data.items ? data.items[idx] : null;
    if (!item) return;
    openSources('Sources for ' + item.name, (item.sources || []).map((s) => ({ ...s, item: item.name })));
  }

  /* =========================================================
     Chat drawer
     ========================================================= */
  function openChat() {
    els.chat.classList.add('is-open');
    els.chat.removeAttribute('inert');
    els.chat.setAttribute('aria-hidden', 'false');
    els.scrim.hidden = false;
    els.chatInput.focus();
  }

  function closeChat() {
    els.chat.classList.remove('is-open');
    els.chat.setAttribute('inert', '');
    els.chat.setAttribute('aria-hidden', 'true');
    els.scrim.hidden = true;
    els.askBtn.focus();
  }

  function appendChat(cls, text) {
    const el = document.createElement('div');
    el.className = 'msg ' + cls;
    el.textContent = text;
    els.chatLog.appendChild(el);
    els.chatLog.scrollTop = els.chatLog.scrollHeight;
    return el;
  }

  function resetChat() {
    els.chatLog.innerHTML = '';
    appendChat('bot', CONFIG.chatGreeting);
    els.chatSuggest.hidden = true;
  }

  async function sendChat(text) {
    if (state.chatBusy) return;
    const msg = String(text ?? els.chatInput.value).trim();
    if (!msg) return;

    state.chatBusy = true;
    els.chatSend.disabled = true;
    els.chatInput.value = '';
    appendChat('user', msg);
    const pending = appendChat('bot thinking', 'Thinking...');

    try {
      const res = await fetch(CONFIG.endpoints.chat, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: state.sessionId || '', message: msg }),
      });
      const d = await res.json();
      pending.className = 'msg bot';
      pending.textContent = d.reply || d.error || 'No response.';
    } catch (e) {
      pending.className = 'msg bot';
      pending.textContent = 'Error: ' + e.message;
    } finally {
      state.chatBusy = false;
      els.chatSend.disabled = false;
      els.chatLog.scrollTop = els.chatLog.scrollHeight;
    }
  }

  /* =========================================================
     Wiring
     ========================================================= */
  const modeButtons = Array.from(document.querySelectorAll('.mode'));
  modeButtons.forEach((btn, i) => {
    btn.addEventListener('click', () => setMode(btn.dataset.mode));
    btn.addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
      const step = e.key === 'ArrowRight' ? 1 : modeButtons.length - 1;
      const next = modeButtons[(i + step) % modeButtons.length];
      next.focus();
      setMode(next.dataset.mode);
    });
  });

  els.text.addEventListener('input', refreshInput);
  els.text.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') startPipeline();
  });
  els.form.addEventListener('submit', (e) => {
    e.preventDefault();
    startPipeline();
  });

  bindDropzone('image');
  bindDropzone('pdf');

  // One delegated click handler for every data-* action button.
  document.addEventListener('click', (e) => {
    const clear = e.target.closest('[data-clear]');
    if (clear) {
      e.preventDefault();
      e.stopPropagation();
      return clearFile(clear.dataset.clear);
    }

    const all = e.target.closest('[data-sources]');
    if (all) return openAllSources(all.dataset.sources);

    const item = e.target.closest('[data-item-sources]');
    if (item) {
      const [type, idx] = item.dataset.itemSources.split(':');
      return openItemSources(type, Number(idx));
    }

    const ask = e.target.closest('[data-ask]');
    if (ask) {
      openChat();
      sendChat(ask.dataset.ask);
    }
  });

  els.dialog.addEventListener('click', (e) => {
    if (e.target.closest('[data-close]')) return els.dialog.close();
    if (e.target === els.dialog) {
      const r = els.dialog.getBoundingClientRect();
      const outside = e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
      if (outside) els.dialog.close();
    }
  });

  els.askBtn.addEventListener('click', openChat);
  $('chat-close').addEventListener('click', closeChat);
  els.scrim.addEventListener('click', closeChat);
  $('chat-form').addEventListener('submit', (e) => {
    e.preventDefault();
    sendChat();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && els.chat.classList.contains('is-open')) closeChat();
  });

  setMode('text');
})();
