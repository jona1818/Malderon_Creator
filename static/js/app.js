/* ──────────────────────────────────────────────────────────────────────────
   YouTube Video Creator – Frontend JS
   ────────────────────────────────────────────────────────────────────────── */

// ── State ─────────────────────────────────────────────────────────────────
let currentProjectId = null;
let pollInterval = null;
let logEventSource = null;
let lastLogId = 0;

// Reference YouTube videos collected in the form [{url, title, transcript}]
const referenceVideos = [];

const STATUS_ICONS = {
  queued:                  '🕐',
  processing:              '⚙️',
  awaiting_approval:       '✏️',
  awaiting_voice_config:   '🎙️',
  awaiting_audio_approval: '🎵',
  done:                    '✅',
  error:                   '❌',
};

const MODE_ICONS = {
  animated: '🎨',
  stock:    '📹',
};

// ── View routing ──────────────────────────────────────────────────────────
function showView(name, projectId = null) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.getElementById(`view-${name}`).classList.add('active');

  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  const navBtn = document.querySelector(`[data-view="${name}"]`);
  if (navBtn) navBtn.classList.add('active');

  stopPolling();
  stopLogs();

  if (name === 'dashboard') loadDashboard();
  if (name === 'detail' && projectId) openDetail(projectId);
}

document.querySelectorAll('.nav-btn').forEach(btn => {
  btn.addEventListener('click', () => showView(btn.dataset.view));
});

// ── Dashboard ─────────────────────────────────────────────────────────────
async function loadDashboard() {
  try {
    const projects = await apiFetch('/api/projects/');
    const list = document.getElementById('projectList');
    const empty = document.getElementById('emptyState');

    list.innerHTML = '';
    if (!projects.length) {
      list.appendChild(empty);
      empty.style.display = '';
      return;
    }
    empty.style.display = 'none';

    projects.forEach(p => {
      const pct = p.chunk_count > 0 ? Math.round((p.chunks_done / p.chunk_count) * 100) : 0;
      const card = document.createElement('div');
      card.className = 'project-card';
      card.innerHTML = `
        <div class="project-card-icon">${MODE_ICONS[p.mode] || '🎬'}</div>
        <div class="project-card-body">
          <div class="project-card-title">${escHtml(p.title)}</div>
          <div class="project-card-meta">
            <span class="badge badge-${p.status}">${p.status.toUpperCase()}</span>
            <span>${new Date(p.created_at).toLocaleDateString('es-ES', { day:'2-digit', month:'short', year:'numeric' })}</span>
            ${p.chunk_count > 0 ? `<span>${p.chunks_done}/${p.chunk_count} escenas</span>` : ''}
          </div>
          ${p.chunk_count > 0 ? `
          <div class="project-card-progress">
            <div class="mini-progress-bar">
              <div class="mini-progress-fill" style="width:${pct}%"></div>
            </div>
          </div>` : ''}
        </div>
        <div class="project-card-actions">
          <button class="btn btn-ghost btn-sm" onclick="event.stopPropagation(); openDetail(${p.id})">Ver</button>
          ${p.status === 'error' ? `<button class="btn btn-ghost btn-sm" onclick="event.stopPropagation(); retryProject(${p.id})">Reintentar</button>` : ''}
          <button class="btn btn-ghost btn-sm" style="color:var(--red)" onclick="event.stopPropagation(); deleteProject(${p.id})">Borrar</button>
        </div>
      `;
      card.addEventListener('click', () => openDetail(p.id));
      list.appendChild(card);
    });

    updateWorkerStatus();
  } catch (e) {
    showToast('Error cargando proyectos: ' + e.message, 'error');
  }
}

// ── Outline collapse state ────────────────────────────────────────────────
let _outlineOpen = false;

function toggleOutline() {
  _outlineOpen = !_outlineOpen;
  const content = document.getElementById('outlineContent');
  const toggle  = document.getElementById('outlineToggle');
  if (content) content.style.display = _outlineOpen ? '' : 'none';
  if (toggle)  toggle.textContent = _outlineOpen ? '▲' : '▼';
}

// ── Detail view ───────────────────────────────────────────────────────────
async function openDetail(projectId) {
  currentProjectId = projectId;
  _outlineOpen      = false;
  _selectedVoiceId   = '';
  _selectedVoiceName = '';
  _allVoices         = [];

  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.getElementById('view-detail').classList.add('active');

  // Reset all sections
  document.getElementById('progressCard').style.display            = 'none';
  document.getElementById('outlineSection').style.display          = 'none';
  document.getElementById('scriptSection').style.display           = 'none';
  document.getElementById('voiceConfigSection').style.display      = 'none';
  document.getElementById('voiceoverApprovalSection').style.display = 'none';
  document.getElementById('chunksSection').style.display           = 'none';
  document.getElementById('videoPreviewContainer').style.display   = 'none';
  document.getElementById('chunkGrid').innerHTML   = '';
  document.getElementById('chunksList').innerHTML  = '';
  document.getElementById('logsContainer').innerHTML = '<div class="log-placeholder">Cargando logs\u2026</div>';

  await refreshDetail(projectId);

  startLogStream(projectId);
  pollInterval = setInterval(() => refreshDetail(projectId), 4000);
}

async function refreshDetail(projectId) {
  try {
    const p      = await apiFetch(`/api/projects/${projectId}`);
    const chunks = p.chunks || [];

    // ── Header ───────────────────────────────────────────────────────────
    document.getElementById('detailTitle').textContent = p.title;
    const badge = document.getElementById('detailBadge');
    badge.textContent = p.status.toUpperCase();
    badge.className   = `badge badge-${p.status}`;

    // ── Progress card (queued / processing / error) ───────────────────────
    const progressCard = document.getElementById('progressCard');
    const isRunning    = ['queued', 'processing', 'error'].includes(p.status);
    progressCard.style.display = isRunning ? '' : 'none';
    if (isRunning) {
      const total = chunks.length;
      const done  = chunks.filter(c => c.status === 'done').length;
      const pct   = total > 0 ? Math.round((done / total) * 100) : 0;
      document.getElementById('progressBar').style.width  = `${pct}%`;
      document.getElementById('progressPct').textContent  = `${pct}%`;
      document.getElementById('progressLabel').textContent =
        p.status === 'error'      ? `Error: ${p.error_message || ''}` :
        p.status === 'processing' ? 'Procesando…' : 'En cola…';

      const grid = document.getElementById('chunkGrid');
      grid.innerHTML = '';
      chunks.forEach(c => {
        const dot = document.createElement('div');
        dot.className = `chunk-dot ${c.status}`;
        dot.textContent = c.chunk_number;
        dot.title = `Chunk ${c.chunk_number}: ${c.status}`;
        grid.appendChild(dot);
      });
    }

    // ── 1. Outline (colapsable) ───────────────────────────────────────────
    const outlineSection  = document.getElementById('outlineSection');
    const outlineContent  = document.getElementById('outlineContent');
    const outlineToggleEl = document.getElementById('outlineToggle');
    if (p.outline) {
      outlineSection.style.display  = '';
      outlineContent.textContent    = p.outline;
      outlineContent.style.display  = _outlineOpen ? '' : 'none';
      if (outlineToggleEl) outlineToggleEl.textContent = _outlineOpen ? '▲' : '▼';
    } else {
      outlineSection.style.display = 'none';
    }

    // ── 2. Script section (editable ↔ read-only) ──────────────────────────
    const scriptSection          = document.getElementById('scriptSection');
    const approvalTextarea       = document.getElementById('approvalTextarea');
    const scriptContent          = document.getElementById('scriptContent');
    const scriptApprovalControls = document.getElementById('scriptApprovalControls');
    const scriptDoneBadge        = document.getElementById('scriptDoneBadge');
    const chunkConfigBar         = document.getElementById('chunkConfigBar');
    const scriptHint             = document.getElementById('scriptHint');

    if (p.script || p.script_final) {
      scriptSection.style.display = '';

      if (p.status === 'awaiting_approval') {
        // — Editable mode —
        scriptSection.classList.add('script-awaiting');
        approvalTextarea.style.display       = '';
        scriptContent.style.display          = 'none';
        scriptApprovalControls.style.display = '';
        scriptDoneBadge.style.display        = 'none';
        chunkConfigBar.style.display         = '';
        scriptHint.textContent = 'Edita el texto si lo deseas, luego aprueba para continuar.';

        if (!approvalTextarea.dataset.edited) {
          approvalTextarea.value = p.script;
          const sizeInput = document.getElementById('chunkSizeInput');
          if (sizeInput && p.target_chunk_size) sizeInput.value = p.target_chunk_size;
          updateChunkPreview();
        }
      } else {
        // — Read-only mode —
        scriptSection.classList.remove('script-awaiting');
        approvalTextarea.dataset.edited      = '';
        approvalTextarea.style.display       = 'none';
        scriptContent.style.display          = '';
        scriptContent.textContent            = p.script_final || p.script;
        scriptApprovalControls.style.display = 'none';
        scriptDoneBadge.style.display        = '';
        chunkConfigBar.style.display         = 'none';
        scriptHint.textContent               = '';
      }
    } else {
      scriptSection.style.display = 'none';
    }

    // ── 3. Voice config (awaiting_voice_config only) ──────────────────────
    const voiceConfigSection  = document.getElementById('voiceConfigSection');
    const voiceChunksSummary  = document.getElementById('voiceChunksSummary');
    if (p.status === 'awaiting_voice_config') {
      voiceConfigSection.style.display = '';

      // Show chunk summary + resplit
      if (chunks.length > 0 && voiceChunksSummary) {
        voiceChunksSummary.style.display = '';
        const info = document.getElementById('voiceChunksInfo');
        if (info) info.textContent = `Script dividido en ${chunks.length} chunks de ~${p.target_chunk_size || 1500} chars`;
        const resplitInput = document.getElementById('resplitInput');
        if (resplitInput) resplitInput.value = p.target_chunk_size || 1500;
      }

      if (p.tts_provider) {
        const sel = document.getElementById('ttsProvider');
        if (sel) sel.value = p.tts_provider;
      }
      onProviderChange();

      // Restore saved voice selection
      if (p.tts_voice_id && !_selectedVoiceId) {
        _selectedVoiceId = p.tts_voice_id;
        let voiceName = p.tts_voice_id;
        try {
          const cfg = JSON.parse(p.tts_config || '{}');
          if (cfg.voice_name) voiceName = cfg.voice_name;
        } catch (_) {}
        _selectedVoiceName = voiceName;
        const display = document.getElementById('selectedVoiceDisplay');
        const nameEl  = document.getElementById('selectedVoiceName');
        if (nameEl)  nameEl.textContent    = voiceName;
        if (display) display.style.display = '';
      }
    } else {
      voiceConfigSection.style.display = 'none';
    }

    // ── 3b. Voiceover Approval (awaiting_audio_approval) ─────────────────
    const approvalSection = document.getElementById('voiceoverApprovalSection');
    if (p.status === 'awaiting_audio_approval' && approvalSection) {
      approvalSection.style.display = '';
      const approvalAudio = document.getElementById('voiceoverApprovalAudio');
      if (approvalAudio && p.voiceover_path) {
        const audioUrl = `/api/projects/${p.id}/voiceover/audio`;
        if (approvalAudio.dataset.src !== audioUrl) {
          approvalAudio.src = audioUrl;
          approvalAudio.dataset.src = audioUrl;
        }
      }
    } else if (approvalSection) {
      approvalSection.style.display = 'none';
    }

    // ── 4. Escenas (ocultas hasta que el audio esté aprobado) ─────────────
    const chunksSection = document.getElementById('chunksSection');
    const hiddenStatuses = ['awaiting_voice_config', 'awaiting_audio_approval'];
    if (chunks.length > 0 && !hiddenStatuses.includes(p.status)) {
      chunksSection.style.display = '';
      const countEl = document.getElementById('chunksCount');
      if (countEl) countEl.textContent = `— ${chunks.length} escenas`;

      const list = document.getElementById('chunksList');
      list.innerHTML = '';
      chunks.forEach(c => {
        const text    = c.scene_text || '';
        const preview = text.length > 100 ? text.slice(0, 100) + '…' : text;
        const chars   = text.length.toLocaleString('es-ES');
        const card    = document.createElement('div');
        card.className = 'chunk-card';
        card.innerHTML = `
          <div class="chunk-card-header" onclick="toggleChunkCard(${c.chunk_number})">
            <span class="chunk-card-num">Chunk #${c.chunk_number}</span>
            <span class="chunk-card-preview">${escHtml(preview)}</span>
            <span class="chunk-card-chars">${chars} chars</span>
            <span class="chunk-card-status ${c.status}">${c.status}</span>
            <span class="chunk-card-toggle" id="toggle-${c.chunk_number}">▼</span>
          </div>
          <div class="chunk-card-body" id="chunk-body-${c.chunk_number}" style="display:none">${escHtml(text)}</div>
        `;
        list.appendChild(card);
      });
    } else {
      chunksSection.style.display = 'none';
    }

    // ── 5. Video final ────────────────────────────────────────────────────
    if (p.final_video_path) {
      const container = document.getElementById('videoPreviewContainer');
      const video     = document.getElementById('videoPreview');
      const relPath   = p.final_video_path.replace(/\\/g, '/').split('/projects/').pop();
      video.src = `/media/${relPath}`;
      container.style.display = '';
    } else {
      document.getElementById('videoPreviewContainer').style.display = 'none';
    }

    // ── Stop polling when in stable state ─────────────────────────────────
    if (['done', 'error', 'awaiting_approval', 'awaiting_voice_config', 'awaiting_audio_approval'].includes(p.status)) {
      stopPolling();
    }
  } catch (e) {
    console.error('refreshDetail error:', e);
  }
}

// ── Log streaming (SSE) ───────────────────────────────────────────────────
function startLogStream(projectId) {
  stopLogs();
  const container = document.getElementById('logsContainer');
  container.innerHTML = '';
  lastLogId = 0;

  logEventSource = new EventSource(`/api/logs/${projectId}/stream`);

  logEventSource.onmessage = (event) => {
    const log = JSON.parse(event.data);
    appendLog(log);
  };

  logEventSource.onerror = () => {
    // Fallback to polling if SSE fails
    logEventSource.close();
    logEventSource = null;
    startLogPolling(projectId);
  };
}

function startLogPolling(projectId) {
  const pollLogs = async () => {
    try {
      const logs = await apiFetch(`/api/logs/${projectId}?since_id=${lastLogId}&limit=50`);
      logs.forEach(log => {
        lastLogId = Math.max(lastLogId, log.id);
        appendLog(log);
      });
    } catch (e) { /* ignore */ }
  };
  const logPollTimer = setInterval(pollLogs, 1500);
  // Store timer for cleanup
  window._logPollTimer = logPollTimer;
}

function appendLog(log) {
  const container = document.getElementById('logsContainer');
  const placeholder = container.querySelector('.log-placeholder');
  if (placeholder) placeholder.remove();

  const line = document.createElement('div');
  line.className = `log-line ${log.level}`;
  const ts = new Date(log.timestamp).toLocaleTimeString('es-ES', { hour12: false });
  line.innerHTML = `
    <span class="log-ts">${ts}</span>
    <span class="log-stage">[${escHtml(log.stage || 'general')}]</span>
    <span class="log-msg">${escHtml(log.message)}</span>
  `;
  container.appendChild(line);
  container.scrollTop = container.scrollHeight;
}

function clearLogs() {
  document.getElementById('logsContainer').innerHTML = '';
}

function stopLogs() {
  if (logEventSource) { logEventSource.close(); logEventSource = null; }
  if (window._logPollTimer) { clearInterval(window._logPollTimer); window._logPollTimer = null; }
}

function stopPolling() {
  if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
}

// ── Reference Videos ──────────────────────────────────────────────────────

function renderReferenceList() {
  const list = document.getElementById('referenceList');
  list.innerHTML = '';
  referenceVideos.forEach((v, idx) => {
    const item = document.createElement('div');
    item.className = 'ref-item';
    item.innerHTML = `
      <div class="ref-item-icon">&#x1F3AC;</div>
      <div class="ref-item-body">
        <div class="ref-item-title">${escHtml(v.title || v.url)}</div>
        <div class="ref-item-status ${v.status === 'ok' ? 'ok' : v.status === 'error' ? 'error' : 'loading'}">
          ${v.status === 'ok' ? 'Transcripci\u00f3n obtenida \u2713' : v.status === 'error' ? v.error : 'Obteniendo transcripci\u00f3n\u2026'}
        </div>
      </div>
      <button class="ref-item-remove" onclick="removeReferenceVideo(${idx})" title="Eliminar">&#x2715;</button>
    `;
    list.appendChild(item);
  });

  // Hide add button when 3 videos reached
  const addBtn = document.getElementById('refAddBtn');
  const refInput = document.getElementById('refUrlInput');
  if (referenceVideos.length >= 3) {
    addBtn.disabled = true;
    refInput.disabled = true;
    refInput.placeholder = 'M\u00e1ximo 3 videos de referencia';
  } else {
    addBtn.disabled = false;
    refInput.disabled = false;
    refInput.placeholder = 'https://www.youtube.com/watch?v=...';
  }
}

async function addReferenceVideo() {
  const input = document.getElementById('refUrlInput');
  const url = input.value.trim();
  if (!url) return;
  if (referenceVideos.length >= 3) {
    showToast('M\u00e1ximo 3 videos de referencia', 'error');
    return;
  }

  // Add placeholder entry while loading
  const idx = referenceVideos.length;
  referenceVideos.push({ url, title: url, status: 'loading', transcript: '' });
  input.value = '';
  renderReferenceList();

  try {
    const result = await apiFetch('/api/youtube/transcript', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    referenceVideos[idx] = {
      url,
      title: result.title,
      transcript: result.transcript,
      status: 'ok',
    };
    showToast(`"${result.title}" \u2013 transcripci\u00f3n obtenida`, 'success');
  } catch (e) {
    referenceVideos[idx] = { url, title: url, status: 'error', error: e.message, transcript: '' };
    showToast('No se pudo obtener la transcripci\u00f3n: ' + e.message, 'error');
  }
  renderReferenceList();
}

function removeReferenceVideo(idx) {
  referenceVideos.splice(idx, 1);
  renderReferenceList();
}

// ── New video form ────────────────────────────────────────────────────────
const modeOptions = document.querySelectorAll('.mode-option');
modeOptions.forEach(opt => {
  opt.addEventListener('click', () => {
    modeOptions.forEach(o => o.classList.remove('active'));
    opt.classList.add('active');
    const isAnimated = opt.querySelector('input').value === 'animated';
    document.getElementById('characterGroup').style.display = isAnimated ? '' : 'none';
  });
});

const typeOptions = document.querySelectorAll('.type-option');
typeOptions.forEach(opt => {
  opt.addEventListener('click', () => {
    typeOptions.forEach(o => o.classList.remove('active'));
    opt.classList.add('active');
  });
});

const durOptions = document.querySelectorAll('.dur-option');
durOptions.forEach(opt => {
  opt.addEventListener('click', () => {
    durOptions.forEach(o => o.classList.remove('active'));
    opt.classList.add('active');
  });
});

async function submitNewVideo(event) {
  event.preventDefault();
  const btn = document.getElementById('submitBtn');
  btn.disabled = true;
  btn.textContent = 'Creando…';

  const mode = document.querySelector('input[name="mode"]:checked').value;
  const video_type = document.querySelector('input[name="video_type"]:checked').value;
  const duration = document.querySelector('input[name="duration"]:checked').value;

  // Only include completed reference videos (status === 'ok')
  const completedRefs = referenceVideos.filter(v => v.status === 'ok');
  const reference_transcripts = completedRefs.length > 0
    ? JSON.stringify(completedRefs.map(v => ({ url: v.url, title: v.title, transcript: v.transcript })))
    : null;

  const payload = {
    title: document.getElementById('title').value.trim(),
    topic: document.getElementById('topic').value.trim(),
    mode,
    video_type,
    duration,
    reference_character: mode === 'animated' ? document.getElementById('referenceCharacter').value.trim() || null : null,
    reference_transcripts,
  };

  try {
    const project = await apiFetch('/api/projects/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    showToast(`Video "${project.title}" creado y en cola`, 'success');
    document.getElementById('newVideoForm').reset();
    modeOptions[0].click(); // reset to animated
    typeOptions[0].click(); // reset to top10
    durOptions[0].click();  // reset to 6-8 min
    // Reset reference videos
    referenceVideos.length = 0;
    renderReferenceList();
    showView('detail', project.id);
  } catch (e) {
    showToast('Error al crear proyecto: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = '🚀 Crear Video';
  }
}

// ── Chunk card toggle ─────────────────────────────────────────────────────
function toggleChunkCard(num) {
  const body = document.getElementById(`chunk-body-${num}`);
  const icon = document.getElementById(`toggle-${num}`);
  if (!body) return;
  const open = body.style.display === 'none';
  body.style.display = open ? '' : 'none';
  if (icon) {
    icon.textContent = open ? '▲' : '▼';
    icon.classList.toggle('open', open);
  }
}

// ── Client-side sentence-break splitter (mirrors Python logic) ────────────
function _findSentenceBreak(text, target) {
  const start = Math.min(target, text.length - 1);
  for (let i = start; i >= Math.max(start - 400, 0); i--) {
    if ('.?!'.includes(text[i]) && (i + 1 >= text.length || ' \n\t\r'.includes(text[i + 1]))) {
      return i + 1;
    }
  }
  for (let i = start; i < Math.min(start + 400, text.length); i++) {
    if ('.?!'.includes(text[i]) && (i + 1 >= text.length || ' \n\t\r'.includes(text[i + 1]))) {
      return i + 1;
    }
  }
  return target;
}

function countChunks(text, targetSize) {
  let count = 0;
  let remaining = text.trim();
  while (remaining.length > 0) {
    count++;
    if (remaining.length <= targetSize) break;
    const breakAt = _findSentenceBreak(remaining, targetSize);
    remaining = remaining.substring(breakAt).trim();
  }
  return count;
}

function updateChunkPreview() {
  const ta = document.getElementById('approvalTextarea');
  const sizeInput = document.getElementById('chunkSizeInput');
  const preview = document.getElementById('chunkPreview');
  if (!ta || !sizeInput || !preview) return;
  const text = ta.value.trim();
  const size = parseInt(sizeInput.value, 10) || 1500;
  if (!text) { preview.textContent = ''; return; }
  const n = countChunks(text, size);
  preview.textContent = `→ ~${n} chunk${n !== 1 ? 's' : ''} para este script`;
}

// ── Script Approval ───────────────────────────────────────────────────────

// Mark textarea as user-edited so polling won't overwrite the content
document.addEventListener('DOMContentLoaded', () => {
  const ta = document.getElementById('approvalTextarea');
  if (ta) {
    ta.addEventListener('input', () => { ta.dataset.edited = '1'; });
  }
});

async function approveScript() {
  if (!currentProjectId) return;
  const ta = document.getElementById('approvalTextarea');
  const script_final = ta.value.trim();
  if (!script_final) {
    showToast('El script no puede estar vacío.', 'error');
    return;
  }
  const sizeInput = document.getElementById('chunkSizeInput');
  const target_chunk_size = parseInt(sizeInput?.value || '1500', 10) || 1500;
  try {
    await apiFetch(`/api/projects/${currentProjectId}/approve-script`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ script_final, target_chunk_size }),
    });
    ta.dataset.edited = '';
    showToast('Script aprobado. Dividiendo en chunks…', 'success');
    // Resume polling
    if (!pollInterval) {
      pollInterval = setInterval(() => refreshDetail(currentProjectId), 4000);
    }
    refreshDetail(currentProjectId);
  } catch (e) {
    showToast('Error al aprobar: ' + e.message, 'error');
  }
}

async function resplitChunks() {
  if (!currentProjectId) return;
  const input = document.getElementById('resplitInput');
  const target_chunk_size = parseInt(input?.value || '1500', 10) || 1500;
  try {
    await apiFetch(`/api/projects/${currentProjectId}/resplit`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target_chunk_size }),
    });
    showToast(`Re-dividiendo en chunks de ${target_chunk_size} chars…`, 'info');
    setTimeout(() => refreshDetail(currentProjectId), 1200);
  } catch (e) {
    showToast('Error al re-dividir: ' + e.message, 'error');
  }
}

async function regenerateScript() {
  if (!currentProjectId) return;
  if (!confirm('¿Regenerar el script desde el outline actual? Se perderá el script actual.')) return;
  try {
    const ta = document.getElementById('approvalTextarea');
    ta.dataset.edited = '';
    await apiFetch(`/api/projects/${currentProjectId}/regenerate-script`, { method: 'POST' });
    showToast('Regenerando script…', 'info');
    if (!pollInterval) {
      pollInterval = setInterval(() => refreshDetail(currentProjectId), 4000);
    }
    refreshDetail(currentProjectId);
  } catch (e) {
    showToast('Error al regenerar: ' + e.message, 'error');
  }
}

// ── Actions ───────────────────────────────────────────────────────────────
async function deleteProject(id) {
  if (!confirm('¿Borrar este proyecto? Esta acción es irreversible.')) return;
  try {
    await apiFetch(`/api/projects/${id}`, { method: 'DELETE' });
    showToast('Proyecto borrado', 'info');
    loadDashboard();
  } catch (e) {
    showToast('Error al borrar: ' + e.message, 'error');
  }
}

async function retryProject(id) {
  try {
    await apiFetch(`/api/projects/${id}/retry`, { method: 'POST' });
    showToast('Reintentando…', 'info');
    openDetail(id);
  } catch (e) {
    showToast('Error al reintentar: ' + e.message, 'error');
  }
}

// ── Worker status ─────────────────────────────────────────────────────────
async function updateWorkerStatus() {
  try {
    const workers = await apiFetch('/api/workers/');
    const active = workers.filter(w => w.status === 'busy').length;
    document.getElementById('workerCount').textContent = active;
    const dot = document.querySelector('.worker-dot');
    dot.className = `worker-dot ${active > 0 ? 'active' : 'idle'}`;
  } catch (e) { /* ignore */ }
}

// Refresh worker status every 5s
setInterval(updateWorkerStatus, 5000);

// ── Utilities ─────────────────────────────────────────────────────────────
async function apiFetch(url, options = {}) {
  const res = await fetch(url, options);
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { const d = await res.json(); msg = d.detail || JSON.stringify(d); } catch {}
    throw new Error(msg);
  }
  if (res.status === 204) return null;
  return res.json();
}

function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function showToast(message, type = 'info', duration = 4000) {
  const container = document.getElementById('toastContainer');
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.animation = 'fadeOut 0.4s ease forwards';
    setTimeout(() => toast.remove(), 400);
  }, duration);
}

// ── Voice Config ──────────────────────────────────────────────────────────

// Voice browser state (GenAIPro)
let _allVoices      = [];
let _selectedVoiceId   = '';
let _selectedVoiceName = '';

/**
 * Per-provider field definitions (sliders / selects shown below the voice section).
 * GenAIPro: voice is selected from the list browser, not a text field.
 */
const TTS_PROVIDER_FIELDS = {
  genaipro: [
    { id: 'model_id', label: 'Modelo', type: 'select',
      options: [
        { value: 'eleven_multilingual_v2', label: 'Multilingual v2 (recomendado)' },
        { value: 'eleven_monolingual_v1',  label: 'Monolingual v1 (inglés)' },
        { value: 'eleven_turbo_v2',        label: 'Turbo v2 (rápido)' },
      ],
      default: 'eleven_multilingual_v2',
    },
    { id: 'speed',      label: 'Velocidad',   type: 'range', min: 0.7, max: 1.2,  step: 0.05, default: 1.0  },
    { id: 'stability',  label: 'Estabilidad', type: 'range', min: 0,   max: 1,    step: 0.05, default: 0.5  },
    { id: 'similarity', label: 'Similarity',  type: 'range', min: 0,   max: 1,    step: 0.05, default: 0.75 },
    { id: 'style',      label: 'Style',       type: 'range', min: 0,   max: 1,    step: 0.05, default: 0.0  },
  ],
  elevenlabs: [
    { id: 'voice_id',   label: 'Voice ID', type: 'text', placeholder: 'Ej: EXAVITQu4vr4xnSDxMaL', default: '', fullWidth: true },
    { id: 'model_id',   label: 'Modelo', type: 'select',
      options: [
        { value: 'eleven_multilingual_v2', label: 'Multilingual v2 (recomendado)' },
        { value: 'eleven_monolingual_v1',  label: 'Monolingual v1 (inglés)' },
        { value: 'eleven_turbo_v2',        label: 'Turbo v2 (rápido)' },
      ],
      default: 'eleven_multilingual_v2',
    },
    { id: 'stability',  label: 'Estabilidad', type: 'range', min: 0, max: 1, step: 0.05, default: 0.5 },
    { id: 'similarity', label: 'Similarity Boost', type: 'range', min: 0, max: 1, step: 0.05, default: 0.75 },
  ],
  openai: [
    { id: 'voice', label: 'Voz', type: 'select',
      options: [
        { value: 'alloy',   label: 'Alloy (neutral)' },
        { value: 'echo',    label: 'Echo (masculino)' },
        { value: 'fable',   label: 'Fable (expresivo)' },
        { value: 'onyx',    label: 'Onyx (profundo)' },
        { value: 'nova',    label: 'Nova (femenino)' },
        { value: 'shimmer', label: 'Shimmer (suave)' },
      ],
      default: 'alloy',
    },
    { id: 'model', label: 'Modelo', type: 'select',
      options: [
        { value: 'tts-1',    label: 'TTS-1 (rápido)' },
        { value: 'tts-1-hd', label: 'TTS-1-HD (alta calidad)' },
      ],
      default: 'tts-1',
    },
    { id: 'speed', label: 'Velocidad', type: 'range', min: 0.25, max: 4.0, step: 0.05, default: 1.0, fullWidth: true },
  ],
};

function onProviderChange() {
  const provider = document.getElementById('ttsProvider')?.value || 'genaipro';

  // Show voice list browser only for GenAIPro
  const voiceListSection = document.getElementById('voiceListSection');
  if (voiceListSection) voiceListSection.style.display = provider === 'genaipro' ? '' : 'none';

  const container = document.getElementById('ttsProviderFields');
  if (!container) return;

  const fields = TTS_PROVIDER_FIELDS[provider] || [];
  container.innerHTML = '';

  if (fields.length === 0) return;

  // Provider label
  const badge = document.createElement('div');
  badge.className = 'vconfig-provider-badge';
  badge.textContent = `Opciones de ${provider}`;
  container.appendChild(badge);

  fields.forEach(f => {
    const group = document.createElement('div');
    group.className = `vconfig-group${f.fullWidth ? ' full' : ''}`;

    const label = document.createElement('label');
    label.className = 'vconfig-label';
    label.textContent = f.label;
    group.appendChild(label);

    if (f.type === 'text') {
      const inp = document.createElement('input');
      inp.type = 'text';
      inp.id = `tts_${f.id}`;
      inp.className = 'vconfig-input';
      inp.placeholder = f.placeholder || '';
      inp.value = f.default || '';
      group.appendChild(inp);

    } else if (f.type === 'select') {
      const sel = document.createElement('select');
      sel.id = `tts_${f.id}`;
      sel.className = 'vconfig-select';
      (f.options || []).forEach(opt => {
        const o = document.createElement('option');
        o.value = opt.value;
        o.textContent = opt.label;
        if (opt.value === f.default) o.selected = true;
        sel.appendChild(o);
      });
      group.appendChild(sel);

    } else if (f.type === 'range') {
      const row = document.createElement('div');
      row.className = 'vconfig-range-row';

      const range = document.createElement('input');
      range.type = 'range';
      range.id = `tts_${f.id}`;
      range.className = 'vconfig-range';
      range.min = f.min;
      range.max = f.max;
      range.step = f.step;
      range.value = f.default;

      const val = document.createElement('span');
      val.className = 'vconfig-range-val';
      val.id = `tts_${f.id}_val`;
      val.textContent = f.default;

      range.addEventListener('input', () => { val.textContent = parseFloat(range.value).toFixed(2); });

      row.appendChild(range);
      row.appendChild(val);
      group.appendChild(row);
    }

    container.appendChild(group);
  });
}

// ── Voice browser (GenAIPro) ───────────────────────────────────────────────

async function loadVoices() {
  const apiKey = document.getElementById('ttsApiKey')?.value?.trim();
  if (!apiKey) { showToast('Ingresa tu API key primero.', 'error'); return; }

  const btn = document.querySelector('#voiceListSection .btn-ghost');
  const origText = btn ? btn.textContent : '🔃 Cargar voces';
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Cargando…'; }

  try {
    const data = await apiFetch('/api/tts/voices', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tts_provider: 'genaipro', tts_api_key: apiKey }),
    });
    _allVoices = data.voices || [];
    renderVoiceList(_allVoices);
    showToast(`${_allVoices.length} voces cargadas ✓`, 'success');
  } catch (e) {
    showToast('Error cargando voces: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = origText; }
  }
}

function filterVoices() {
  const q = (document.getElementById('voiceSearch')?.value || '').toLowerCase();
  const filtered = q
    ? _allVoices.filter(v =>
        (v.name     || '').toLowerCase().includes(q) ||
        (v.gender   || '').toLowerCase().includes(q) ||
        (v.accent   || '').toLowerCase().includes(q) ||
        (v.language || '').toLowerCase().includes(q)
      )
    : _allVoices;
  renderVoiceList(filtered);
}

function renderVoiceList(voices) {
  const list = document.getElementById('voiceList');
  if (!list) return;

  if (!voices.length) {
    list.innerHTML = '<div class="voice-list-empty">No se encontraron voces.</div>';
    return;
  }

  list.innerHTML = '';
  voices.forEach(v => {
    const item = document.createElement('div');
    item.className = 'voice-item' + (v.voice_id === _selectedVoiceId ? ' selected' : '');
    const tags = [v.gender, v.accent, v.language].filter(Boolean);
    item.innerHTML = `
      <div class="voice-item-name">${escHtml(v.name || v.voice_id || '—')}</div>
      ${tags.length ? `<div class="voice-item-tags">${tags.map(t => `<span class="voice-tag">${escHtml(t)}</span>`).join('')}</div>` : ''}
    `;
    item.addEventListener('click', () => selectVoice(v));
    list.appendChild(item);
  });
}

function selectVoice(voice) {
  _selectedVoiceId   = voice.voice_id || voice.id || '';
  _selectedVoiceName = voice.name || _selectedVoiceId;

  const display = document.getElementById('selectedVoiceDisplay');
  const nameEl  = document.getElementById('selectedVoiceName');
  if (nameEl)  nameEl.textContent   = _selectedVoiceName;
  if (display) display.style.display = '';

  renderVoiceList(_allVoices); // re-render to update selected highlight
}

// ── Voice config collection ───────────────────────────────────────────────

function _collectVoiceConfig() {
  const provider = document.getElementById('ttsProvider')?.value || 'genaipro';
  const api_key  = document.getElementById('ttsApiKey')?.value?.trim() || '';

  const fields = TTS_PROVIDER_FIELDS[provider] || [];
  const extraConfig = {};
  fields.forEach(f => {
    const el = document.getElementById(`tts_${f.id}`);
    if (!el) return;
    if (f.type === 'range') {
      extraConfig[f.id] = parseFloat(el.value);
    } else {
      extraConfig[f.id] = el.value;
    }
  });

  // For GenAIPro: voice_id comes from the voice browser; for others from a text field
  let voice_id;
  if (provider === 'genaipro') {
    voice_id = _selectedVoiceId || null;
    // Persist voice name so it can be restored across page loads
    if (_selectedVoiceName && _selectedVoiceName !== _selectedVoiceId) {
      extraConfig['voice_name'] = _selectedVoiceName;
    }
  } else {
    voice_id = extraConfig['voice_id'] || null;
    if (voice_id !== null) delete extraConfig['voice_id'];
  }

  return {
    tts_provider: provider,
    tts_api_key:  api_key,
    tts_voice_id: voice_id || null,
    tts_config:   JSON.stringify(extraConfig),
  };
}

async function testVoice() {
  if (!currentProjectId) return;
  const cfg = _collectVoiceConfig();
  if (!cfg.tts_api_key) {
    showToast('Ingresa tu API key primero.', 'error');
    return;
  }
  if (!cfg.tts_voice_id) {
    showToast('Selecciona una voz primero.', 'error');
    return;
  }

  const btn = document.querySelector('.voice-config-actions-top .btn-ghost');
  const origText = btn ? btn.textContent : '🔊 Probar Voz';
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Generando…'; }

  try {
    const resp = await fetch(`/api/projects/${currentProjectId}/test-voice`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(cfg),
    });

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      throw new Error(err.detail || resp.statusText);
    }

    const blob = await resp.blob();
    const url  = URL.createObjectURL(blob);

    const container = document.getElementById('testAudioContainer');
    const audio     = document.getElementById('testAudio');
    if (audio) {
      if (audio.src && audio.src.startsWith('blob:')) URL.revokeObjectURL(audio.src);
      audio.src = url;
      audio.play().catch(() => {});
    }
    if (container) container.style.display = '';
    showToast('Vista previa generada ✓', 'success');
  } catch (e) {
    showToast('Error al probar voz: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = origText; }
  }
}

async function generateVoiceover() {
  if (!currentProjectId) return;
  const cfg = _collectVoiceConfig();
  if (!cfg.tts_api_key) {
    showToast('Ingresa tu API key primero.', 'error');
    return;
  }
  if (!cfg.tts_voice_id) {
    showToast('Selecciona una voz primero.', 'error');
    return;
  }

  const btn = document.querySelector('.voice-config-actions-top .btn-primary');
  const origText = btn ? btn.textContent : '🎙️ Generar Voiceover Completo';
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Iniciando…'; }

  try {
    await apiFetch(`/api/projects/${currentProjectId}/generate-voiceover`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(cfg),
    });
    showToast('Generando voiceover… revisa los logs para ver el progreso.', 'info');

    // Status changes to 'processing' — restart polling to track progress
    stopPolling();
    await refreshDetail(currentProjectId);
    pollInterval = setInterval(() => refreshDetail(currentProjectId), 4000);
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
    if (btn) { btn.disabled = false; btn.textContent = origText; }
  }
}

// ── Audio Approval ────────────────────────────────────────────────────────

async function approveAudio() {
  if (!currentProjectId) return;
  if (!confirm('¿Aprobar el voiceover y continuar con la generación de video?')) return;

  const btn = document.querySelector('#voiceoverApprovalSection .btn-success');
  const origText = btn ? btn.textContent : '✅ Aprobar Audio y Continuar';
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Iniciando…'; }

  try {
    await apiFetch(`/api/projects/${currentProjectId}/approve-audio`, { method: 'POST' });
    showToast('Audio aprobado. Iniciando generación de video…', 'success');
    stopPolling();
    await refreshDetail(currentProjectId);
    pollInterval = setInterval(() => refreshDetail(currentProjectId), 4000);
  } catch (e) {
    showToast('Error al aprobar: ' + e.message, 'error');
    if (btn) { btn.disabled = false; btn.textContent = origText; }
  }
}

async function regenerateVoiceover() {
  if (!currentProjectId) return;
  if (!confirm('¿Descartar el voiceover actual y volver a configurar la voz?')) return;

  try {
    await apiFetch(`/api/projects/${currentProjectId}/regenerate-voiceover`, { method: 'POST' });
    showToast('Voiceover descartado. Configura la voz y genera de nuevo.', 'info');
    await refreshDetail(currentProjectId);
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
  }
}

// ── Init ──────────────────────────────────────────────────────────────────
loadDashboard();
