/* ──────────────────────────────────────────────────────────────────────────
   YouTube Video Creator – Frontend JS
   ────────────────────────────────────────────────────────────────────────── */

// ── State ─────────────────────────────────────────────────────────────────
let currentProjectId = null;
let pollInterval = null;
let logEventSource = null;
let lastLogId = 0;

// Cached settings (loaded on demand)
let _settings = {};

/** Return capitalised image provider name from settings. */
function _imgProviderName() {
  const p = (_settings['image_provider'] || 'wavespeed');
  return p.charAt(0).toUpperCase() + p.slice(1);
}

// Reference YouTube videos collected in the form [{url, title, transcript}]
const referenceVideos = [];

const STATUS_ICONS = {
  queued: '🕐',
  processing: '⚙️',
  awaiting_approval: '✏️',
  awaiting_voice_config: '🎙️',
  awaiting_audio_approval: '🎵',
  audio_approved: '✅',
  scenes_ready: '🎬',
  generating_images: '🖼️',
  images_ready: '✅',
  done: '✅',
  error: '❌',
};

const MODE_ICONS = {
  animated: '🎨',
  stock: '📹',
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
  if (name === 'settings') loadSettingsPage();
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
            <span>${new Date(p.created_at).toLocaleDateString('es-ES', { day: '2-digit', month: 'short', year: 'numeric' })}</span>
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



// ── Detail view ───────────────────────────────────────────────────────────
async function openDetail(projectId) {
  currentProjectId = projectId;
  _outlineOpen = false;
  _selectedVoiceId = '';
  _selectedVoiceName = '';
  _allVoices = [];

  // Pre-load settings so the API key warning is accurate when voice config shows
  await _fetchSettings();

  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.getElementById('view-detail').classList.add('active');

  // Reset all sections
  document.getElementById('progressCard').style.display = 'none';
  document.getElementById('scriptSection').style.display = 'none';
  document.getElementById('voiceConfigSection').style.display = 'none';
  document.getElementById('voiceoverApprovalSection').style.display = 'none';
  document.getElementById('chunksSection').style.display = 'none';
  document.getElementById('videoPreviewContainer').style.display = 'none';
  document.getElementById('chunkGrid').innerHTML = '';
  document.getElementById('chunksList').innerHTML = '';
  document.getElementById('logsContainer').innerHTML = '<div class="log-placeholder">Cargando logs\u2026</div>';

  await refreshDetail(projectId);

  startLogStream(projectId);
  pollInterval = setInterval(() => refreshDetail(projectId), 4000);
}

async function refreshDetail(projectId) {
  try {
    const p = await apiFetch(`/api/projects/${projectId}`);
    const chunks = p.chunks || [];

    // ── Header ───────────────────────────────────────────────────────────
    document.getElementById('detailTitle').textContent = p.title;
    const badge = document.getElementById('detailBadge');
    badge.textContent = p.status.toUpperCase();
    badge.className = `badge badge-${p.status}`;

    // ── Progress card (queued / processing / error) ───────────────────────
    const progressCard = document.getElementById('progressCard');
    const isRunning = ['queued', 'processing', 'error'].includes(p.status);
    progressCard.style.display = isRunning ? '' : 'none';
    if (isRunning) {
      const total = chunks.length;
      const done = chunks.filter(c => c.status === 'done').length;
      const pct = total > 0 ? Math.round((done / total) * 100) : 0;
      document.getElementById('progressBar').style.width = `${pct}%`;
      document.getElementById('progressPct').textContent = `${pct}%`;
      document.getElementById('progressLabel').textContent =
        p.status === 'error' ? `Error: ${p.error_message || ''}` :
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



    // ── 2. Script section (editable ↔ read-only) ──────────────────────────
    const scriptSection = document.getElementById('scriptSection');
    const approvalTextarea = document.getElementById('approvalTextarea');
    const scriptContent = document.getElementById('scriptContent');
    const scriptApprovalControls = document.getElementById('scriptApprovalControls');
    const scriptDoneBadge = document.getElementById('scriptDoneBadge');
    const chunkConfigBar = document.getElementById('chunkConfigBar');
    const scriptHint = document.getElementById('scriptHint');

    if (p.script || p.script_final) {
      scriptSection.style.display = '';

      if (p.status === 'awaiting_approval') {
        // — Editable mode —
        scriptSection.classList.add('script-awaiting');
        approvalTextarea.style.display = '';
        scriptContent.style.display = 'none';
        scriptApprovalControls.style.display = '';
        scriptDoneBadge.style.display = 'none';
        chunkConfigBar.style.display = '';
        scriptHint.textContent = 'Edita el texto si lo deseas, luego aprueba para continuar.';

        if (!approvalTextarea.dataset.edited) {
          approvalTextarea.value = p.script;
          const sizeInput = document.getElementById('chunkSizeInput');
          if (sizeInput && p.target_chunk_size) sizeInput.value = p.target_chunk_size;
          updateChunkPreview();
        }
        updateWordCount();
      } else {
        // — Read-only mode —
        scriptSection.classList.remove('script-awaiting');
        approvalTextarea.dataset.edited = '';
        approvalTextarea.style.display = 'none';
        const wcContainer = document.getElementById('scriptWordCount');
        if (wcContainer) wcContainer.style.display = 'none';
        scriptContent.style.display = '';
        scriptContent.textContent = p.script_final || p.script;
        scriptApprovalControls.style.display = 'none';
        scriptDoneBadge.style.display = '';
        chunkConfigBar.style.display = 'none';
        scriptHint.textContent = '';
      }
    } else {
      scriptSection.style.display = 'none';
    }

    // ── 3. Voice config (awaiting_voice_config only) ──────────────────────
    const voiceConfigSection = document.getElementById('voiceConfigSection');
    const voiceChunksSummary = document.getElementById('voiceChunksSummary');
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

      // Restore saved voice selection
      if (p.tts_voice_id && !_selectedVoice.voice_id) {
        let voiceName = p.tts_voice_id;
        let voiceDesc = '';
        try {
          const cfg = JSON.parse(p.tts_config || '{}');
          if (cfg.voice_name) voiceName = cfg.voice_name;
        } catch (_) { }
        _selectedVoice = { ..._selectedVoice, voice_id: p.tts_voice_id, name: voiceName, description: voiceDesc };
        updateVoiceCard();
      }

      // Load voices if not yet loaded
      if (!_allVoices.length) initVoiceConfig();
    } else {
      voiceConfigSection.style.display = 'none';
    }

    // ── 3b. Voiceover section — visible whenever a voiceover exists ───────
    // Audio player stays visible after approval; buttons hide once approved.
    const approvalSection = document.getElementById('voiceoverApprovalSection');
    const isAwaitingAudio = p.status === 'awaiting_audio_approval';
    const isAudioApproved = p.status === 'audio_approved';
    const isScenesReady = p.status === 'scenes_ready';
    const isGeneratingImages = p.status === 'generating_images';
    const isImagesReady = p.status === 'images_ready';
    const isErrorWithVO = p.status === 'error' && !!p.voiceover_path;
    const showImagePanel = isScenesReady || isGeneratingImages || isImagesReady;
    const hasVoiceover = !!p.voiceover_path;

    if (approvalSection && hasVoiceover) {
      approvalSection.style.display = '';

      // Load waveform player for approval section
      const approvalWaveCanvas = document.getElementById('approvalWaveCanvas');
      const audioUrl = `/api/projects/${p.id}/voiceover/audio?t=${new Date(p.updated_at).getTime() || Date.now()}`;
      if (approvalWaveCanvas && approvalWaveCanvas.dataset.src !== audioUrl) {
        approvalWaveCanvas.dataset.src = audioUrl;
        fetch(audioUrl).then(r => r.blob()).then(blob => renderWaveformPlayer(blob, 'approval')).catch(() => {});
      }

      // Show approve/regenerate buttons only when pending approval
      const approvalActions = approvalSection.querySelector('.voiceover-approval-actions');
      if (approvalActions) approvalActions.style.display = isAwaitingAudio ? '' : 'none';

      // Show "Continuar con Escenas" button only when audio is approved
      const continueActions = document.getElementById('voiceoverContinueActions');
      if (continueActions) continueActions.style.display = isAudioApproved ? '' : 'none';

      // Show "Reintentar" button when errored but voiceover exists
      const retryActions = document.getElementById('voiceoverRetryActions');
      if (retryActions) retryActions.style.display = isErrorWithVO ? '' : 'none';

      // Update badge
      const badge = approvalSection.querySelector('.badge');
      if (badge) {
        if (isAwaitingAudio) badge.textContent = '✓ Listo para revisar';
        else if (isAudioApproved) badge.textContent = '✓ APROBADO';
        else if (isScenesReady) badge.textContent = '✓ Aprobado';
        else badge.textContent = '✓ Aprobado';
      }
    } else if (approvalSection) {
      approvalSection.style.display = 'none';
    }

    // ── 4. Escenas — visible desde scenes_ready en adelante ──────────────
    const chunksSection = document.getElementById('chunksSection');
    const hiddenStatuses = ['awaiting_voice_config', 'awaiting_audio_approval', 'audio_approved'];
    if (chunks.length > 0 && !hiddenStatuses.includes(p.status)) {
      chunksSection.style.display = '';
      const countEl = document.getElementById('chunksCount');
      if (countEl) {
        if (showImagePanel) {
          const doneImgs = chunks.filter(c => c.image_path).length;
          countEl.textContent = doneImgs > 0
            ? `— ${chunks.length} escenas · ${doneImgs} con imagen`
            : `— ${chunks.length} escenas`;
        } else {
          countEl.textContent = `— ${chunks.length} escenas`;
        }
      }

      const list = document.getElementById('chunksList');
      list.innerHTML = '';
      chunks.forEach(c => {
        const text = c.scene_text || '';
        const preview = text.length > 100 ? text.slice(0, 100) + '…' : text;
        const chars = text.length.toLocaleString('es-ES');

        // Timing info from start_ms / end_ms
        let timeHtml = '';
        if (c.start_ms != null && c.end_ms != null) {
          const fmtTime = (ms) => {
            const totalSec = Math.floor(ms / 1000);
            const m = Math.floor(totalSec / 60);
            const s = totalSec % 60;
            return `${m}:${String(s).padStart(2, '0')}`;
          };
          const durSec = ((c.end_ms - c.start_ms) / 1000).toFixed(1);
          timeHtml = `<span class="chunk-card-time">${fmtTime(c.start_ms)} — ${fmtTime(c.end_ms)}</span><span class="chunk-card-dur">${durSec}s</span>`;
        }

        // Image thumbnail — use the dedicated API endpoint to avoid Windows path issues
        let imgHtml = '';
        if (c.image_path) {
          imgHtml = `<img class="chunk-img-thumb" src="/api/projects/${p.id}/chunk/${c.chunk_number}/image?t=${Date.now()}" alt="Escena ${c.chunk_number}" loading="lazy" />`;
        }

        // Generated image prompt (collapsible)
        let promptHtml = '';
        if (c.image_prompt) {
          promptHtml = `
            <div class="chunk-prompt-section">
              <div class="chunk-prompt-header" onclick="toggleChunkPrompt(${c.chunk_number})">
                <span>🔎 Prompt generado</span>
                <span class="chunk-prompt-toggle" id="prompt-toggle-${c.chunk_number}">▼</span>
              </div>
              <div class="chunk-prompt-body" id="prompt-body-${c.chunk_number}" style="display:none">${escHtml(c.image_prompt)}</div>
            </div>`;
        }

        // Error message
        let errorHtml = '';
        if (c.status === 'error' && c.error_message) {
          errorHtml = `<div class="chunk-error-box">${escHtml(c.error_message)}</div>`;
        }

        // Retry button (visible when error and in image generation phase)
        let retryHtml = '';
        if (c.status === 'error' && showImagePanel) {
          retryHtml = `<button class="chunk-retry-btn" onclick="retryChunkImage(${c.chunk_number})">🔄 Reintentar imagen</button>`;
        }

        // Pollinations regenerate button (visible when image_prompt exists and in image panel)
        let regenHtml = '';
        if (c.image_prompt && showImagePanel) {
          regenHtml = `<button class="chunk-regen-btn" onclick="regenerateImageGenaipro(${c.chunk_number})">⚡ Rehacer Imagen (${_imgProviderName()})</button>`;
        }

        // Motion Prompt logic (visible if project is in a state where it generated images)
        let motionHtml = '';
        if (p.status === 'images_ready' || p.status === 'done' || p.status === 'error' || p.status === 'animating' || p.status === 'motion_prompts_ready') {
          const motionVal = c.motion_prompt || '';
          motionHtml = `
            <div class="chunk-motion-section" style="margin-top:10px;">
              <div style="font-size:12px; font-weight:600; margin-bottom:4px;">🎥 Movimiento:</div>
              <div style="display:flex; gap:8px;">
                <textarea id="motion_prompt_${c.chunk_number}" rows="2" style="flex:1; padding:4px; font-size:12px; border:1px solid var(--border-color); border-radius:4px; background:var(--bg-card); color:var(--text-main);">${escHtml(motionVal)}</textarea>
                <button class="btn btn-ghost btn-sm" onclick="saveMotionPrompt(${c.chunk_number})">💾</button>
              </div>
            </div>`;
        }

        const card = document.createElement('div');
        card.className = 'chunk-card';
        card.innerHTML = `
          <div class="chunk-card-header" onclick="toggleChunkCard(${c.chunk_number})">
            <span class="chunk-card-num">Escena #${c.chunk_number}</span>
            ${c.image_path ? '<span class="chunk-img-badge">🖼️</span>' : ''}
            <span class="chunk-card-preview">${escHtml(preview)}</span>
            <span class="chunk-card-chars">${chars} chars</span>
            ${timeHtml}
            <span class="chunk-card-status ${c.status}">${c.status}</span>
            <span class="chunk-card-toggle" id="toggle-${c.chunk_number}">▼</span>
          </div>
          <div class="chunk-card-body" id="chunk-body-${c.chunk_number}" style="display:none">
            ${imgHtml}
            ${promptHtml}
            ${motionHtml}
            ${c.video_path ? `<div style="margin-top:10px"><video src="/api/projects/${p.id}/chunk/${c.chunk_number}/video?t=${Date.now()}" controls style="max-width:100%"></video></div>` : ''}
            ${errorHtml}
            ${retryHtml}
            ${regenHtml}
            <div class="chunk-card-text">${escHtml(text)}</div>
          </div>
        `;
        list.appendChild(card);
      });
    } else {
      chunksSection.style.display = 'none';
    }

    // ── 4b. Imagen panel — visible en scenes_ready / generating_images / images_ready ──
    const scenesReadySection = document.getElementById('scenesReadySection');
    if (scenesReadySection) {
      scenesReadySection.style.display = showImagePanel ? '' : 'none';

      // Character reference UI
      const charPreview = document.getElementById('refCharPreview');
      const charThumb = document.getElementById('refCharThumb');
      const charStatus = document.getElementById('refCharStatus');
      const deleteCharBtn = document.getElementById('deleteRefCharBtn');
      if (p.reference_character_path) {
        if (charPreview) charPreview.style.display = '';
        if (charThumb) charThumb.src = `/api/projects/${p.id}/reference-character?t=${Date.now()}`;
        if (charStatus) { charStatus.textContent = '✅'; charStatus.style.color = '#2ecc71'; }
        if (deleteCharBtn) deleteCharBtn.style.display = '';
      } else {
        if (charPreview) charPreview.style.display = 'none';
        if (charStatus) { charStatus.textContent = ''; charStatus.style.color = ''; }
        if (deleteCharBtn) deleteCharBtn.style.display = 'none';
      }
      // Style reference UI
      const stylePreview = document.getElementById('refStylePreview');
      const styleThumb = document.getElementById('refStyleThumb');
      const styleStatus = document.getElementById('refStyleStatus');
      const deleteStyleBtn = document.getElementById('deleteRefStyleBtn');
      if (p.reference_style_path) {
        if (stylePreview) stylePreview.style.display = '';
        if (styleThumb) styleThumb.src = `/api/projects/${p.id}/reference-style?t=${Date.now()}`;
        if (styleStatus) { styleStatus.textContent = '✅'; styleStatus.style.color = '#2ecc71'; }
        if (deleteStyleBtn) deleteStyleBtn.style.display = '';
      } else {
        if (stylePreview) stylePreview.style.display = 'none';
        if (styleStatus) { styleStatus.textContent = ''; styleStatus.style.color = ''; }
        if (deleteStyleBtn) deleteStyleBtn.style.display = 'none';
      }

      if (showImagePanel) {
        const doneImgs = chunks.filter(c => c.image_path).length;
        const label = document.getElementById('scenesReadyLabel');
        const progressCount = document.getElementById('imagesProgressCount');
        const generateBtn = document.getElementById('generateImagesBtn');
        const continueBtn = document.getElementById('continueWithVideoBtn');
        const hint = document.getElementById('scenesReadyHint');

        if (isGeneratingImages) {
          if (label) label.textContent = `🎨 Generando escena ${doneImgs} de ${chunks.length}…`;
          if (progressCount) { progressCount.style.display = ''; progressCount.textContent = `${doneImgs} de ${chunks.length} escenas completadas`; }
          if (generateBtn) { generateBtn.style.display = ''; generateBtn.disabled = true; generateBtn.textContent = `⏳ Generando con ${_imgProviderName()}…`; }
          if (continueBtn) continueBtn.style.display = 'none';
          if (hint) hint.textContent = 'Google Imagen 4 Fast está procesando cada escena. Puedes seguir viendo su progreso en vivo minimizando esta ventana.';
        } else if (isImagesReady) {
          const hasErrors = chunks.some(c => c.status === 'error');
          const doneVideos = chunks.filter(c => c.video_path).length;
          if (label) label.textContent = hasErrors
            ? `⚠️ ${doneImgs} de ${chunks.length} escenas generadas (con errores)`
            : `✅ ${doneImgs} imágenes · ${doneVideos} videos generados`;
          if (progressCount) { progressCount.style.display = ''; progressCount.textContent = `${doneImgs} imágenes · ${doneVideos} videos de ${chunks.length}`; }

          if (hasErrors) {
            if (generateBtn) {
              generateBtn.style.display = '';
              generateBtn.disabled = false;
              generateBtn.textContent = '🔄 Reintentar Escenas Fallidas';
              generateBtn.className = 'btn btn-warning btn-lg';
            }
          } else {
            if (generateBtn) generateBtn.style.display = 'none';
          }

          if (continueBtn) continueBtn.style.display = 'none';
          if (hint) hint.textContent = hasErrors
            ? 'Algunas escenas fallaron. Puedes reintentar las fallidas individualmente.'
            : `✅ Imágenes listas con ${_imgProviderName()}. Motion prompts generados para ajuste manual.`;

          // "Regenerar TODAS" button
          let regenAllBtn = document.getElementById('regenAllGenaipro');
          if (!regenAllBtn) {
            regenAllBtn = document.createElement('button');
            regenAllBtn.id = 'regenAllGenaipro';
            regenAllBtn.className = 'btn btn-warning btn-sm';
            regenAllBtn.style.marginTop = '8px';
            regenAllBtn.textContent = '⚠️ Regenerar TODAS las imágenes';
            regenAllBtn.onclick = regenerateAllGenaipro;
            hint.parentNode.insertBefore(regenAllBtn, hint.nextSibling);
          }
          regenAllBtn.style.display = '';

          // Show Veo3 Animation Block
          const veo3Section = document.getElementById('veo3AnimationSection');
          if (veo3Section) {
            veo3Section.style.display = 'block';
            const doneVids = chunks.filter(c => c.video_path).length;
            const progressEl = document.getElementById('animationProgress');
            if (progressEl) {
              progressEl.textContent = doneVids > 0 ? `${doneVids} de ${chunks.length} videos animados` : '';
            }
            const veoBtn = document.getElementById('startVeo3AnimationBtn');
            if (veoBtn) {
              if (doneVids === chunks.length && chunks.length > 0) {
                veoBtn.textContent = '✅ Todas las escenas animadas';
                veoBtn.disabled = true;
              } else if (doneVids > 0) {
                veoBtn.textContent = `🎬 Animar ${chunks.length - doneVids} escenas restantes`;
                veoBtn.disabled = false;
              }
            }
          }
        }
        else {
          // scenes_ready — ready to generate
          if (label) label.textContent = `✅ ${chunks.length} escenas listas para generar`;
          if (progressCount) progressCount.style.display = 'none';
          if (generateBtn) { generateBtn.style.display = ''; generateBtn.disabled = false; generateBtn.textContent = `🎨 Generar Imágenes (${_imgProviderName()})`; }
          if (continueBtn) continueBtn.style.display = 'none';
          if (hint) hint.textContent = `Gemini generará un prompt visual por escena, luego ${_imgProviderName()} creará la imagen (16:9).`;
        }
      }
    }

    // ── 5. Video final ────────────────────────────────────────────────────
    if (p.final_video_path) {
      const container = document.getElementById('videoPreviewContainer');
      const video = document.getElementById('videoPreview');
      const relPath = p.final_video_path.replace(/\\/g, '/').split('/projects/').pop();
      video.src = `/media/${relPath}`;
      container.style.display = '';
    } else {
      document.getElementById('videoPreviewContainer').style.display = 'none';
    }

    // ── Stop polling when in stable state ─────────────────────────────────
    if (['done', 'error', 'awaiting_approval', 'awaiting_voice_config', 'awaiting_audio_approval', 'audio_approved', 'scenes_ready', 'images_ready'].includes(p.status)) {
      // Keep polling while Veo3 animation is running (videos still being generated)
      const animating = p.status === 'images_ready' && chunks.some(c => c.image_path && !c.video_path);
      if (!animating) stopPolling();
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

// ── Settings ───────────────────────────────────────────────────────────────

const _PROVIDER_KEY_MAP = {
  genaipro: 'genaipro_api_key',
  elevenlabs: 'genaipro_api_key',  // ElevenLabs uses same Genaipro key (proxy)
  openai: 'anthropic_api_key', // fallback; adjust if OpenAI key is separate
};

async function _fetchSettings() {
  try {
    const result = await apiFetch('/api/settings/');
    _settings = result.data || {};
  } catch (e) {
    _settings = {};
  }
}

async function loadSettingsPage() {
  await _fetchSettings();

  const masked = '••••••••';
  const fields = [
    'anthropic_api_key', 'genaipro_api_key', 'pollinations_api_key', 'wavespeed_api_key',
    'google_api_key', 'pexels_api_key', 'pixabay_api_key',
    'image_provider',
    'default_tts_provider', 'default_tts_voice_id', 'default_tts_model_id',
    'default_tts_speed', 'default_tts_stability', 'default_tts_similarity', 'default_tts_style',
    'default_video_mode', 'default_image_interval',
  ];

  fields.forEach(key => {
    const el = document.getElementById(`setting_${key}`);
    if (!el) return;
    const val = _settings[key] || '';
    el.value = val;
    // Update slider display values
    const valDisplay = document.getElementById(`setting_${key}_val`);
    if (valDisplay && val) valDisplay.textContent = parseFloat(val).toFixed(2);
  });
}

async function saveSetting(key) {
  const el = document.getElementById(`setting_${key}`);
  if (!el) return;
  const value = el.value.trim();

  try {
    const result = await apiFetch('/api/settings/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ data: { [key]: value } }),
    });
    _settings = result.data || {};
    // Show the mask placeholder after saving so the user knows it's saved
    el.value = _settings[key] || '';
    showToast('✓ Guardado', 'success');
  } catch (e) {
    showToast('Error al guardar: ' + e.message, 'error');
  }
}

async function testGenaIproImage() {
  const resultEl = document.getElementById('genaipro-test-result');
  resultEl.style.display = 'block';
  resultEl.textContent = '⏳ Enviando prueba a Genaipro /veo/create-image… (60s timeout)';
  try {
    const result = await apiFetch('/api/settings/test-genaipro-image', { method: 'POST' });
    let txt = `Clave usada: ${result.api_key_suffix}\nPrompt: ${result.test_prompt}\n\n`;
    for (const r of (result.results || [])) {
      txt += `\n── ${r.strategy} ──\n`;
      if (r.error) {
        txt += `  ERROR: ${r.error}\n`;
      } else {
        txt += `  HTTP: ${r.http_status}  Content-Type: ${r.content_type}\n`;
        txt += `  Body:\n${r.body_preview}\n`;
      }
    }
    resultEl.textContent = txt;
  } catch (e) {
    resultEl.textContent = 'Error: ' + e.message;
  }
}

async function saveVoiceDefaults() {
  const data = {
    default_tts_provider: document.getElementById('setting_default_tts_provider')?.value || '',
    default_tts_voice_id: document.getElementById('setting_default_tts_voice_id')?.value?.trim() || '',
    default_tts_model_id: document.getElementById('setting_default_tts_model_id')?.value?.trim() || '',
    default_tts_speed: document.getElementById('setting_default_tts_speed')?.value || '1.0',
    default_tts_stability: document.getElementById('setting_default_tts_stability')?.value || '0.5',
    default_tts_similarity: document.getElementById('setting_default_tts_similarity')?.value || '0.75',
    default_tts_style: document.getElementById('setting_default_tts_style')?.value || '0.0',
  };
  try {
    const result = await apiFetch('/api/settings/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ data }),
    });
    _settings = result.data || {};
    showToast('✓ Configuración de voz guardada', 'success');
  } catch (e) {
    showToast('Error al guardar: ' + e.message, 'error');
  }
}

async function saveVideoDefaults() {
  const data = {
    default_video_mode: document.getElementById('setting_default_video_mode')?.value || 'animated',
    default_image_interval: document.getElementById('setting_default_image_interval')?.value || '5',
  };
  try {
    const result = await apiFetch('/api/settings/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ data }),
    });
    _settings = result.data || {};
    showToast('✓ Configuración de video guardada', 'success');
  } catch (e) {
    showToast('Error al guardar: ' + e.message, 'error');
  }
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

function toggleChunkPrompt(num) {
  const body = document.getElementById(`prompt-body-${num}`);
  const icon = document.getElementById(`prompt-toggle-${num}`);
  if (!body) return;
  const open = body.style.display === 'none';
  body.style.display = open ? '' : 'none';
  if (icon) icon.textContent = open ? '▲' : '▼';
}

async function retryChunkImage(chunkNumber) {
  if (!currentProjectId) return;
  const btn = event.target;
  const origText = btn.textContent;
  btn.disabled = true;
  btn.textContent = '⏳ Reintentando…';

  try {
    await apiFetch(`/api/projects/${currentProjectId}/retry-chunk-image/${chunkNumber}`, { method: 'POST' });
    showToast(`Reintentando imagen para escena #${chunkNumber}…`, 'info');
    stopPolling();
    await refreshDetail(currentProjectId);
    pollInterval = setInterval(() => refreshDetail(currentProjectId), 3000);
  } catch (e) {
    showToast('Error al reintentar: ' + e.message, 'error');
    btn.disabled = false;
    btn.textContent = origText;
  }
}

async function regenerateImageGenaipro(chunkNumber) {
  if (!currentProjectId) return;
  const btn = event.target;
  const origText = btn.textContent;
  btn.disabled = true;
  btn.textContent = '⏳ Regenerando…';

  try {
    await apiFetch(`/api/projects/${currentProjectId}/scenes/${chunkNumber}/regenerate-genaipro`, { method: 'POST' });
    showToast(`⚡ Regenerando imagen de escena #${chunkNumber} con ${_imgProviderName()}…`, 'info');
    stopPolling();
    await refreshDetail(currentProjectId);
    pollInterval = setInterval(() => refreshDetail(currentProjectId), 3000);
  } catch (e) {
    showToast('Error al regenerar: ' + e.message, 'error');
    btn.disabled = false;
    btn.textContent = origText;
  }
}

async function regenerateAllGenaipro() {
  if (!currentProjectId) return;
  const btn = document.getElementById('regenAllGenaipro');
  const origText = btn ? btn.textContent : '⚠️ Regenerar TODAS las imágenes (Imagen 4)';
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Iniciando regeneración masiva…'; }

  try {
    await apiFetch(`/api/projects/${currentProjectId}/regenerate-all-genaipro`, { method: 'POST' });
    showToast(`⚡ Regenerando TODAS las imágenes con ${_imgProviderName()} en segundo plano. Revisa los logs.`, 'info');
    stopPolling();
    await refreshDetail(currentProjectId);
    pollInterval = setInterval(() => refreshDetail(currentProjectId), 3000);
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
    if (btn) { btn.disabled = false; btn.textContent = origText; }
  }
}

// ── Reference Images (Character + Style) ──────────────────────────────────
async function uploadReferenceCharacter(input) {
  if (!currentProjectId || !input.files[0]) return;
  const formData = new FormData();
  formData.append('file', input.files[0]);
  try {
    await apiFetch(`/api/projects/${currentProjectId}/reference-character`, {
      method: 'POST', body: formData, isFormData: true,
    });
    showToast('Imagen de personaje subida', 'success');
    await refreshDetail(currentProjectId);
  } catch (e) {
    showToast('Error al subir personaje: ' + e.message, 'error');
  }
  input.value = '';
}

async function deleteReferenceCharacter() {
  if (!currentProjectId) return;
  try {
    await apiFetch(`/api/projects/${currentProjectId}/reference-character`, { method: 'DELETE' });
    showToast('Imagen de personaje eliminada', 'info');
    await refreshDetail(currentProjectId);
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
  }
}

async function uploadReferenceStyle(input) {
  if (!currentProjectId || !input.files[0]) return;
  const formData = new FormData();
  formData.append('file', input.files[0]);
  try {
    await apiFetch(`/api/projects/${currentProjectId}/reference-style`, {
      method: 'POST', body: formData, isFormData: true,
    });
    showToast('Imagen de estilo subida', 'success');
    await refreshDetail(currentProjectId);
  } catch (e) {
    showToast('Error al subir estilo: ' + e.message, 'error');
  }
  input.value = '';
}

async function deleteReferenceStyle() {
  if (!currentProjectId) return;
  try {
    await apiFetch(`/api/projects/${currentProjectId}/reference-style`, { method: 'DELETE' });
    showToast('Imagen de estilo eliminada', 'info');
    await refreshDetail(currentProjectId);
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
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
  if (!await showConfirm('¿Regenerar el script desde el outline actual? Se perderá el script actual.')) return;
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

// ── Edit Script Modal ──────────────────────────────────────────────────────

function openEditScriptModal() {
  const modal = document.getElementById('editScriptModal');
  if (!modal) return;
  modal.style.display = '';
  const ta = document.getElementById('editScriptPrompt');
  if (ta) { ta.value = ''; ta.focus(); }
}

function closeEditScriptModal() {
  const modal = document.getElementById('editScriptModal');
  if (modal) modal.style.display = 'none';
}

function setEditPrompt(text) {
  const ta = document.getElementById('editScriptPrompt');
  if (ta) { ta.value = text; ta.focus(); }
}

async function sendEditScriptPrompt() {
  if (!currentProjectId) return;
  const ta = document.getElementById('editScriptPrompt');
  const prompt = ta?.value?.trim();
  if (!prompt) { showToast('Escribe una instrucción primero.', 'error'); return; }

  const btn = document.getElementById('editScriptSendBtn');
  const btnText = document.getElementById('editScriptBtnText');
  if (btn) btn.disabled = true;
  if (btnText) btnText.textContent = '⏳ Procesando…';

  try {
    const data = await apiFetch(`/api/projects/${currentProjectId}/edit-script`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt }),
    });
    const scriptTA = document.getElementById('approvalTextarea');
    if (scriptTA) {
      scriptTA.value = data.script || '';
      scriptTA.dataset.edited = '1';
      updateChunkPreview();
      updateWordCount();
    }
    closeEditScriptModal();
    showToast('Guion actualizado por Claude ✓', 'success');
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
  } finally {
    if (btn) btn.disabled = false;
    if (btnText) btnText.textContent = '✨ Enviar a Claude';
  }
}

// ── Word count ─────────────────────────────────────────────────────────────

function updateWordCount() {
  const ta = document.getElementById('approvalTextarea');
  const countEl = document.getElementById('wordCountVal');
  const container = document.getElementById('scriptWordCount');
  if (!ta || !countEl) return;
  const words = ta.value.trim() ? ta.value.trim().split(/\s+/).length : 0;
  countEl.textContent = words.toLocaleString();
  if (container) container.style.display = ta.style.display === 'none' ? 'none' : '';
}

// ── Actions ───────────────────────────────────────────────────────────────
async function deleteProject(id) {
  if (!await showConfirm('¿Borrar este proyecto? Esta acción es irreversible.', 'Borrar')) return;
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
    try { const d = await res.json(); msg = d.detail || JSON.stringify(d); } catch { }
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

// ── Generic Confirm Modal ─────────────────────────────────────────────────
let _confirmResolve = null;

function showConfirm(message, okLabel = 'Aceptar') {
  return new Promise(resolve => {
    _confirmResolve = resolve;
    document.getElementById('confirmModalMsg').textContent = message;
    document.getElementById('confirmModalOk').textContent = okLabel;
    document.getElementById('confirmModal').style.display = 'flex';
  });
}

function _confirmOk() {
  document.getElementById('confirmModal').style.display = 'none';
  if (_confirmResolve) { _confirmResolve(true); _confirmResolve = null; }
}

function _confirmCancel() {
  document.getElementById('confirmModal').style.display = 'none';
  if (_confirmResolve) { _confirmResolve(false); _confirmResolve = null; }
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

// ── State ──────────────────────────────────────────────────────────────────
let _allVoices = [];       // raw from API
let _filteredVoices = [];  // after search + filters
let _selectedVoice = { voice_id: '', name: 'Andrew - Smooth, Smart and Clear', description: '', language: '', accent: '', gender: '', age: '', category: '', preview_url: '' };
let _favorites = new Set(JSON.parse(localStorage.getItem('voiceFavorites') || '[]'));
let _activeFilters = {};
let _trendingOrder = [];   // original API order = trending
let _previewAudio = null;  // HTMLAudio for voice preview
let _previewVoiceId = '';  // which voice is currently previewing

// ── Waveform player state ──────────────────────────────────────────────────
let _waveAudio = null;      // HTMLAudio element
let _waveCtx = null;        // AudioContext
let _waveAnalyser = null;
let _waveBuffer = null;     // decoded AudioBuffer
let _waveRafId = null;
let _waveMode = 'test';     // 'test' | 'approval'
let _approvalWaveAudio = null;
let _approvalWaveBuffer = null;
let _approvalRafId = null;

// ── Language code → full name map ──────────────────────────────────────────
const LANG_NAMES = {
  af:'Afrikaans', ar:'Arabic', bg:'Bulgarian', bn:'Bengali', ca:'Catalan',
  cs:'Czech', cy:'Welsh', da:'Danish', de:'German', el:'Greek',
  en:'English', es:'Spanish', et:'Estonian', fa:'Persian', fi:'Finnish',
  fil:'Filipino', fr:'French', gl:'Galician', gu:'Gujarati', he:'Hebrew',
  hi:'Hindi', hr:'Croatian', hu:'Hungarian', hy:'Armenian', id:'Indonesian',
  is:'Icelandic', it:'Italian', ja:'Japanese', ka:'Georgian', kn:'Kannada',
  ko:'Korean', lt:'Lithuanian', lv:'Latvian', mk:'Macedonian', ml:'Malayalam',
  mr:'Marathi', ms:'Malay', mt:'Maltese', nl:'Dutch', no:'Norwegian',
  pa:'Punjabi', pl:'Polish', pt:'Portuguese', ro:'Romanian', ru:'Russian',
  sk:'Slovak', sl:'Slovenian', sq:'Albanian', sr:'Serbian', sv:'Swedish',
  sw:'Swahili', ta:'Tamil', te:'Telugu', th:'Thai', tl:'Filipino',
  tr:'Turkish', uk:'Ukrainian', ur:'Urdu', vi:'Vietnamese',
  zh:'Chinese', 'zh-cn':'Chinese (Simplified)', 'zh-tw':'Chinese (Traditional)',
};

let _langOptions = []; // [{code, name}] populated from _allVoices

// ── Voice normalizer ───────────────────────────────────────────────────────
function normalizeVoice(v) {
  const labels = v.labels || {};
  return {
    voice_id:    v.voice_id || v.id || '',
    name:        v.name || '',
    description: v.description || labels.description || '',
    language:    v.language   || labels.language   || '',
    accent:      v.accent     || labels.accent      || '',
    gender:      (v.gender    || labels.gender      || '').toLowerCase(),
    age:         (v.age       || labels.age         || '').toLowerCase(),
    category:    (v.category  || labels.use_case    || '').toLowerCase().replace(/[\s&]+/g, '_'),
    preview_url: v.preview_url || v.preview || '',
    high_quality: Array.isArray(v.high_quality_base_model_ids) && v.high_quality_base_model_ids.length > 0,
    notice_period: v.notice_period || labels.notice_period || '',
    live_moderation: v.live_moderation != null ? !!v.live_moderation : true,
    _raw: v,
  };
}

// ── Init ───────────────────────────────────────────────────────────────────
async function initVoiceConfig() {
  updateVoiceCard();
  await loadVoicesFromServer();
}

async function loadVoicesFromServer() {
  try {
    const data = await apiFetch('/api/tts/voices', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tts_provider: 'genaipro', tts_api_key: '' }),
    });
    const raw = data.voices || [];
    _allVoices = raw.map(normalizeVoice);
    _trendingOrder = _allVoices.map((_, i) => i);

    if (!_selectedVoice.voice_id) {
      const andrew = _allVoices.find(v => v.name.toLowerCase().includes('andrew'));
      const def = andrew || _allVoices[0];
      if (def) _selectedVoice = def;
    }
    updateVoiceCard();
    _filteredVoices = [..._allVoices];
    populateFilterDropdowns();
  } catch (e) {
    console.warn('Could not load voices:', e.message);
  }
}

function populateFilterDropdowns() {
  const langs = [...new Set(_allVoices.map(v => v.language).filter(Boolean))].sort();
  _langOptions = langs.map(code => ({ code, name: LANG_NAMES[code] || code }));
  _langOptions.sort((a, b) => a.name.localeCompare(b.name));
  _updateAccentDropdown(''); // show all accents initially
}

function _renderLangDropdown(query) {
  const dropdown = document.getElementById('vfLangDropdown');
  if (!dropdown) return;
  const q = (query || '').toLowerCase().trim();
  const matches = q
    ? _langOptions.filter(l => l.name.toLowerCase().includes(q) || l.code.toLowerCase().includes(q))
    : _langOptions;
  if (!matches.length) { dropdown.style.display = 'none'; return; }
  dropdown.innerHTML = matches.map(l =>
    `<li data-code="${l.code}" onmousedown="_selectLangOption('${l.code}','${l.name.replace(/'/g,"\\'")}')">
       <span class="vf-lang-name">${l.name}</span>
       <span class="vf-lang-code">${l.code}</span>
     </li>`
  ).join('');
  dropdown.style.display = 'block';
}

function _hideLangDropdown() {
  const dropdown = document.getElementById('vfLangDropdown');
  if (dropdown) dropdown.style.display = 'none';
  // If text input doesn't match a selected lang, clear it
  const input = document.getElementById('vfLanguageInput');
  const hidden = document.getElementById('vfLanguage');
  if (input && hidden && !hidden.value) input.value = '';
}

function _selectLangOption(code, name) {
  const input = document.getElementById('vfLanguageInput');
  const hidden = document.getElementById('vfLanguage');
  if (input) input.value = name;
  if (hidden) hidden.value = code;
  _hideLangDropdown();
  _updateAccentDropdown(code);
}

function _clearLang() {
  const input = document.getElementById('vfLanguageInput');
  const hidden = document.getElementById('vfLanguage');
  if (input) input.value = '';
  if (hidden) hidden.value = '';
  _updateAccentDropdown('');
}

function _updateAccentDropdown(langCode) {
  const voices = langCode ? _allVoices.filter(v => v.language === langCode) : _allVoices;
  const accents = [...new Set(voices.map(v => v.accent).filter(Boolean))].sort();
  const sel = document.getElementById('vfAccent');
  if (!sel) return;
  sel.innerHTML = '<option value="">Select accent</option>';
  accents.forEach(a => {
    const o = document.createElement('option');
    o.value = a;
    o.textContent = a.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
    sel.appendChild(o);
  });
}

// ── Voice Card ─────────────────────────────────────────────────────────────
function updateVoiceCard() {
  const nameEl = document.getElementById('voiceCardName');
  const descEl = document.getElementById('voiceCardDesc');
  const favBtn = document.getElementById('voiceFavBtn');
  if (nameEl) nameEl.textContent = _selectedVoice.name || '—';
  if (descEl) descEl.textContent = _selectedVoice.description || (_selectedVoice.language ? `${_selectedVoice.language} · ${_selectedVoice.accent || ''}`.trim().replace(/·\s*$/, '') : 'Sin descripción');
  if (favBtn) favBtn.textContent = _favorites.has(_selectedVoice.voice_id) ? '♥' : '♡';
}

function toggleFavoriteSelected() {
  const vid = _selectedVoice.voice_id;
  if (!vid) return;
  if (_favorites.has(vid)) _favorites.delete(vid); else _favorites.add(vid);
  localStorage.setItem('voiceFavorites', JSON.stringify([..._favorites]));
  updateVoiceCard();
  // refresh modal list if open
  if (document.getElementById('voiceModal')?.style.display !== 'none') renderVoiceModalList(_filteredVoices);
}

// ── Voice Modal ────────────────────────────────────────────────────────────
function openVoiceModal() {
  const modal = document.getElementById('voiceModal');
  if (!modal) return;
  modal.style.display = '';
  const search = document.getElementById('vmSearch');
  if (search) search.value = '';
  _filteredVoices = [..._allVoices];
  renderVoiceModalList(_filteredVoices);
  if (search) search.focus();
}

function closeVoiceModal() {
  const modal = document.getElementById('voiceModal');
  if (modal) modal.style.display = 'none';
  stopPreview();
}

function renderVoiceModalList(voices) {
  const list = document.getElementById('vmVoiceList');
  if (!list) return;
  if (!voices.length) {
    list.innerHTML = '<div class="vm-empty">No se encontraron voces.</div>';
    return;
  }
  list.innerHTML = '';
  const frag = document.createDocumentFragment();
  voices.forEach(v => frag.appendChild(_buildVoiceRow(v)));
  list.appendChild(frag);
}

function _buildVoiceRow(v) {
  const isFav = _favorites.has(v.voice_id);
  const isSelected = v.voice_id === _selectedVoice.voice_id;
  const isPreviewing = v.voice_id === _previewVoiceId;
  const row = document.createElement('div');
  row.className = 'vm-row' + (isSelected ? ' vm-row--selected' : '') + (isPreviewing ? ' vm-row--previewing' : '');
  row.dataset.voiceId = v.voice_id;

  const chips = [v.language, v.accent].filter(Boolean).map(c => `<span class="vm-chip">${escHtml(c)}</span>`).join('');
  const catLabel = v.category ? `<span class="vm-chip vm-chip--cat">${escHtml(categoryLabel(v.category))}</span>` : '';

  row.innerHTML = `
    <div class="vm-row-main" onclick="previewVoiceRow(${escAttr(JSON.stringify(v))})">
      <div class="vm-row-name">${escHtml(v.name)}</div>
      <div class="vm-row-desc">${escHtml(v.description || '')}</div>
      <div class="vm-row-chips">${chips}${catLabel}</div>
    </div>
    <div class="vm-row-actions">
      <button class="vm-icon-btn vm-icon-btn--select${isSelected ? ' vm-icon-btn--selected' : ''}" title="Seleccionar voz" onclick="event.stopPropagation();selectVoice(${escAttr(JSON.stringify(v))})">↩</button>
      <button class="vm-icon-btn${isFav ? ' vm-icon-btn--fav' : ''}" title="Favorito" onclick="event.stopPropagation();toggleFavoriteModal('${escHtml(v.voice_id)}',this)">${isFav ? '♥' : '♡'}</button>
      <button class="vm-icon-btn" title="Copiar Voice ID" onclick="event.stopPropagation();copyVoiceId('${escHtml(v.voice_id)}',this)">📋</button>
    </div>
  `;
  return row;
}

function categoryLabel(cat) {
  const MAP = {
    narrative_story: 'Narrative & Story', conversational: 'Conversational',
    characters_animation: 'Characters & Animation', social_media: 'Social Media',
    entertainment_tv: 'Entertainment & TV', advertisement: 'Advertisement',
    informative_educational: 'Informative & Educational',
  };
  return MAP[cat] || cat;
}

function escAttr(str) { return str.replace(/"/g, '&quot;'); }

function selectVoice(voice) {
  _selectedVoice = typeof voice === 'string' ? JSON.parse(voice) : voice;
  updateVoiceCard();
  closeVoiceModal();
  showToast(`Voz seleccionada: ${_selectedVoice.name}`, 'success');
}

// ── Search & Sort ──────────────────────────────────────────────────────────
let _searchDebounce = null;
let _serverSearchVersion = 0;  // race-condition guard

function searchVoices(query) {
  const q = query.toLowerCase().trim();
  clearTimeout(_searchDebounce);

  if (!q) {
    _filteredVoices = [..._allVoices];
    renderVoiceModalList(_filteredVoices);
    return;
  }

  // Local filter first
  const local = _allVoices.filter(v =>
    v.name.toLowerCase().includes(q) ||
    v.voice_id.toLowerCase().includes(q) ||
    (v.description || '').toLowerCase().includes(q)
  );

  if (local.length > 0) {
    _filteredVoices = _applyFilterMap(local, _activeFilters);
    renderVoiceModalList(_filteredVoices);
    return;
  }

  // No local results — search server (debounced)
  if (q.length >= 3) {
    const list = document.getElementById('vmVoiceList');
    if (list) list.innerHTML = '<div class="vm-empty">Buscando en servidor...</div>';
    _searchDebounce = setTimeout(() => _searchVoicesServer(q), 500);
  } else {
    renderVoiceModalList([]);
  }
}

async function _searchVoicesServer(query) {
  const version = ++_serverSearchVersion;
  console.log('[VoiceSearch] Server search:', query);
  try {
    const data = await apiFetch('/api/tts/voices', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tts_provider: 'genaipro', tts_api_key: '', search: query }),
    });
    // Ignore stale responses
    if (version !== _serverSearchVersion) return;

    const raw = data.voices || [];
    console.log('[VoiceSearch] Server returned:', raw.length, 'voices');
    if (!raw.length) {
      const list = document.getElementById('vmVoiceList');
      if (list) list.innerHTML = '<div class="vm-empty">No se encontraron voces.</div>';
      return;
    }
    const newVoices = raw.map(normalizeVoice);
    // Merge into _allVoices so subsequent local searches find them
    for (const v of newVoices) {
      if (!_allVoices.find(e => e.voice_id === v.voice_id)) {
        _allVoices.push(v);
      }
    }
    _filteredVoices = newVoices;
    renderVoiceModalList(_filteredVoices);
  } catch (e) {
    console.warn('[VoiceSearch] Server search failed:', e.message);
    if (version !== _serverSearchVersion) return;
    const list = document.getElementById('vmVoiceList');
    if (list) list.innerHTML = '<div class="vm-empty">Error buscando voces.</div>';
  }
}

function sortTrending() {
  _filteredVoices = [..._filteredVoices].sort((a, b) => {
    const ia = _allVoices.findIndex(v => v.voice_id === a.voice_id);
    const ib = _allVoices.findIndex(v => v.voice_id === b.voice_id);
    return ia - ib;
  });
  renderVoiceModalList(_filteredVoices);
}

// ── Favorites ──────────────────────────────────────────────────────────────
function toggleFavoriteModal(voiceId, btn) {
  if (_favorites.has(voiceId)) { _favorites.delete(voiceId); btn.textContent = '♡'; btn.classList.remove('vm-icon-btn--fav'); }
  else { _favorites.add(voiceId); btn.textContent = '♥'; btn.classList.add('vm-icon-btn--fav'); }
  localStorage.setItem('voiceFavorites', JSON.stringify([..._favorites]));
  if (_selectedVoice.voice_id === voiceId) updateVoiceCard();
}

// ── Copy Voice ID ──────────────────────────────────────────────────────────
function copyVoiceId(voiceId, btn) {
  navigator.clipboard.writeText(voiceId).then(() => {
    const orig = btn.textContent;
    btn.textContent = '✓';
    setTimeout(() => { btn.textContent = orig; }, 1500);
  }).catch(() => showToast('No se pudo copiar', 'error'));
}

// ── Voice Preview ──────────────────────────────────────────────────────────

// Preview bar state
let _pbAudio = null;       // HTMLAudio for preview bar
let _pbVoice = null;       // voice object currently in bar
let _pbBuffer = null;      // decoded AudioBuffer (for waveform)
let _pbRafId = null;       // requestAnimationFrame id

function previewVoiceRow(voice) {
  const v = typeof voice === 'string' ? JSON.parse(voice) : voice;
  // Toggle: click same voice = pause/play
  if (_previewVoiceId === v.voice_id && _pbAudio) {
    if (_pbAudio.paused) { _pbAudio.play().catch(() => {}); }
    else { _pbAudio.pause(); }
    return;
  }
  stopPreview();
  if (!v.preview_url) {
    showToast('Esta voz no tiene muestra de audio disponible.', 'info');
    return;
  }
  _previewVoiceId = v.voice_id;
  _pbVoice = v;
  _pbAudio = new Audio(v.preview_url);
  _pbAudio.crossOrigin = 'anonymous';

  // Update row highlights
  document.querySelectorAll('.vm-row--previewing').forEach(r => r.classList.remove('vm-row--previewing'));
  const activeRow = document.querySelector(`.vm-row[data-voice-id="${CSS.escape(v.voice_id)}"]`);
  if (activeRow) activeRow.classList.add('vm-row--previewing');

  // Show bar immediately (before audio loads)
  showPreviewBar(v);

  _pbAudio.onplay  = () => { _updatePbBtn(true);  _startPbProgress(); };
  _pbAudio.onpause = () => { _updatePbBtn(false); cancelAnimationFrame(_pbRafId); };
  _pbAudio.onended = () => {
    _updatePbBtn(false);
    cancelAnimationFrame(_pbRafId);
    _previewVoiceId = '';
    document.querySelectorAll('.vm-row--previewing').forEach(r => r.classList.remove('vm-row--previewing'));
    _drawPbWave(0); // reset to full unplayed
  };

  _pbAudio.play().catch(e => showToast('No se pudo reproducir preview: ' + e.message, 'error'));

  // Async: fetch + decode for waveform (non-blocking)
  fetch(v.preview_url)
    .then(r => r.arrayBuffer())
    .then(buf => {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      return ctx.decodeAudioData(buf).then(ab => { ctx.close(); return ab; });
    })
    .then(ab => { _pbBuffer = ab; _drawPbWave(_pbAudio ? _pbAudio.currentTime / (_pbAudio.duration || 1) : 0); })
    .catch(() => {}); // waveform is optional, don't break on error
}

function showPreviewBar(voice) {
  const bar = document.getElementById('vmPreviewBar');
  if (!bar) return;
  bar.style.display = '';
  const nameEl = document.getElementById('vmPbName');
  const langEl = document.getElementById('vmPbLang');
  if (nameEl) nameEl.textContent = voice.name || '—';
  if (langEl) langEl.textContent = [voice.language, voice.accent].filter(Boolean).join(' · ');
  _pbBuffer = null; // reset until async decode finishes
  _drawPbWave(0);
}

function togglePreviewBarPlayback() {
  if (!_pbAudio) return;
  if (_pbAudio.paused) _pbAudio.play().catch(() => {}); else _pbAudio.pause();
}

function seekPreview(seconds) {
  if (!_pbAudio || !_pbAudio.duration) return;
  _pbAudio.currentTime = Math.max(0, Math.min(_pbAudio.duration, _pbAudio.currentTime + seconds));
}

function seekPreviewByClick(event, canvas) {
  if (!_pbAudio || !_pbAudio.duration) return;
  const pct = event.offsetX / canvas.offsetWidth;
  _pbAudio.currentTime = pct * _pbAudio.duration;
}

function _updatePbBtn(playing) {
  const btn = document.getElementById('vmPbPlayBtn');
  if (btn) btn.textContent = playing ? '⏸' : '▶';
}

function _startPbProgress() {
  cancelAnimationFrame(_pbRafId);
  const tick = () => {
    if (!_pbAudio || _pbAudio.paused) return;
    const prog = _pbAudio.duration ? _pbAudio.currentTime / _pbAudio.duration : 0;
    _drawPbWave(prog);
    const fmt = s => { const m = Math.floor(s/60); return `${m}:${String(Math.floor(s%60)).padStart(2,'0')}`; };
    const tl = document.getElementById('vmPbTimeLeft');
    const tr = document.getElementById('vmPbTimeRight');
    if (tl) tl.textContent = fmt(_pbAudio.currentTime);
    if (tr) tr.textContent = fmt(_pbAudio.duration || 0);
    _pbRafId = requestAnimationFrame(tick);
  };
  _pbRafId = requestAnimationFrame(tick);
}

function _drawPbWave(progress) {
  const canvas = document.getElementById('vmPbCanvas');
  if (!canvas) return;
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.offsetWidth || 800;
  const H = canvas.offsetHeight || 48;
  canvas.width  = W * dpr;
  canvas.height = H * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, W, H);

  const barW = 2;
  const gap  = 1.5;
  const cols = Math.floor(W / (barW + gap));
  const playedColor   = '#6c63ff';
  const unplayedColor = '#3a3a55';

  if (_pbBuffer) {
    // Real waveform from decoded audio
    const data = _pbBuffer.getChannelData(0);
    const samplesPerCol = Math.ceil(data.length / cols);
    for (let i = 0; i < cols; i++) {
      let max = 0;
      const start = i * samplesPerCol;
      for (let j = start; j < start + samplesPerCol && j < data.length; j++) {
        const vv = Math.abs(data[j]);
        if (vv > max) max = vv;
      }
      const barH = Math.max(3, max * H * 0.85);
      const x = i * (barW + gap);
      const y = (H - barH) / 2;
      ctx.fillStyle = (i / cols) < progress ? playedColor : unplayedColor;
      ctx.beginPath(); ctx.roundRect(x, y, barW, barH, 1); ctx.fill();
    }
  } else {
    // Placeholder: pseudo-random looking waveform
    const seed = (_pbVoice?.voice_id || 'x').split('').reduce((a,c) => a + c.charCodeAt(0), 0);
    for (let i = 0; i < cols; i++) {
      const pseudo = Math.abs(Math.sin((i + seed) * 0.4) * 0.5 + Math.sin((i + seed) * 1.1) * 0.3 + 0.2);
      const barH = Math.max(3, pseudo * H * 0.85);
      const x = i * (barW + gap);
      const y = (H - barH) / 2;
      ctx.fillStyle = (i / cols) < progress ? playedColor : unplayedColor;
      ctx.beginPath(); ctx.roundRect(x, y, barW, barH, 1); ctx.fill();
    }
  }
}

function stopPreview() {
  if (_pbAudio) { _pbAudio.pause(); _pbAudio = null; }
  cancelAnimationFrame(_pbRafId);
  _previewVoiceId = '';
  _pbVoice = null;
  _pbBuffer = null;
  document.querySelectorAll('.vm-row--previewing').forEach(r => r.classList.remove('vm-row--previewing'));
  const bar = document.getElementById('vmPreviewBar');
  if (bar) bar.style.display = 'none';
}

// ── Filter Modal ───────────────────────────────────────────────────────────
function openFilterModal() {
  const modal = document.getElementById('voiceFilterModal');
  if (modal) modal.style.display = '';
}

function closeFilterModal() {
  const modal = document.getElementById('voiceFilterModal');
  if (modal) modal.style.display = 'none';
}

function toggleFilterChip(btn) {
  btn.classList.toggle('vf-chip--active');
}

function selectToggle(btn) {
  const group = btn.dataset.group;
  document.querySelectorAll(`.vf-toggle[data-group="${group}"]`).forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
}

function applyFilters() {
  const filters = {};

  // Language (sent to server)
  const lang = document.getElementById('vfLanguage')?.value;
  if (lang) filters.language = lang;

  // Accent (client-side)
  const accent = document.getElementById('vfAccent')?.value;
  if (accent) filters.accent = accent;

  // Category chips (client-side)
  const cats = [...document.querySelectorAll('#vfCategoryChips .vf-chip--active')].map(b => b.dataset.val);
  if (cats.length) filters.categories = cats;

  // Toggle groups
  const toggleVal = (group) => document.querySelector(`.vf-toggle.active[data-group="${group}"]`)?.dataset.val || '';
  const quality = toggleVal('quality');
  if (quality) filters.quality = quality;
  const gender = toggleVal('gender'); // sent to server
  if (gender) filters.gender = gender;
  const age = toggleVal('age');
  if (age) filters.age = age;

  _activeFilters = filters;
  const query = document.getElementById('vmSearch')?.value || '';
  const base = query
    ? _allVoices.filter(v => v.name.toLowerCase().includes(query.toLowerCase()) || v.voice_id.toLowerCase().includes(query.toLowerCase()))
    : [..._allVoices];
  _filteredVoices = _applyFilterMap(base, _activeFilters);
  renderVoiceModalList(_filteredVoices);
  closeFilterModal();
}

function _applyFilterMap(voices, filters) {
  return voices.filter(v => {
    if (filters.language && v.language !== filters.language) return false;
    if (filters.accent && v.accent !== filters.accent) return false;
    if (filters.categories && filters.categories.length && !filters.categories.includes(v.category)) return false;
    if (filters.quality === 'high' && !v.high_quality) return false;
    if (filters.gender && v.gender !== filters.gender) return false;
    if (filters.age && v.age !== filters.age) return false;
    return true;
  });
}

function resetFilters() {
  _activeFilters = {};
  document.querySelectorAll('.vf-chip--active').forEach(b => b.classList.remove('vf-chip--active'));
  document.querySelectorAll('.vf-toggle').forEach(b => {
    b.classList.remove('active');
    if (b.dataset.val === '') b.classList.add('active');
  });
  _clearLang();
  const accentSel = document.getElementById('vfAccent');
  if (accentSel) accentSel.value = '';
  _filteredVoices = [..._allVoices];
  renderVoiceModalList(_filteredVoices);
  closeFilterModal();
}

// ── Voice config collection ────────────────────────────────────────────────
function _collectVoiceConfig() {
  const modelId  = document.getElementById('ttsModelId')?.value  || 'eleven_multilingual_v2';
  const speed    = parseFloat(document.getElementById('ttsSpeed')?.value    || '1.0');
  const stability = parseFloat(document.getElementById('ttsStability')?.value || '0.5');
  const similarity = parseFloat(document.getElementById('ttsSimilarity')?.value || '0.75');
  const style    = parseFloat(document.getElementById('ttsStyle')?.value    || '0.0');

  const extraConfig = { model_id: modelId, speed, stability, similarity, style };
  if (_selectedVoice.name) extraConfig['voice_name'] = _selectedVoice.name;

  return {
    tts_provider:  'genaipro',
    tts_api_key:   '',
    tts_voice_id:  _selectedVoice.voice_id || null,
    tts_config:    JSON.stringify(extraConfig),
  };
}

async function testVoice() {
  if (!currentProjectId) return;
  const cfg = _collectVoiceConfig();
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
    const label = document.getElementById('wavePlayerLabel');
    if (label) label.textContent = '🔊 Vista previa (200 chars)';
    await renderWaveformPlayer(blob, 'test');
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
    stopPolling();
    await refreshDetail(currentProjectId);
    pollInterval = setInterval(() => refreshDetail(currentProjectId), 4000);
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
    if (btn) { btn.disabled = false; btn.textContent = origText; }
  }
}

// ── Waveform Player ────────────────────────────────────────────────────────

async function renderWaveformPlayer(blob, mode) {
  _waveMode = mode || 'test';
  const canvasId  = mode === 'approval' ? 'approvalWaveCanvas' : 'waveCanvas';
  const playBtnId = mode === 'approval' ? 'approvalWavePlayBtn' : 'wavePlayBtn';
  const timeId    = mode === 'approval' ? 'approvalWaveTime' : 'waveTime';
  const playerId  = mode === 'approval' ? 'approvalWavePlayer' : 'waveformPlayer';

  const player = document.getElementById(playerId);
  if (player) player.style.display = '';

  // Decode audio
  const arrayBuf = await blob.arrayBuffer();
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const audioBuf = await ctx.decodeAudioData(arrayBuf);

  if (mode === 'approval') {
    _approvalWaveBuffer = audioBuf;
    if (_approvalWaveAudio) { _approvalWaveAudio.pause(); URL.revokeObjectURL(_approvalWaveAudio.src); }
    _approvalWaveAudio = new Audio(URL.createObjectURL(blob));
  } else {
    _waveBuffer = audioBuf;
    if (_waveAudio) { _waveAudio.pause(); URL.revokeObjectURL(_waveAudio.src); }
    _waveAudio = new Audio(URL.createObjectURL(blob));
  }

  await ctx.close();

  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const buf = mode === 'approval' ? _approvalWaveBuffer : _waveBuffer;
  drawWaveformStatic(canvas, buf, 0);

  const audio = mode === 'approval' ? _approvalWaveAudio : _waveAudio;
  const playBtn = document.getElementById(playBtnId);
  const timeEl  = document.getElementById(timeId);

  function fmt(s) { const m = Math.floor(s / 60); return `${m}:${String(Math.floor(s % 60)).padStart(2, '0')}`; }

  audio.ontimeupdate = () => {
    const prog = audio.duration ? audio.currentTime / audio.duration : 0;
    drawWaveformStatic(canvas, buf, prog);
    if (timeEl) timeEl.textContent = `${fmt(audio.currentTime)} / ${fmt(audio.duration || 0)}`;
  };
  audio.onended = () => { if (playBtn) playBtn.textContent = '▶'; };
  audio.onplay  = () => { if (playBtn) playBtn.textContent = '⏸'; };
  audio.onpause = () => { if (playBtn) playBtn.textContent = '▶'; };
}

function drawWaveformStatic(canvas, audioBuf, progress) {
  if (!canvas || !audioBuf) return;
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.offsetWidth || 600;
  const H = canvas.offsetHeight || 64;
  canvas.width  = W * dpr;
  canvas.height = H * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, W, H);

  const data = audioBuf.getChannelData(0);
  const step = Math.ceil(data.length / W);
  const barW = 2;
  const gap  = 1;
  const cols = Math.floor(W / (barW + gap));
  const samplesPerCol = Math.ceil(data.length / cols);
  const playedColor = '#7c3aed';
  const unplayedColor = '#3f3f46';

  for (let i = 0; i < cols; i++) {
    let max = 0;
    const start = i * samplesPerCol;
    for (let j = start; j < start + samplesPerCol && j < data.length; j++) {
      const v = Math.abs(data[j]);
      if (v > max) max = v;
    }
    const barH = Math.max(2, max * H * 0.85);
    const x = i * (barW + gap);
    const y = (H - barH) / 2;
    ctx.fillStyle = (i / cols) < progress ? playedColor : unplayedColor;
    ctx.beginPath();
    ctx.roundRect(x, y, barW, barH, 1);
    ctx.fill();
  }
}

function toggleWavePlayback() {
  const audio = _waveAudio;
  if (!audio) return;
  if (audio.paused) audio.play().catch(() => {}); else audio.pause();
}

function toggleApprovalPlayback() {
  const audio = _approvalWaveAudio;
  if (!audio) return;
  if (audio.paused) audio.play().catch(() => {}); else audio.pause();
}

// Seek on canvas click
document.addEventListener('click', (e) => {
  const canvas = e.target;
  if (canvas.id === 'waveCanvas' && _waveAudio) {
    const pct = e.offsetX / canvas.offsetWidth;
    _waveAudio.currentTime = pct * _waveAudio.duration;
  }
  if (canvas.id === 'approvalWaveCanvas' && _approvalWaveAudio) {
    const pct = e.offsetX / canvas.offsetWidth;
    _approvalWaveAudio.currentTime = pct * _approvalWaveAudio.duration;
  }
});

// ── Audio Approval ────────────────────────────────────────────────────────

async function approveAudio() {
  if (!currentProjectId) return;
  if (!await showConfirm('¿Aprobar el voiceover? Después podrás continuar con la generación de escenas.', 'Aprobar')) return;

  const btn = document.querySelector('#voiceoverApprovalSection .btn-success');
  const origText = btn ? btn.textContent : '✅ Aprobar Audio';
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Aprobando…'; }

  try {
    await apiFetch(`/api/projects/${currentProjectId}/approve-audio`, { method: 'POST' });
    showToast('¡Audio aprobado! Haz clic en "Continuar con Escenas" para procesar el video.', 'success');
    await refreshDetail(currentProjectId);
  } catch (e) {
    showToast('Error al aprobar: ' + e.message, 'error');
    if (btn) { btn.disabled = false; btn.textContent = origText; }
  }
}

async function continueWithScenes() {
  if (!currentProjectId) return;
  if (!await showConfirm('¿Continuar con la generación de escenas? Se dividirá el SRT en escenas de 5 segundos y se iniciará la generación de video.', 'Continuar')) return;

  const btn = document.getElementById('voiceoverContinueActions')?.querySelector('button');
  const origText = btn ? btn.textContent : '▶️ Continuar con Escenas';
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Creando escenas…'; }

  try {
    await apiFetch(`/api/projects/${currentProjectId}/create-scenes-from-srt`, { method: 'POST' });
    showToast('Escenas creadas. Iniciando generación de video…', 'success');
    stopPolling();
    await refreshDetail(currentProjectId);
    pollInterval = setInterval(() => refreshDetail(currentProjectId), 4000);
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
    if (btn) { btn.disabled = false; btn.textContent = origText; }
  }
}

async function resetToAudioApproved() {
  if (!currentProjectId) return;
  if (!await showConfirm('¿Reintentar desde audio aprobado? Se limpiarán los chunks con error para que puedas continuar con las escenas.', 'Reintentar')) return;

  const btn = document.getElementById('voiceoverRetryActions')?.querySelector('button');
  const origText = btn ? btn.textContent : '🔄 Reintentar desde Audio Aprobado';
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Reseteando…'; }

  try {
    await apiFetch(`/api/projects/${currentProjectId}/reset-to-audio-approved`, { method: 'POST' });
    showToast('Proyecto reseteado. Ahora haz clic en "Continuar con Escenas".', 'success');
    await refreshDetail(currentProjectId);
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
    if (btn) { btn.disabled = false; btn.textContent = origText; }
  }
}

async function generateImages() {
  if (!currentProjectId) return;

  const btn = document.getElementById('generateImagesBtn');
  const origText = btn ? btn.textContent : '🎨 Generar Imágenes';
  if (btn) { btn.disabled = true; btn.textContent = `⏳ Iniciando ${_imgProviderName()}…`; }

  try {
    await apiFetch(`/api/projects/${currentProjectId}/generate-images`, { method: 'POST' });
    showToast(`🎨 ${_imgProviderName()} iniciado — generando imagen por escena. Revisa los logs.`, 'info');
    stopPolling();
    await refreshDetail(currentProjectId);
    pollInterval = setInterval(() => refreshDetail(currentProjectId), 3000);
  } catch (e) {
    showToast('Error al generar imágenes: ' + e.message, 'error');
    if (btn) { btn.disabled = false; btn.textContent = origText; }
  }
}

async function continueWithVideo() {
  showToast('Próximamente — integración con NCA para renderizado de video.', 'info');
}

async function startVeo3Animation() {
  if (!currentProjectId) return;
  const btn = document.getElementById('startVeo3AnimationBtn');
  const origText = btn ? btn.textContent : '🎬 Animar con WaveSpeed';
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Iniciando WaveSpeed…'; }

  try {
    await apiFetch(`/api/projects/${currentProjectId}/start-animation`, { method: 'POST' });
    showToast('🎬 Animación con WaveSpeed iniciada (máx. 2 simultáneas). Revisa los logs.', 'success');
    stopPolling();
    await refreshDetail(currentProjectId);
    pollInterval = setInterval(() => refreshDetail(currentProjectId), 5000);
  } catch (e) {
    showToast('Error al iniciar animación: ' + e.message, 'error');
    if (btn) { btn.disabled = false; btn.textContent = origText; }
  }
}

async function saveMotionPrompt(chunk_number) {
  if (!currentProjectId) return;
  const val = document.getElementById(`motion_prompt_${chunk_number}`).value;
  try {
    await apiFetch(`/api/projects/${currentProjectId}/chunk/${chunk_number}/motion-prompt`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ motion_prompt: val })
    });
    showToast(`Prompt de movimiento #${chunk_number} guardado`, 'success');
  } catch (e) {
    showToast('Error al guardar: ' + e.message, 'error');
  }
}

async function regenerateVoiceover() {
  if (!currentProjectId) return;
  if (!await showConfirm('¿Descartar el voiceover actual y volver a configurar la voz?', 'Descartar')) return;

  try {
    await apiFetch(`/api/projects/${currentProjectId}/regenerate-voiceover`, { method: 'POST' });
    showToast('Voiceover descartado. Configura la voz y genera de nuevo.', 'info');
    await refreshDetail(currentProjectId);
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
  }
}

// ── Init ──────────────────────────────────────────────────────────────────
_fetchSettings();
loadDashboard();
