/**
 * CanvasPlayer — Real-time video player with transitions + voiceover.
 *
 * Plays an ordered list of chunks on a <canvas>, rendering xfade-style
 * transitions between clips entirely in the browser (no FFmpeg needed).
 * Syncs voiceover audio to the playback position.
 */
class CanvasPlayer {
  constructor(canvas, projectId, chunks) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.projectId = projectId;
    this.chunks = chunks;

    this.mediaA = null;
    this.mediaB = null;
    this.currentIdx = 0;
    this.state = 'idle';
    this.rafId = null;
    this._chunkStartTime = 0;
    this._currentDurMs = 0;
    this._pausedElapsed = 0;

    // Voiceover audio element
    this.audio = null;
    this._audioLoaded = false;

    // Callbacks
    this.onStateChange = null;
    this.onTimeUpdate = null;
    this.onChunkChange = null;

    // Precompute cumulative offsets (ms) for each chunk
    this._offsets = [];
    let acc = 0;
    for (const c of chunks) {
      this._offsets.push(acc);
      acc += this._durOf(c);
    }
    this.totalDurMs = acc;

    // Start loading voiceover
    this._loadAudio();
  }

  // ── Public API ────────────────────────────────────────────────────────

  play(fromIdx = 0) {
    this.stop();
    this.currentIdx = fromIdx;
    this.state = 'playing';
    this._pausedElapsed = 0;
    this._showCanvas();
    this._fireStateChange();
    this._startChunk(fromIdx, 0);
  }

  pause() {
    if (this.state !== 'playing') return;
    this.state = 'paused';
    this._pausedElapsed = performance.now() - this._chunkStartTime;
    if (this.rafId) { cancelAnimationFrame(this.rafId); this.rafId = null; }
    if (this.mediaA && this.mediaA.tagName === 'VIDEO') this.mediaA.pause();
    if (this.mediaB && this.mediaB.tagName === 'VIDEO') this.mediaB.pause();
    if (this.audio) this.audio.pause();
    this._fireStateChange();
  }

  resume() {
    if (this.state !== 'paused') return;
    this.state = 'playing';
    this._chunkStartTime = performance.now() - this._pausedElapsed;
    if (this.mediaA && this.mediaA.tagName === 'VIDEO') this.mediaA.play().catch(() => {});
    if (this.mediaB && this.mediaB.tagName === 'VIDEO') this.mediaB.play().catch(() => {});
    if (this.audio) this.audio.play().catch(() => {});
    this._fireStateChange();
    this._renderFrame();
  }

  togglePlayPause() {
    if (this.state === 'playing') this.pause();
    else if (this.state === 'paused') this.resume();
    else this.play(0);
  }

  stop() {
    this.state = 'idle';
    if (this.rafId) { cancelAnimationFrame(this.rafId); this.rafId = null; }
    this._destroyMedia(this.mediaA);
    this._destroyMedia(this.mediaB);
    this.mediaA = null;
    this.mediaB = null;
    if (this.audio) { this.audio.pause(); }
    this._fireStateChange();
  }

  seekToTransition(chunkNumber) {
    const arrIdx = this.chunks.findIndex(c => c.chunk_number === chunkNumber);
    if (arrIdx <= 0) return;
    this.stop();
    this.currentIdx = arrIdx - 1;
    this.state = 'playing';
    const prevDur = this._chunkDurMs(arrIdx - 1);
    const seekMs = Math.max(0, prevDur - 1500);
    this._showCanvas();
    this._fireStateChange();
    this._startChunk(arrIdx - 1, seekMs);
  }

  seekTo(ms) {
    const targetMs = Math.max(0, Math.min(ms, this.totalDurMs));
    // Find which chunk contains this time
    let idx = 0;
    for (let i = this._offsets.length - 1; i >= 0; i--) {
      if (targetMs >= this._offsets[i]) { idx = i; break; }
    }
    this.stop();
    this.currentIdx = idx;
    this.state = 'playing';
    const offsetInChunk = targetMs - this._offsets[idx];
    this._showCanvas();
    this._fireStateChange();
    this._startChunk(idx, offsetInChunk);
  }

  updateChunks(chunks) {
    this.chunks = chunks;
    this._offsets = [];
    let acc = 0;
    for (const c of chunks) {
      this._offsets.push(acc);
      acc += this._durOf(c);
    }
    this.totalDurMs = acc;
  }

  // ── Audio ─────────────────────────────────────────────────────────────

  _loadAudio() {
    this.audio = new Audio();
    this.audio.preload = 'auto';
    this.audio.src = `/api/projects/${this.projectId}/voiceover/audio`;
    this.audio.addEventListener('canplaythrough', () => { this._audioLoaded = true; }, { once: true });
    this.audio.load();
  }

  _syncAudio(globalMs) {
    if (!this.audio || !this._audioLoaded) return;
    const targetSec = globalMs / 1000;
    // Only seek if drift > 300ms
    if (Math.abs(this.audio.currentTime - targetSec) > 0.3) {
      this.audio.currentTime = targetSec;
    }
    if (this.state === 'playing' && this.audio.paused) {
      this.audio.play().catch(() => {});
    }
  }

  // ── Internal ──────────────────────────────────────────────────────────

  _durOf(c) {
    return (c.start_ms != null && c.end_ms != null) ? (c.end_ms - c.start_ms) : 3800;
  }

  _showCanvas() {
    this.canvas.style.display = '';
    const parent = this.canvas.parentElement;
    if (parent) {
      this.canvas.width = parent.clientWidth || 960;
      this.canvas.height = parent.clientHeight || 540;
    }
    const placeholder = document.getElementById('editingPreviewPlaceholder');
    if (placeholder) placeholder.style.display = 'none';
    const videoEl = document.getElementById('editingPreviewPlayer');
    if (videoEl) { videoEl.pause(); videoEl.style.display = 'none'; }
  }

  _chunkDurMs(idx) {
    const c = this.chunks[idx];
    return c ? this._durOf(c) : 0;
  }

  _chunkMediaUrl(chunk) {
    const cb = chunk.updated_at ? `?t=${new Date(chunk.updated_at).getTime()}` : `?t=${Date.now()}`;
    if (chunk.video_path) return `/api/projects/${this.projectId}/chunk/${chunk.chunk_number}/video${cb}`;
    if (chunk.image_path) return `/api/projects/${this.projectId}/chunk/${chunk.chunk_number}/image${cb}`;
    return null;
  }

  _isVideo(chunk) { return !!chunk.video_path; }

  async _loadMedia(chunk) {
    const url = this._chunkMediaUrl(chunk);
    if (!url) {
      const c = document.createElement('canvas');
      c.width = 16; c.height = 9;
      const x = c.getContext('2d');
      x.fillStyle = '#000'; x.fillRect(0, 0, 16, 9);
      const img = new Image();
      img.src = c.toDataURL();
      await img.decode().catch(() => {});
      img._isStatic = true;
      return img;
    }

    if (this._isVideo(chunk)) {
      const vid = document.createElement('video');
      vid.crossOrigin = 'anonymous';
      vid.muted = true;  // muted because audio comes from voiceover
      vid.playsInline = true;
      vid.preload = 'auto';
      vid.src = url;
      vid._isStatic = false;
      await new Promise((resolve) => {
        vid.oncanplaythrough = resolve;
        vid.onerror = resolve;
        setTimeout(resolve, 8000);
      });
      return vid;
    } else {
      const img = new Image();
      img.crossOrigin = 'anonymous';
      img.src = url;
      img._isStatic = true;
      await img.decode().catch(() => {});
      return img;
    }
  }

  _destroyMedia(el) {
    if (!el) return;
    if (el.tagName === 'VIDEO') {
      el.pause();
      el.removeAttribute('src');
      el.load();
    }
  }

  async _startChunk(idx, seekMs = 0) {
    if (idx >= this.chunks.length) {
      // Finished all clips
      if (this.audio) this.audio.pause();
      this.state = 'idle';
      this._fireStateChange();
      return;
    }

    this.currentIdx = idx;
    this._currentDurMs = this._chunkDurMs(idx);
    if (this.onChunkChange) this.onChunkChange(idx);

    // Load current if needed
    if (!this.mediaA) {
      this.mediaA = await this._loadMedia(this.chunks[idx]);
    }

    // Preload next
    if (idx + 1 < this.chunks.length) {
      this._loadMedia(this.chunks[idx + 1]).then(m => { this.mediaB = m; });
    } else {
      this.mediaB = null;
    }

    // Start video playback
    if (this.mediaA && this.mediaA.tagName === 'VIDEO') {
      this.mediaA.currentTime = seekMs / 1000;
      this.mediaA.play().catch(() => {});
    }

    // Sync voiceover audio
    const globalMs = (this._offsets[idx] || 0) + seekMs;
    this._syncAudio(globalMs);

    this._chunkStartTime = performance.now() - seekMs;
    this._pausedElapsed = 0;

    // Start render loop
    if (this.rafId) cancelAnimationFrame(this.rafId);
    this._renderFrame();
  }

  _renderFrame() {
    if (this.state !== 'playing') return;

    const now = performance.now();
    const elapsed = now - this._chunkStartTime;
    const chunkDur = this._currentDurMs;
    const nextChunk = (this.currentIdx + 1 < this.chunks.length)
      ? this.chunks[this.currentIdx + 1] : null;
    const trType = nextChunk?.transition || null;
    const trDur = trType ? (nextChunk.transition_duration || 500) : 0;

    const ctx = this.ctx;
    const w = this.canvas.width;
    const h = this.canvas.height;

    if (elapsed >= chunkDur) {
      // Chunk finished → advance
      this._advanceToNext();
      return;
    }

    if (trType && this.mediaB && elapsed >= chunkDur - trDur) {
      // In transition zone
      const trProgress = Math.min((elapsed - (chunkDur - trDur)) / trDur, 1.0);

      // Start next video if not started
      if (this.mediaB.tagName === 'VIDEO' && this.mediaB.paused) {
        this.mediaB.currentTime = 0;
        this.mediaB.play().catch(() => {});
      }

      this._drawTransition(ctx, w, h, trProgress, trType, this.mediaA, this.mediaB);
    } else {
      // Normal frame
      this._drawMedia(ctx, this.mediaA, 0, 0, w, h);
    }

    // Time update callback
    if (this.onTimeUpdate) {
      const globalMs = (this._offsets[this.currentIdx] || 0) + Math.min(elapsed, chunkDur);
      this.onTimeUpdate(globalMs, this.totalDurMs);
    }

    this.rafId = requestAnimationFrame(() => this._renderFrame());
  }

  _advanceToNext() {
    const nextIdx = this.currentIdx + 1;
    // Swap: B becomes A
    this._destroyMedia(this.mediaA);
    this.mediaA = this.mediaB;
    this.mediaB = null;
    this._startChunk(nextIdx, 0);
  }

  _drawMedia(ctx, media, x, y, w, h) {
    if (!media) { ctx.fillStyle = '#000'; ctx.fillRect(x, y, w, h); return; }
    try { ctx.drawImage(media, x, y, w, h); } catch (e) {
      ctx.fillStyle = '#000'; ctx.fillRect(x, y, w, h);
    }
  }

  // ── Transition rendering ──────────────────────────────────────────────

  _drawTransition(ctx, w, h, progress, type, mediaA, mediaB) {
    const p = Math.max(0, Math.min(1, progress));
    switch (type) {
      case 'fade': case 'dissolve': this._trFade(ctx, w, h, p, mediaA, mediaB); break;
      case 'fadeblack': this._trFadeColor(ctx, w, h, p, mediaA, mediaB, '#000'); break;
      case 'fadewhite': this._trFadeColor(ctx, w, h, p, mediaA, mediaB, '#fff'); break;
      case 'wipeleft':  this._trWipe(ctx, w, h, p, mediaA, mediaB, 'left'); break;
      case 'wiperight': this._trWipe(ctx, w, h, p, mediaA, mediaB, 'right'); break;
      case 'wipeup':    this._trWipe(ctx, w, h, p, mediaA, mediaB, 'up'); break;
      case 'wipedown':  this._trWipe(ctx, w, h, p, mediaA, mediaB, 'down'); break;
      case 'slideleft':  this._trSlide(ctx, w, h, p, mediaA, mediaB, 'left'); break;
      case 'slideright': this._trSlide(ctx, w, h, p, mediaA, mediaB, 'right'); break;
      case 'slideup':    this._trSlide(ctx, w, h, p, mediaA, mediaB, 'up'); break;
      case 'slidedown':  this._trSlide(ctx, w, h, p, mediaA, mediaB, 'down'); break;
      case 'circleopen':  this._trCircle(ctx, w, h, p, mediaA, mediaB, true); break;
      case 'circleclose': this._trCircle(ctx, w, h, p, mediaA, mediaB, false); break;
      case 'radial':      this._trRadial(ctx, w, h, p, mediaA, mediaB); break;
      case 'smoothleft':  this._trSmooth(ctx, w, h, p, mediaA, mediaB, 'left'); break;
      case 'smoothright': this._trSmooth(ctx, w, h, p, mediaA, mediaB, 'right'); break;
      case 'zoomin':      this._trZoom(ctx, w, h, p, mediaA, mediaB); break;
      default: this._trFade(ctx, w, h, p, mediaA, mediaB);
    }
  }

  // ── Transition implementations ────────────────────────────────────────

  _trFade(ctx, w, h, p, a, b) {
    ctx.globalAlpha = 1;
    this._drawMedia(ctx, a, 0, 0, w, h);
    ctx.globalAlpha = p;
    this._drawMedia(ctx, b, 0, 0, w, h);
    ctx.globalAlpha = 1;
  }

  _trFadeColor(ctx, w, h, p, a, b, color) {
    if (p < 0.5) {
      const sub = p * 2;
      ctx.globalAlpha = 1;
      this._drawMedia(ctx, a, 0, 0, w, h);
      ctx.globalAlpha = sub;
      ctx.fillStyle = color;
      ctx.fillRect(0, 0, w, h);
      ctx.globalAlpha = 1;
    } else {
      const sub = (p - 0.5) * 2;
      ctx.fillStyle = color;
      ctx.fillRect(0, 0, w, h);
      ctx.globalAlpha = sub;
      this._drawMedia(ctx, b, 0, 0, w, h);
      ctx.globalAlpha = 1;
    }
  }

  _trWipe(ctx, w, h, p, a, b, dir) {
    ctx.globalAlpha = 1;
    this._drawMedia(ctx, a, 0, 0, w, h);
    ctx.save();
    ctx.beginPath();
    switch (dir) {
      case 'left':  ctx.rect(w * (1 - p), 0, w * p, h); break;
      case 'right': ctx.rect(0, 0, w * p, h); break;
      case 'up':    ctx.rect(0, h * (1 - p), w, h * p); break;
      case 'down':  ctx.rect(0, 0, w, h * p); break;
    }
    ctx.clip();
    this._drawMedia(ctx, b, 0, 0, w, h);
    ctx.restore();
  }

  _trSlide(ctx, w, h, p, a, b, dir) {
    ctx.globalAlpha = 1;
    let ax = 0, ay = 0, bx = 0, by = 0;
    switch (dir) {
      case 'left':  ax = -w * p; bx = w * (1 - p); break;
      case 'right': ax = w * p;  bx = -w * (1 - p); break;
      case 'up':    ay = -h * p; by = h * (1 - p); break;
      case 'down':  ay = h * p;  by = -h * (1 - p); break;
    }
    this._drawMedia(ctx, a, ax, ay, w, h);
    this._drawMedia(ctx, b, bx, by, w, h);
  }

  _trCircle(ctx, w, h, p, a, b, opening) {
    const maxR = Math.sqrt(w * w + h * h) / 2;
    const r = opening ? (p * maxR) : (maxR * (1 - p));
    ctx.globalAlpha = 1;
    if (opening) {
      this._drawMedia(ctx, a, 0, 0, w, h);
      ctx.save();
      ctx.beginPath();
      ctx.arc(w / 2, h / 2, Math.max(r, 1), 0, Math.PI * 2);
      ctx.clip();
      this._drawMedia(ctx, b, 0, 0, w, h);
      ctx.restore();
    } else {
      this._drawMedia(ctx, b, 0, 0, w, h);
      ctx.save();
      ctx.beginPath();
      ctx.arc(w / 2, h / 2, Math.max(r, 1), 0, Math.PI * 2);
      ctx.clip();
      this._drawMedia(ctx, a, 0, 0, w, h);
      ctx.restore();
    }
  }

  _trRadial(ctx, w, h, p, a, b) {
    const cx = w / 2, cy = h / 2;
    const maxR = Math.sqrt(w * w + h * h);
    const angle = p * Math.PI * 2 - Math.PI / 2;
    ctx.globalAlpha = 1;
    this._drawMedia(ctx, a, 0, 0, w, h);
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.arc(cx, cy, maxR, -Math.PI / 2, angle, false);
    ctx.closePath();
    ctx.clip();
    this._drawMedia(ctx, b, 0, 0, w, h);
    ctx.restore();
  }

  _trSmooth(ctx, w, h, p, a, b, dir) {
    ctx.globalAlpha = 1;
    this._drawMedia(ctx, a, 0, 0, w, h);
    const tmp = document.createElement('canvas');
    tmp.width = w; tmp.height = h;
    const tc = tmp.getContext('2d');
    this._drawMedia(tc, b, 0, 0, w, h);
    tc.globalCompositeOperation = 'destination-in';
    const feather = w * 0.15;
    let grad;
    if (dir === 'left') {
      const edge = w * (1 - p);
      grad = tc.createLinearGradient(edge + feather, 0, edge - feather, 0);
    } else {
      const edge = w * p;
      grad = tc.createLinearGradient(edge - feather, 0, edge + feather, 0);
    }
    grad.addColorStop(0, 'rgba(0,0,0,0)');
    grad.addColorStop(1, 'rgba(0,0,0,1)');
    tc.fillStyle = grad;
    tc.fillRect(0, 0, w, h);
    ctx.drawImage(tmp, 0, 0);
  }

  _trZoom(ctx, w, h, p, a, b) {
    ctx.globalAlpha = 1;
    this._drawMedia(ctx, a, 0, 0, w, h);
    const scale = p;
    const sw = w * scale, sh = h * scale;
    const sx = (w - sw) / 2, sy = (h - sh) / 2;
    ctx.globalAlpha = p;
    this._drawMedia(ctx, b, sx, sy, sw, sh);
    ctx.globalAlpha = 1;
  }

  _fireStateChange() {
    if (this.onStateChange) this.onStateChange(this.state);
  }
}
