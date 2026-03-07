"""
Pipeline orchestrator.

Modes:
  - animated: Claude → TTS → ImagePrompt → Google Imagen 4 Fast → Animation → NCA
  - stock:    Claude → TTS → Keywords → Pexels/Pixabay → NCA

Chunk processing runs in a thread pool. Progress is persisted to SQLite
so the frontend can poll for updates.
"""
from __future__ import annotations

import asyncio
import os
import re
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime
from typing import List

from sqlalchemy.orm import Session

from ..config import settings, PROJECTS_PATH
from ..database import SessionLocal
from ..models import Project, Chunk, Worker, ProjectStatus, ChunkStatus, VideoMode

from .claude_service import (
    generate_script_full,
    clean_script,
    generate_image_prompt,
    generate_search_keywords,
    divide_script_into_scenes,
)
from .openai_service import generate_tts
from . import pexels_service, pixabay_service, nca_service, google_service, wavespeed_service
from .image import generate_image as _dispatch_generate_image
from .video import motion_service, pollinations_video_service

MAX_WORKERS = settings.max_workers


# ── DB setting helper ─────────────────────────────────────────────────────────

def _get_db_setting(db, key: str) -> str:
    """Fetch a value from the AppSetting table. Returns empty string if not found."""
    from ..models import AppSetting
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    return (row.value or "") if row else ""


def _get_pollinations_api_key(db) -> str:
    """Return the Pollinations API key (DB setting → .env). Empty string is OK (free tier)."""
    return _get_db_setting(db, "pollinations_api_key") or settings.pollinations_api_key or ""


def _get_wavespeed_api_key(db) -> str:
    """Return the WaveSpeed API key (DB setting → .env)."""
    return _get_db_setting(db, "wavespeed_api_key") or settings.wavespeed_api_key or ""


def _get_image_provider(db) -> str:
    """Return the image provider name (DB setting → .env → default 'wavespeed')."""
    return _get_db_setting(db, "image_provider") or settings.image_provider or "wavespeed"


def _get_reference_character(db, project) -> str | None:
    """Return the character reference image path, or None."""
    ref = getattr(project, "reference_character_path", None) or ""
    if ref and Path(ref).exists():
        return ref
    return None


def _get_reference_style(db, project) -> str | None:
    """Return the style reference image path, or None."""
    ref = getattr(project, "reference_style_path", None) or ""
    if ref and Path(ref).exists():
        return ref
    return None


# ── Logging helper ────────────────────────────────────────────────────────────

def _log(db: Session, project_id: int, message: str, stage: str = "", level: str = "info"):
    from ..models import Log
    import sys as _sys
    try:
        _sys.stdout.buffer.write(f"[{level.upper()}][{stage}] {message}\n".encode("utf-8", errors="replace"))
        _sys.stdout.buffer.flush()
    except Exception:
        pass
    try:
        # Only log if project still exists (guards against delete-while-running)
        if not db.query(Project).filter(Project.id == project_id).first():
            return
        entry = Log(
            project_id=project_id,
            level=level,
            stage=stage,
            message=message,
            timestamp=datetime.utcnow(),
        )
        db.add(entry)
        db.commit()
    except Exception:
        db.rollback()


class _ProjectGoneError(RuntimeError):
    """Raised when the project is deleted mid-pipeline."""


def _update_project(db: Session, project: Project, **kwargs):
    from sqlalchemy.orm.exc import StaleDataError
    for k, v in kwargs.items():
        setattr(project, k, v)
    project.updated_at = datetime.utcnow()
    try:
        db.commit()
        db.refresh(project)
    except StaleDataError:
        db.rollback()
        raise _ProjectGoneError("Project was deleted while pipeline was running")
    except Exception:
        db.rollback()
        raise


def _update_chunk(db: Session, chunk: Chunk, **kwargs):
    for k, v in kwargs.items():
        setattr(chunk, k, v)
    chunk.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(chunk)


# ── Project directory helpers ─────────────────────────────────────────────────

def project_dir(slug: str) -> Path:
    return PROJECTS_PATH / slug


def voiceover_dir(slug: str) -> Path:
    return project_dir(slug) / "voiceover"


def chunk_dir(slug: str, n: int) -> Path:
    return project_dir(slug) / f"chunk_{n}"


def rendered_dir(slug: str) -> Path:
    return project_dir(slug) / "rendered-chunks"


def final_dir(slug: str) -> Path:
    return project_dir(slug) / "final"


# ── Entry points ──────────────────────────────────────────────────────────────

def start_pipeline(project_id: int):
    """Phase 1: outline → script → pause at awaiting_approval."""
    t = threading.Thread(target=_run_pipeline_phase1, args=(project_id,), daemon=True)
    t.start()


def start_pipeline_phase2(project_id: int):
    """Phase 2: split script_final → chunks → audio/video → concat."""
    t = threading.Thread(target=_run_pipeline_phase2, args=(project_id,), daemon=True)
    t.start()


def start_regenerate_script(project_id: int):
    """Re-generate the script from the existing outline, then pause again."""
    t = threading.Thread(target=_regenerate_script_thread, args=(project_id,), daemon=True)
    t.start()


# ── Phase 1: outline + script ─────────────────────────────────────────────────

def _run_pipeline_phase1(project_id: int):
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        _update_project(db, project, status=ProjectStatus.processing)
        _log(db, project_id, f"Pipeline started for '{project.title}'", stage="init")

        # ── 1. Generate full script (outline is generated internally) ──────
        _log(db, project_id, "Generating full script with Claude…", stage="script")
        import json as _json
        transcripts = []
        if project.reference_transcripts:
            try:
                transcripts = _json.loads(project.reference_transcripts)
            except Exception:
                transcripts = []
                
        script_text = generate_script_full(
            title=project.title,
            transcripts=transcripts or None,
            video_type=project.video_type or "top10",
            duration=project.duration or "6-8"
        )

        script_text = clean_script(script_text)
        _update_project(db, project, script=script_text)
        _log(db, project_id, "Script generated. Awaiting manual approval.", stage="script")

        # ── 2. Pause — wait for user approval ─────────────────────────────
        _update_project(db, project, status=ProjectStatus.awaiting_approval)
        _log(db, project_id, "Status set to awaiting_approval. Review and approve the script.", stage="approval")

    except _ProjectGoneError:
        print(f"[INFO][pipeline] Project {project_id} was deleted mid-run, aborting.")
    except Exception as exc:
        db.rollback()
        db.expire_all()
        project = db.query(Project).filter(Project.id == project_id).first()
        if project:
            try:
                _update_project(db, project, status=ProjectStatus.error, error_message=str(exc))
            except Exception:
                pass
        _log(db, project_id, f"Pipeline phase1 error: {exc}\n{traceback.format_exc()}", stage="error", level="error")
    finally:
        db.close()


# ── Script regeneration ───────────────────────────────────────────────────────

def _regenerate_script_thread(project_id: int):
    """Re-run script generation from the saved outline; set awaiting_approval again."""
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        # ── Regenerate full script ──
        _log(db, project_id, "Regenerating full script with Claude…", stage="script")
        
        import json as _json
        transcripts = []
        if project.reference_transcripts:
            try:
                transcripts = _json.loads(project.reference_transcripts)
            except Exception:
                transcripts = []

        script_text = generate_script_full(
            title=project.title,
            transcripts=transcripts or None,
            video_type=project.video_type or "top10",
            duration=project.duration or "6-8"
        )

        script_text = clean_script(script_text)
        _update_project(db, project, script=script_text, script_approved=False, script_final=None)
        _log(db, project_id, "Script regenerated. Awaiting manual approval.", stage="script")
        
        _update_project(db, project, status=ProjectStatus.awaiting_approval)
        _log(db, project_id, "Status set to awaiting_approval.", stage="approval")

    except _ProjectGoneError:
        print(f"[INFO][pipeline] Project {project_id} was deleted mid-run, aborting.")
    except Exception as exc:
        db.rollback()
        db.expire_all()
        project = db.query(Project).filter(Project.id == project_id).first()
        if project:
            try:
                _update_project(db, project, status=ProjectStatus.error, error_message=str(exc))
            except Exception:
                pass
        _log(db, project_id, f"Regenerate script error: {exc}\n{traceback.format_exc()}", stage="error", level="error")
    finally:
        db.close()


# ── Phase 2: split script into chunks (no TTS/video yet) ──────────────────────

def _run_pipeline_phase2(project_id: int):
    """Validate approved script and prepare for TTS.

    In the new system the script is clean narration (no [N] markers).
    Chunks are NOT created here — they're created after TTS + SRT + Claude scene division.
    """
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        _update_project(db, project, status=ProjectStatus.processing)
        _log(db, project_id, "Procesando script aprobado...", stage="chunks")

        script_text = project.script_final or project.script
        if not script_text:
            raise RuntimeError("No hay script disponible.")

        # Clean the script (remove any leftover formatting/markers)
        script_text = clean_script(script_text)
        project.script_final = script_text

        word_count = len(script_text.split())
        _log(db, project_id,
             f"Script listo: {word_count} palabras. Listo para generar voiceover.",
             stage="chunks")

        # Delete any existing chunks from previous attempts
        db.query(Chunk).filter(Chunk.project_id == project_id).delete()
        db.commit()

        _update_project(db, project, status=ProjectStatus.awaiting_voice_config)
        _log(db, project_id,
             "Script procesado — configurar voz para continuar.",
             stage="done")

    except _ProjectGoneError:
        print(f"[INFO][pipeline] Project {project_id} was deleted mid-run, aborting.")
    except Exception as exc:
        db.rollback()
        db.expire_all()
        project = db.query(Project).filter(Project.id == project_id).first()
        if project:
            try:
                _update_project(db, project, status=ProjectStatus.error, error_message=str(exc))
            except Exception:
                pass
        _log(db, project_id, f"Pipeline phase2 error: {exc}\n{traceback.format_exc()}", stage="error", level="error")
    finally:
        db.close()


def _make_synthetic_srt(text: str, audio_path: Path) -> str:
    """Generate a minimal 1-block SRT covering the full audio duration.
    Duration is estimated from file size (no external API).
    """
    try:
        size_bytes = audio_path.stat().st_size
        # Rough estimate: MP3 at ~64 kbps for speech
        duration_secs = max(size_bytes * 8 / 64_000, 1.0)
    except Exception:
        # Fallback: ~2.5 words per second for spoken Spanish/English
        duration_secs = max(len(text.split()) / 2.5, 1.0)

    def _fmt(s: float) -> str:
        h = int(s // 3600)
        m = int((s % 3600) // 60)
        sec = int(s % 60)
        ms = int((s % 1) * 1000)
        return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"

    return f"1\n00:00:00,000 --> {_fmt(duration_secs)}\n{text.strip()}\n"


def _make_script_srt(text: str, audio_path: Path, words_per_block: int = 10) -> str:
    """Create a multi-segment SRT from script text + exact audio duration.

    Groups the script into ~words_per_block-word subtitle blocks and distributes
    them proportionally across the audio duration (uses mutagen for exact length).
    No external API required — text is the script that was spoken.
    """
    duration = _mp3_duration(audio_path) if audio_path.exists() else 0.0
    if duration <= 0:
        duration = max(len(text.split()) / 2.5, 1.0)

    words = text.split()
    if not words:
        return ""

    # Group into subtitle blocks of ~words_per_block words
    blocks: list[str] = []
    for i in range(0, len(words), words_per_block):
        blocks.append(" ".join(words[i:i + words_per_block]))

    n = len(blocks)
    lines: list[str] = []
    for idx, block in enumerate(blocks):
        start = duration * idx / n
        end   = duration * (idx + 1) / n
        lines.append(str(idx + 1))
        lines.append(f"{_fmt_srt_time(start)} --> {_fmt_srt_time(end)}")
        lines.append(block)
        lines.append("")

    return "\n".join(lines)


def _resolve_srt(
    db,
    project_id: int,
    chunk,
    n: int,
    audio_path: Path,
    vo_dir: Path,
) -> Path:
    """Return an SRT path for a chunk. Never calls external APIs.

    Priority:
    1. chunk.srt_path already in DB and file exists
    2. Per-chunk SRT on disk: vo_dir/audio-chunk-N.srt
    3. Global SRT from TTS provider: vo_dir/subtitles.srt
    4. Synthetic SRT generated from the chunk text
    """
    # 1. Already resolved in DB
    if chunk.srt_path and Path(chunk.srt_path).exists():
        _log(db, project_id, f"[Chunk {n}] Usando SRT existente (DB).", stage=f"chunk_{n}_srt")
        return Path(chunk.srt_path)

    # 2. Per-chunk SRT file on disk (TTS provider saves alongside the MP3)
    per_chunk_srt = vo_dir / f"audio-chunk-{n}.srt"
    if per_chunk_srt.exists():
        _log(db, project_id, f"[Chunk {n}] Usando SRT por chunk de TTS provider.", stage=f"chunk_{n}_srt")
        _update_chunk(db, chunk, srt_path=str(per_chunk_srt))
        return per_chunk_srt

    # 3. Global subtitles.srt from TTS provider
    global_srt = vo_dir / "subtitles.srt"
    if global_srt.exists():
        _log(db, project_id, f"[Chunk {n}] Usando subtitles.srt global.", stage=f"chunk_{n}_srt")
        _update_chunk(db, chunk, srt_path=str(global_srt))
        return global_srt

    # 4. Generate synthetic SRT from chunk text — no external API needed
    srt_path = audio_path.with_suffix(".srt")
    _log(db, project_id, f"[Chunk {n}] Generando SRT sintético desde texto.", stage=f"chunk_{n}_srt")
    srt_content = _make_synthetic_srt(chunk.scene_text or "", audio_path)
    srt_path.write_text(srt_content, encoding="utf-8")
    _update_chunk(db, chunk, srt_path=str(srt_path))
    return srt_path


def start_pipeline_phase3(project_id: int):
    """Phase 3: generate images/videos and render all chunks (audio already exists)."""
    t = threading.Thread(target=_run_pipeline_phase3, args=(project_id,), daemon=True)
    t.start()


# ── SRT-based scene creation ──────────────────────────────────────────────────

def _parse_srt_entries(srt_path: Path) -> list:
    """Parse SRT file, return list of (start_secs, end_secs, text). No external API."""
    entries = []
    try:
        content = srt_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return entries
    blocks = re.split(r"\n\s*\n", content.strip())
    ts_pattern = re.compile(
        r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})"
    )
    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 2:
            continue
        for i, line in enumerate(lines):
            m = ts_pattern.match(line.strip())
            if m:
                h1, m1, s1, ms1, h2, m2, s2, ms2 = [int(x) for x in m.groups()]
                start = h1 * 3600 + m1 * 60 + s1 + ms1 / 1000
                end = h2 * 3600 + m2 * 60 + s2 + ms2 / 1000
                text = " ".join(lines[i + 1:]).strip()
                if text:
                    entries.append((start, end, text))
                break
    return entries


def _find_srt_for_project(slug: str) -> tuple:
    """Locate the best available SRT file for the project.

    Priority:
    1. voiceover/subtitles.srt
    2. Any voiceover/audio-chunk-N.srt  (concatenated into a single entry list)
    3. None  (caller must generate synthetic entries)

    Returns (srt_path_or_None, entries_list).
    """
    vo = voiceover_dir(slug)

    # 1. Global SRT
    global_srt = vo / "subtitles.srt"
    if global_srt.exists():
        entries = _parse_srt_entries(global_srt)
        if entries:
            return global_srt, entries

    # 2. Per-chunk SRTs — concatenate them in order
    import glob as _glob
    chunk_srts = sorted(
        _glob.glob(str(vo / "audio-chunk-*.srt")),
        key=lambda p: int(re.search(r"audio-chunk-(\d+)\.srt", p).group(1))
        if re.search(r"audio-chunk-(\d+)\.srt", p) else 0,
    )
    if chunk_srts:
        all_entries: list = []
        offset = 0.0
        for srt_file in chunk_srts:
            chunk_entries = _parse_srt_entries(Path(srt_file))
            for start, end, text in chunk_entries:
                all_entries.append((start + offset, end + offset, text))
            if chunk_entries:
                offset = max(end for _, end, _ in chunk_entries) + offset
        if all_entries:
            return Path(chunk_srts[0]), all_entries

    return None, []


def _synthetic_entries_from_audio(slug: str, db, project_id: int) -> tuple:
    """Return (duration_secs, []) using mutagen for exact MP3 duration.

    The caller will distribute existing chunk texts across num_scenes
    when entries is empty (use_srt=False path).
    """
    vo = voiceover_dir(slug)
    audio = vo / "audio-completo.mp3"
    if audio.exists():
        duration = _mp3_duration(audio)
    else:
        # Last resort: estimate from chunk word count (~2.5 words/sec)
        chunks = db.query(Chunk).filter(Chunk.project_id == project_id).all()
        words = sum(len((c.scene_text or "").split()) for c in chunks)
        duration = max(words / 2.5, 5.0)

    return max(duration, 1.0), []


def _run_create_scenes_from_srt(project_id: int) -> None:
    """Use Claude + SRT to divide script into scenes with accurate timestamps,
    then slice audio-completo.mp3 into per-scene segments.
    """
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        slug = project.slug
        vo = voiceover_dir(slug)

        # ── Get the script text (clean narration)
        script_text = (project.script_final or project.script or "").strip()
        if not script_text:
            raise RuntimeError("No hay script disponible para dividir en escenas.")

        _log(db, project_id,
             f"Script cargado ({len(script_text.split())} palabras). Buscando SRT...",
             stage="srt_scenes")

        # ── Find and read the SRT file
        srt_file, srt_entries = _find_srt_for_project(slug)
        if not srt_entries:
            raise RuntimeError(
                "No se encontro archivo SRT. El proveedor TTS debe generar subtitulos."
            )

        srt_content = Path(srt_file).read_text(encoding="utf-8", errors="replace")
        total_duration = max(end for _, end, _ in srt_entries)
        _log(db, project_id,
             f"SRT encontrado: {Path(srt_file).name} ({len(srt_entries)} entradas, {total_duration:.1f}s).",
             stage="srt_scenes")

        # ── Call Claude to divide script into scenes using real SRT timestamps
        _log(db, project_id,
             "Enviando script + SRT a Claude para division de escenas...",
             stage="srt_scenes")

        scenes = divide_script_into_scenes(script_text, srt_content)

        _log(db, project_id,
             f"Claude dividio el script en {len(scenes)} escenas.",
             stage="srt_scenes")

        for s in scenes:
            dur = s["endMs"] - s["startMs"]
            _log(db, project_id,
                 f"[Escena {s['id']}] {s['startMs']}ms - {s['endMs']}ms ({dur / 1000:.1f}s)",
                 stage="srt_scenes")

        # ── Create Chunk records from Claude's JSON
        db.query(Chunk).filter(Chunk.project_id == project_id).delete()
        db.flush()
        db.expire_all()

        for s in scenes:
            db.add(Chunk(
                project_id=project_id,
                chunk_number=s["id"],
                status=ChunkStatus.pending,
                scene_text=s["texto"],
                start_ms=s["startMs"],
                end_ms=s["endMs"],
            ))
        db.commit()

        chunks = (
            db.query(Chunk)
            .filter(Chunk.project_id == project_id)
            .order_by(Chunk.chunk_number)
            .all()
        )

        # ── Slice audio-completo.mp3 into per-scene segments
        audio_complete = vo / "audio-completo.mp3"
        if audio_complete.exists():
            import shutil as _shutil
            _log(db, project_id,
                 f"Dividiendo audio en {len(chunks)} segmentos...",
                 stage="srt_scenes")
            for chunk in chunks:
                n = chunk.chunk_number
                start_sec = chunk.start_ms / 1000.0
                duration_sec = max((chunk.end_ms - chunk.start_ms) / 1000.0, 0.1)
                scene_audio = vo / f"audio-chunk-{n}.mp3"
                try:
                    _slice_mp3(audio_complete, scene_audio, start_sec, duration_sec)
                    _log(db, project_id,
                         f"[Escena {n}] Audio cortado ({start_sec:.1f}s - {start_sec + duration_sec:.1f}s).",
                         stage="srt_scenes")
                except Exception as exc:
                    _log(db, project_id,
                         f"[Escena {n}] ffmpeg fallo, copiando audio completo: {exc}",
                         stage="srt_scenes", level="warning")
                    _shutil.copy2(str(audio_complete), str(scene_audio))
                _update_chunk(db, chunk, audio_path=str(scene_audio))
        else:
            _log(db, project_id,
                 "AVISO: audio-completo.mp3 no encontrado.",
                 stage="srt_scenes", level="warning")

        _update_project(db, project, status=ProjectStatus.scenes_ready)
        _log(db, project_id,
             f"{len(chunks)} escenas creadas y listas.",
             stage="srt_scenes")

    except Exception as exc:
        db.rollback()
        try:
            project = db.query(Project).filter(Project.id == project_id).first()
            if project:
                _update_project(db, project, status=ProjectStatus.error, error_message=str(exc))
            _log(db, project_id,
                 f"Error creando escenas: {exc}\n{traceback.format_exc()}",
                 stage="srt_scenes", level="error")
        except Exception:
            pass
    finally:
        db.close()


def start_create_scenes_from_srt(project_id: int) -> None:
    """Align scene chunks to SRT and slice audio. Runs in background thread."""
    t = threading.Thread(target=_run_create_scenes_from_srt, args=(project_id,), daemon=True)
    t.start()


# ── Media generation (Pollinations — image + video per scene) ─────────────────

def _generate_media_for_chunk(
    project_id: int,
    chunk_id: int,
    slug: str,
    reference_character: str | None,
    api_key: str,
) -> None:
    """Generate image for one scene chunk using Pollinations.

    Steps
    -----
    1. Use pre-generated Gemini image prompt, or fall back to Claude.
    2. Call Pollinations image API → save image_N.jpg.
    3. Get or generate a motion prompt (motion_service / fallback).
       Video animation is handled separately in Phase 4.
    """
    db = SessionLocal()
    try:
        chunk = db.query(Chunk).filter(Chunk.id == chunk_id).first()
        if not chunk:
            return

        n         = chunk.chunk_number
        narration = chunk.scene_text or ""
        c_dir     = chunk_dir(slug, n)

        # ── Step 1: image prompt ──────────────────────────────────────────────
        img_prompt = (chunk.image_prompt or "").strip()

        if img_prompt:
            _log(db, project_id, f"[Pollinations {n}] ✓ Prompt pre-generado listo.", stage=f"media_{n}")
        else:
            _log(db, project_id, f"[Pollinations {n}] Generando prompt con Claude…", stage=f"media_{n}")
            generated = None
            for _attempt in range(3):
                try:
                    generated = generate_image_prompt(narration, "", reference_character or "")
                    break
                except Exception as _exc:
                    _exc_str = str(_exc)
                    if "529" in _exc_str or "overloaded" in _exc_str.lower():
                        import time as _t; _t.sleep(5 * (2 ** _attempt))
                    else:
                        raise
            img_prompt = (generated or "").strip()
            if not img_prompt:
                # Last-resort fallback: use the narration text itself
                img_prompt = narration.strip()[:800]
                _log(db, project_id,
                     f"[Pollinations {n}] ⚠️ Claude no generó prompt — usando narración como fallback.",
                     stage=f"media_{n}", level="warning")
            if not img_prompt:
                raise RuntimeError(f"Escena {n} no tiene texto — no se puede generar imagen.")
            _update_chunk(db, chunk, image_prompt=img_prompt)

        print(f"DEBUG [imagen_{n}] Prompt: {img_prompt[:150]}")

        # ── Step 2: image generation ─────────────────────────────────────────
        img_provider = _get_image_provider(db)
        _log(db, project_id, f"[imagen_{n}] Generando con {img_provider.capitalize()}…", stage=f"media_{n}_img")
        img_path = c_dir / "images" / f"image_{n}.jpg"
        img_path.parent.mkdir(parents=True, exist_ok=True)

        poll_key = _get_pollinations_api_key(db)
        ws_key = _get_wavespeed_api_key(db)
        project_obj = db.query(Project).filter(Project.id == project_id).first()
        ref_char = _get_reference_character(db, project_obj) if project_obj else None
        ref_style = _get_reference_style(db, project_obj) if project_obj else None
        _dispatch_generate_image(
            img_prompt, img_path,
            provider=img_provider, api_key=poll_key, wavespeed_api_key=ws_key,
            reference_character_path=ref_char, reference_style_path=ref_style,
        )
        _update_chunk(db, chunk, image_path=str(img_path))
        _log(db, project_id, f"[imagen_{n}] ✅ Guardada: image_{n}.jpg ({img_path.stat().st_size // 1024} KB)", stage=f"media_{n}_img_done")

        # ── Step 3: motion prompt ─────────────────────────────────────────────
        if chunk.motion_prompt:
            motion = chunk.motion_prompt
        else:
            try:
                motion = motion_service.generate_motion_prompt(narration, img_prompt)
                _update_chunk(db, chunk, motion_prompt=motion)
            except Exception as mp_exc:
                motion = "Slow cinematic zoom in, subtle camera movement"
                _log(db, project_id,
                     f"[Pollinations {n}] ⚠️ Motion prompt falló ({mp_exc}), usando fallback.",
                     stage=f"media_{n}", level="warning")
                _update_chunk(db, chunk, motion_prompt=motion)
        
        # We stop here for the image phase.
        # Phase 4 (Pollinations grok-video) handles video animation separately.
        _update_chunk(db, chunk, status=ChunkStatus.done)

    except Exception as exc:
        db.rollback()
        db.expire_all()
        chunk = db.query(Chunk).filter(Chunk.id == chunk_id).first()
        if chunk:
            _update_chunk(db, chunk, status=ChunkStatus.error, error_message=str(exc))
        _log(db, project_id, f"[Pollinations chunk {chunk_id}] Error: {exc}", stage="media_error", level="error")
        raise
    finally:
        db.close()


def _run_generate_images(project_id: int) -> None:
    """Generate image + motion prompt for every scene chunk using Pollinations."""
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        img_provider = _get_image_provider(db)
        poll_key = _get_pollinations_api_key(db)
        _log(db, project_id, f"🔑 {img_provider.capitalize()} configurado.", stage="media")

        _update_project(db, project, status=ProjectStatus.generating_images)
        _log(db, project_id, f"🎨 Iniciando generación de imágenes con {img_provider.capitalize()}…", stage="media")

        chunks = (
            db.query(Chunk)
            .filter(Chunk.project_id == project_id, Chunk.status != ChunkStatus.done)
            .order_by(Chunk.chunk_number)
            .all()
        )

        if not chunks:
            _log(db, project_id, "No hay escenas pendientes.", stage="media")
            _update_project(db, project, status=ProjectStatus.images_ready)
            return

        total = len(chunks)
        _log(db, project_id, f"📋 {total} escenas a procesar (imagen + video por escena).", stage="media")

        # ── STEP 1: Batch-generate image prompts via Gemini (one API call) ─────
        chunks_needing_prompt = [c for c in chunks if not c.image_prompt]
        if chunks_needing_prompt:
            try:
                _log(db, project_id,
                     f"🤖 Pre-generando {len(chunks_needing_prompt)} prompts visuales con Gemini…",
                     stage="media")
                scenes_data = [
                    {"scene_number": c.chunk_number, "narration": c.scene_text or "", "visual_description": ""}
                    for c in chunks_needing_prompt
                ]
                prompt_map = google_service.batch_generate_image_prompts(
                    scenes_data,
                    reference_character=project.reference_character or "",
                )
                for c in chunks_needing_prompt:
                    if c.chunk_number in prompt_map:
                        _update_chunk(db, c, image_prompt=prompt_map[c.chunk_number])
                db.commit()
                db.expire_all()
                chunks = (
                    db.query(Chunk)
                    .filter(Chunk.project_id == project_id, Chunk.status != ChunkStatus.done)
                    .order_by(Chunk.chunk_number)
                    .all()
                )
                _log(db, project_id,
                     f"✅ {len(prompt_map)} prompts generados. Iniciando {img_provider.capitalize()}…",
                     stage="media")
            except Exception as exc:
                _log(db, project_id,
                     f"⚠️ Batch Gemini falló ({exc}). Prompts se generarán por escena.",
                     stage="media", level="warning")

        # ── STEP 2: Image generation — parallel (max 5 concurrent) ──────────
        _log(db, project_id, f"⚡ Generando {total} imágenes en paralelo (max 5)…", stage="media")

        # Gather chunk metadata before spawning threads (DB objects aren't thread-safe)
        chunk_args = [
            (project_id, chunk.id, project.slug, project.reference_character, poll_key)
            for chunk in chunks
        ]

        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {
                pool.submit(
                    _generate_media_for_chunk, *args
                ): args[1]  # chunk.id
                for args in chunk_args
            }
            for future in as_completed(futures):
                chunk_id = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    errors.append(f"Chunk {chunk_id}: {exc}")

        # Refresh to get updated chunk statuses
        db.expire_all()
        done_count = (
            db.query(Chunk)
            .filter(Chunk.project_id == project_id, Chunk.status == ChunkStatus.done)
            .count()
        )
        _log(db, project_id,
             f"Imagen 4 Fast: {done_count}/{total} imágenes generadas.",
             stage="media_progress")

        if errors:
            _update_project(
                db, project,
                status=ProjectStatus.images_ready,
                error_message=f"Errores en {len(errors)} escena(s): {'; '.join(errors[:3])}",
            )
            _log(db, project_id,
                 f"Generación completada con {len(errors)} error(es).",
                 stage="media_done", level="error")
        else:
            _update_project(db, project, status=ProjectStatus.images_ready)
            _log(db, project_id,
                 f"✅ {total} escenas procesadas con Google Imagen 4 Fast.",
                 stage="media_done")

    except Exception as exc:
        db.rollback()
        try:
            project = db.query(Project).filter(Project.id == project_id).first()
            if project:
                _update_project(db, project, status=ProjectStatus.error, error_message=str(exc))
            _log(db, project_id,
                 f"Error en generación masiva: {exc}\n{traceback.format_exc()}",
                 stage="media_error", level="error")
        except Exception:
            pass
    finally:
        db.close()


def start_generate_images(project_id: int) -> None:
    """Launch Pollinations image generation in a background daemon thread."""
    t = threading.Thread(target=_run_generate_images, args=(project_id,), daemon=True)
    t.start()


def _run_pipeline_phase3(project_id: int):
    """Generate images/videos and NCA-render every chunk. TTS audio is already done."""
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        _update_project(db, project, status=ProjectStatus.processing)
        _log(db, project_id, "Iniciando generación de video para los chunks…", stage="phase3")

        chunks = (
            db.query(Chunk)
            .filter(Chunk.project_id == project_id)
            .order_by(Chunk.chunk_number)
            .all()
        )

        if not chunks:
            raise RuntimeError("No hay chunks disponibles para procesar.")

        # ── 1. Batch generate video prompts (animation instructions) if needed ──
        chunks_needing_video_prompt = [c for c in chunks if not c.video_prompt and project.mode == VideoMode.animated]
        if chunks_needing_video_prompt:
            try:
                _log(db, project_id,
                     f"🎬 Generando instrucciones de animación para {len(chunks_needing_video_prompt)} escenas con Gemini 1.5 Flash…",
                     stage="phase3")
                scenes_data = [
                    {
                        "scene_number": c.chunk_number,
                        "narration": c.scene_text or "",
                        "image_prompt": c.image_prompt or "",
                    }
                    for c in chunks_needing_video_prompt
                ]
                vp_map = google_service.batch_generate_video_prompts(scenes_data)
                
                for c in chunks_needing_video_prompt:
                    if c.chunk_number in vp_map:
                        _update_chunk(db, c, video_prompt=vp_map[c.chunk_number])
                db.commit()
                db.expire_all()
                chunks = (
                    db.query(Chunk)
                    .filter(Chunk.project_id == project_id)
                    .order_by(Chunk.chunk_number)
                    .all()
                )
                _log(db, project_id, f"✅ Instrucciones generadas. Iniciando renderizado de video…", stage="phase3")
            except Exception as exc:
                _log(db, project_id, f"⚠️ Error generando prompts de video: {exc}", stage="phase3", level="warning")


        api_key = project.tts_api_key or ""
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                pool.submit(
                    _process_chunk_video,
                    project_id,
                    chunk.id,
                    project.slug,
                    project.mode,
                    project.reference_character,
                    api_key,
                ): chunk.id
                for chunk in chunks
            }
            for future in as_completed(futures):
                chunk_id = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    errors.append(f"Chunk {chunk_id}: {exc}")

        if errors:
            _update_project(
                db, project,
                status=ProjectStatus.error,
                error_message=f"Errores de video: {'; '.join(errors)}",
            )
            _log(db, project_id, f"Fase 3 completada con errores: {'; '.join(errors)}", stage="phase3_done", level="error")
        else:
            _update_project(db, project, status=ProjectStatus.done)
            _log(db, project_id, "¡Todos los chunks procesados! Video listo.", stage="phase3_done")

    except _ProjectGoneError:
        print(f"[INFO][phase3] Project {project_id} was deleted mid-run, aborting.")
    except Exception as exc:
        db.rollback()
        db.expire_all()
        project = db.query(Project).filter(Project.id == project_id).first()
        if project:
            try:
                _update_project(db, project, status=ProjectStatus.error, error_message=str(exc))
            except Exception:
                pass
        _log(db, project_id, f"Phase 3 error: {exc}\n{traceback.format_exc()}", stage="error", level="error")
    finally:
        db.close()


def _process_chunk_video(
    project_id: int,
    chunk_id: int,
    slug: str,
    mode: VideoMode,
    reference_character: str | None,
    api_key: str = "",
):
    """Process one chunk for video only (TTS audio already exists from voiceover phase)."""
    db = SessionLocal()
    try:
        chunk = db.query(Chunk).filter(Chunk.id == chunk_id).first()
        _update_chunk(db, chunk, status=ChunkStatus.processing)
        n = chunk.chunk_number
        narration = chunk.scene_text or ""
        visual_desc = chunk.image_prompt or ""

        _log(db, project_id, f"[Chunk {n}] Iniciando generación de video…", stage=f"chunk_{n}")

        # Resolve audio path
        vo_dir = voiceover_dir(slug)
        c_dir  = chunk_dir(slug, n)
        r_dir  = rendered_dir(slug)
        f_dir  = final_dir(slug)
        for d in (c_dir / "images", c_dir / "videos", r_dir, f_dir):
            d.mkdir(parents=True, exist_ok=True)

        audio_path = Path(chunk.audio_path) if chunk.audio_path else vo_dir / f"audio-chunk-{n}.mp3"

        # SRT: use existing SRT (TTS provider) or generate synthetic — never calls Whisper
        srt_path = _resolve_srt(db, project_id, chunk, n, audio_path, vo_dir)

        if mode == VideoMode.animated:
            video_path = _animated_branch(db, project_id, chunk, n, slug, narration, visual_desc, reference_character, c_dir, api_key)
        else:
            video_path = _stock_branch(db, project_id, chunk, n, slug, narration, visual_desc, c_dir)

        # NCA render
        _log(db, project_id, f"[Chunk {n}] Renderizando con NCA…", stage=f"chunk_{n}_render")
        rendered_filename = f"chunk_{n}.mp4"
        rendered_url = nca_service.render_chunk(
            video_url_or_path=str(video_path),
            audio_url_or_path=str(audio_path),
            srt_url_or_path=str(srt_path),
            output_filename=rendered_filename,
        )
        rendered_local = r_dir / rendered_filename
        nca_service.download_from_nca(rendered_url, rendered_local)
        _update_chunk(db, chunk, rendered_path=str(rendered_local), status=ChunkStatus.done)
        _log(db, project_id, f"[Chunk {n}] Done.", stage=f"chunk_{n}_done")

    except Exception as exc:
        db.rollback()
        db.expire_all()
        chunk = db.query(Chunk).filter(Chunk.id == chunk_id).first()
        if chunk:
            _update_chunk(db, chunk, status=ChunkStatus.error, error_message=str(exc))
        _log(db, project_id, f"[Chunk {chunk_id}] Error en fase de video: {exc}", stage="chunk_error", level="error")
        raise
    finally:
        db.close()


def _mp3_duration(path: Path) -> float:
    """Return exact duration of an MP3 file using mutagen. Falls back to size estimate."""
    try:
        from mutagen.mp3 import MP3
        return MP3(str(path)).info.length
    except Exception:
        try:
            return max(path.stat().st_size * 8 / 64_000, 0.0)
        except Exception:
            return 0.0


def _slice_mp3(src: Path, dst: Path, start: float, duration: float) -> None:
    """Cut a [start, start+duration] segment from an MP3 using ffmpeg.

    Uses stream copy (no re-encode) for speed. Raises RuntimeError on failure.
    """
    import subprocess
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", str(src),
            "-ss", f"{start:.3f}",
            "-t",  f"{duration:.3f}",
            "-acodec", "copy",
            str(dst),
        ],
        capture_output=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg returned {result.returncode}: "
            f"{result.stderr.decode(errors='replace')[:300]}"
        )


def _fmt_srt_time(seconds: float) -> str:
    """Convert seconds → SRT timestamp HH:MM:SS,mmm."""
    total_ms = int(seconds * 1000)
    ms  = total_ms % 1000
    s   = (total_ms // 1000) % 60
    m   = (total_ms // 60_000) % 60
    h   = total_ms // 3_600_000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _merge_chunk_srts(db, project_id: int, chunks, vo_dir: Path) -> None:
    """Merge per-chunk SRTs into a single voiceover/subtitles.srt.

    Timestamps in each chunk SRT are shifted by the cumulative duration
    of all preceding chunks so that the global SRT aligns with
    audio-completo.mp3. Uses mutagen for exact durations.
    """
    merged_lines: list[str] = []
    entry_index = 1
    time_offset = 0.0

    for chunk in chunks:
        mp3_path = vo_dir / f"audio-chunk-{chunk.chunk_number}.mp3"
        srt_path = mp3_path.with_suffix(".srt")

        if srt_path.exists():
            raw_entries = _parse_srt_entries(srt_path)
            for start, end, text in raw_entries:
                merged_lines.append(str(entry_index))
                merged_lines.append(
                    f"{_fmt_srt_time(start + time_offset)} --> {_fmt_srt_time(end + time_offset)}"
                )
                merged_lines.append(text)
                merged_lines.append("")
                entry_index += 1

        # Advance offset by exact chunk audio duration
        if mp3_path.exists():
            time_offset += _mp3_duration(mp3_path)

    if merged_lines:
        subtitles_path = vo_dir / "subtitles.srt"
        subtitles_path.write_text("\n".join(merged_lines), encoding="utf-8")
        _log(db, project_id,
             f"subtitles.srt generado ({entry_index - 1} entradas, {time_offset:.1f}s total).",
             stage="tts_done")
    else:
        _log(db, project_id,
             "No se encontraron SRTs de chunks — subtitles.srt no generado.",
             stage="tts_done", level="warning")


def start_generate_voiceover(project_id: int):
    """Launch TTS generation for all chunks in a daemon thread."""
    t = threading.Thread(target=_run_generate_voiceover, args=(project_id,), daemon=True)
    t.start()


def _run_generate_voiceover(project_id: int):
    """Generate TTS audio for every chunk using the project's saved voice config."""
    import json as _json
    from .tts import get_provider

    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        _update_project(db, project, status=ProjectStatus.processing)
        _log(db, project_id, "Iniciando generacion de voiceover con TTS...", stage="tts")

        if not project.tts_provider or not project.tts_api_key:
            raise RuntimeError("Proveedor TTS o API key no configurados.")

        tts_config = _json.loads(project.tts_config or "{}")
        if project.tts_voice_id:
            tts_config["voice_id"] = project.tts_voice_id

        try:
            provider = get_provider(project.tts_provider, project.tts_api_key, tts_config)
        except ValueError as exc:
            raise RuntimeError(str(exc))

        # Use clean text (no [N] markers) for TTS — single call
        clean_text = project.script_final or project.script
        if not clean_text:
            raise RuntimeError("No hay script disponible para generar audio.")

        vo_dir = voiceover_dir(project.slug)
        vo_dir.mkdir(parents=True, exist_ok=True)

        complete_path = vo_dir / "audio-completo.mp3"
        _log(db, project_id, f"Generando audio TTS (texto completo: {len(clean_text)} chars)...", stage="tts")

        provider.generate(clean_text, complete_path)

        size_kb = complete_path.stat().st_size // 1024
        _log(db, project_id, f"Audio completo generado: {size_kb} KB", stage="tts_done")

        # SRT: GenAIPro downloads it alongside the MP3
        srt_from_tts = complete_path.with_suffix(".srt")
        subtitles_path = vo_dir / "subtitles.srt"
        if srt_from_tts.exists():
            import shutil as _shutil
            if str(srt_from_tts) != str(subtitles_path):
                _shutil.copy2(str(srt_from_tts), str(subtitles_path))
            entries = _parse_srt_entries(subtitles_path)
            _log(db, project_id,
                 f"subtitles.srt descargado ({len(entries)} entradas).",
                 stage="tts_done")
        else:
            # Fallback: generate SRT from text + audio duration
            srt_content = _make_script_srt(clean_text, complete_path)
            subtitles_path.write_text(srt_content, encoding="utf-8")
            _log(db, project_id,
                 "SRT generado desde texto del script (TTS no retorno subtitulos).",
                 stage="tts_done")

        # Mark all scene chunks as done
        chunks = db.query(Chunk).filter(Chunk.project_id == project_id).all()
        for chunk in chunks:
            _update_chunk(db, chunk, status=ChunkStatus.done)

        _update_project(
            db, project,
            status=ProjectStatus.awaiting_audio_approval,
            voiceover_path=str(complete_path),
        )
        _log(db, project_id,
             f"Voiceover generado exitosamente ({len(chunks)} escenas). Esperando aprobacion de audio.",
             stage="tts_done")

    except _ProjectGoneError:
        print(f"[INFO][tts] Project {project_id} was deleted mid-run, aborting.")
    except Exception as exc:
        db.rollback()
        db.expire_all()
        project = db.query(Project).filter(Project.id == project_id).first()
        if project:
            try:
                _update_project(db, project, status=ProjectStatus.error, error_message=str(exc))
            except Exception:
                pass
        _log(db, project_id, f"TTS pipeline error: {exc}\n{traceback.format_exc()}", stage="error", level="error")
    finally:
        db.close()


def _animated_branch(db, project_id, chunk, n, slug, narration, visual_desc, reference_character, c_dir, api_key: str = "") -> Path:
    """Animated mode: image prompt → Pollinations image → WaveSpeed i2v → return video path."""
    # ── 3c-i. Generate image prompt ────────────────────────────────────────
    _log(db, project_id, f"[Chunk {n}] Generando prompt de imagen…", stage=f"chunk_{n}_imgprompt")
    img_prompt = (chunk.image_prompt or "").strip()
    if not img_prompt:
        img_prompt = (generate_image_prompt(narration, visual_desc, reference_character or "") or "").strip()
    if not img_prompt:
        img_prompt = (narration or "").strip()[:800]
    _update_chunk(db, chunk, image_prompt=img_prompt)

    # ── 3c-ii. Generate image ───────────────────────────────────────────
    img_provider = _get_image_provider(db)
    _log(db, project_id, f"[imagen_{n}] Generando con {img_provider.capitalize()}…", stage=f"chunk_{n}_image")
    img_path = c_dir / "images" / f"image_{n}.jpg"
    img_path.parent.mkdir(parents=True, exist_ok=True)
    poll_key = _get_pollinations_api_key(db)
    ws_key = _get_wavespeed_api_key(db)
    project_obj = db.query(Project).filter(Project.id == project_id).first()
    ref_char = _get_reference_character(db, project_obj) if project_obj else None
    ref_style = _get_reference_style(db, project_obj) if project_obj else None
    _dispatch_generate_image(
        img_prompt, img_path,
        provider=img_provider, api_key=poll_key, wavespeed_api_key=ws_key,
        reference_character_path=ref_char, reference_style_path=ref_style,
    )
    _update_chunk(db, chunk, image_path=str(img_path))
    _log(db, project_id, f"[imagen_{n}] ✅ Guardada: image_{n}.jpg", stage=f"chunk_{n}_image")

    # ── 3c-iii. Animate image with WaveSpeed i2v ──────────────────────────
    anim_prompt = chunk.motion_prompt or chunk.video_prompt or "Slow cinematic zoom in, subtle camera movement"
    _log(db, project_id, f"[Chunk {n}] Animando imagen con WaveSpeed i2v...", stage=f"chunk_{n}_animate")
    video_path = c_dir / "videos" / f"video_{n}.mp4"
    video_path.parent.mkdir(parents=True, exist_ok=True)
    ws_key = _get_wavespeed_api_key(db)
    try:
        wavespeed_service.animate_image(
            img_path, video_path, prompt=anim_prompt, api_key=ws_key,
        )
    except Exception as vid_exc:
        _log(db, project_id,
             f"[Chunk {n}] Video fallo: {vid_exc}. Usando imagen estatica como respaldo.",
             stage=f"chunk_{n}_animate", level="warning")
        # Return the image path — NCA will treat it as a still frame
        return img_path
    _update_chunk(db, chunk, video_path=str(video_path))
    return video_path


def _stock_branch(db, project_id, chunk, n, slug, narration, visual_desc, c_dir) -> Path:
    """Stock footage mode: extract keywords → search Pexels/Pixabay → return video path."""
    # ── 3d-i. Extract keywords ─────────────────────────────────────────────
    _log(db, project_id, f"[Chunk {n}] Extracting search keywords…", stage=f"chunk_{n}_keywords")
    kw_data = generate_search_keywords(narration, visual_desc)
    primary = kw_data.get("primary_keyword", narration[:50])
    secondaries = kw_data.get("secondary_keywords", [])
    _update_chunk(db, chunk, search_keywords=primary, image_prompt=None)

    # ── 3d-ii. Search and download stock ──────────────────────────────────
    video_path = c_dir / "videos" / f"video_{n}.mp4"
    queries = [primary] + secondaries

    downloaded = False
    for q in queries:
        try:
            _log(db, project_id, f"[Chunk {n}] Searching Pexels: '{q}'…", stage=f"chunk_{n}_stock")
            url = pexels_service.search_video(q)
            if url:
                pexels_service.download_media(url, video_path)
                downloaded = True
                break
        except Exception:
            pass

        if not downloaded:
            try:
                _log(db, project_id, f"[Chunk {n}] Searching Pixabay: '{q}'…", stage=f"chunk_{n}_stock")
                url = pixabay_service.search_video(q)
                if url:
                    pixabay_service.download_media(url, video_path)
                    downloaded = True
                    break
            except Exception:
                pass

    if not downloaded:
        # Last resort: download a photo and treat as a still video
        _log(db, project_id, f"[Chunk {n}] No video found, using photo…", stage=f"chunk_{n}_stock", level="warning" )
        img_path = c_dir / "images" / f"image_{n}.jpg"
        url = pexels_service.search_photo(primary) or pixabay_service.search_photo(primary)
        if url:
            pexels_service.download_media(url, img_path)
            _update_chunk(db, chunk, image_path=str(img_path))
            # Use the image path as the "video" – NCA will convert it
            return img_path
        else:
            raise RuntimeError(f"Could not find any stock media for chunk {n}: '{primary}'")

    _update_chunk(db, chunk, video_path=str(video_path))
    return video_path


# ── Per-chunk image retry ─────────────────────────────────────────────────────

def _run_retry_chunk_image(project_id: int, chunk_number: int) -> None:
    """Re-generate image for a single scene chunk using Pollinations."""
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        chunk = (
            db.query(Chunk)
            .filter(Chunk.project_id == project_id, Chunk.chunk_number == chunk_number)
            .first()
        )
        if not chunk:
            _log(db, project_id, f"Chunk {chunk_number} no encontrado.", stage="retry_media", level="error")
            return

        api_key = _get_pollinations_api_key(db)

        # Reset chunk status so _generate_media_for_chunk doesn't skip it
        _update_chunk(db, chunk, status=ChunkStatus.pending, error_message=None)

        _log(db, project_id,
             f"[Retry {chunk_number}] Reintentando generación de imagen + video…",
             stage=f"retry_media_{chunk_number}")
        _generate_media_for_chunk(
            project_id, chunk.id, project.slug, project.reference_character, api_key
        )
        _log(db, project_id,
             f"[Retry {chunk_number}] ✓ Escena regenerada.",
             stage=f"retry_media_{chunk_number}_done")

    except Exception as exc:
        _log(db, project_id,
             f"[Retry {chunk_number}] Error: {exc}",
             stage="retry_media_error", level="error")
    finally:
        db.close()


def start_retry_chunk_image(project_id: int, chunk_number: int) -> None:
    """Launch single-chunk image retry in a background daemon thread."""
    t = threading.Thread(target=_run_retry_chunk_image, args=(project_id, chunk_number), daemon=True)
    t.start()


# ── Per-chunk image-only regeneration (Google Imagen 4 Fast) ──────────────────

def _run_regenerate_image_genaipro(project_id: int, chunk_number: int) -> None:
    """Re-generate ONLY the image for one scene chunk using Pollinations.

    Uses the existing image_prompt stored in the chunk DB record.
    Overwrites image_N.jpg in-place so downstream FFmpeg picks up the new file.
    """
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        chunk = (
            db.query(Chunk)
            .filter(Chunk.project_id == project_id, Chunk.chunk_number == chunk_number)
            .first()
        )
        if not chunk:
            _log(db, project_id, f"Chunk {chunk_number} no encontrado.", stage="regen_img", level="error")
            return

        img_provider = _get_image_provider(db)
        poll_key = _get_pollinations_api_key(db)
        ws_key = _get_wavespeed_api_key(db)
        ref_char = _get_reference_character(db, project)
        ref_style = _get_reference_style(db, project)
        n = chunk.chunk_number

        # Resolve prompt: prefer image_prompt, fall back to scene_text
        img_prompt = (chunk.image_prompt or "").strip()
        if not img_prompt:
            img_prompt = (chunk.scene_text or "").strip()[:800]
            if img_prompt:
                _log(db, project_id,
                     f"[Regen {n}] ⚠️ Sin image_prompt — usando narración como fallback.",
                     stage=f"regen_img_{n}", level="warning")

        if not img_prompt:
            msg = (
                "⚠️ Sin prompt visual — usa 'Generar Imágenes' para crear el prompt primero, "
                "o edita el campo de prompt manualmente."
            )
            _log(db, project_id, f"[Regen {n}] {msg}", stage="regen_img", level="error")
            _update_chunk(db, chunk, status=ChunkStatus.error, error_message=msg)
            return

        c_dir = chunk_dir(project.slug, n)
        img_path = c_dir / "images" / f"image_{n}.jpg"
        img_path.parent.mkdir(parents=True, exist_ok=True)

        _log(db, project_id,
             f"[imagen_{n}] Generando con {img_provider.capitalize()}…",
             stage=f"regen_img_{n}")

        _dispatch_generate_image(
            img_prompt, img_path,
            provider=img_provider, api_key=poll_key, wavespeed_api_key=ws_key,
            reference_character_path=ref_char, reference_style_path=ref_style,
        )

        _log(db, project_id,
             f"[imagen_{n}] ✅ Guardada: image_{n}.jpg",
             stage=f"regen_img_{n}")

        _update_chunk(db, chunk, status=ChunkStatus.done, image_path=str(img_path), error_message=None)

        # Also clear project-level error if this was a manual retry that succeeded
        _update_project(db, project, status=ProjectStatus.images_ready, error_message=None)

        _log(db, project_id,
             f"✅ Escena #{n} actualizada y marcada como lista",
             stage=f"regen_img_{n}_done")

    except Exception as exc:
        _log(db, project_id,
             f"[Regen {chunk_number}] Error: {exc}",
             stage="regen_img_error", level="error")
        # Mark chunk as error so the UI shows a red badge
        try:
            db.expire_all()
            chunk = db.query(Chunk).filter(
                Chunk.project_id == project_id, Chunk.chunk_number == chunk_number
            ).first()
            if chunk:
                _update_chunk(db, chunk, status=ChunkStatus.error, error_message=str(exc))
        except Exception:
            pass
    finally:
        db.close()


def start_regenerate_image_genaipro(project_id: int, chunk_number: int) -> None:
    """Launch single-chunk Pollinations image regeneration in a background daemon thread."""
    t = threading.Thread(
        target=_run_regenerate_image_genaipro,
        args=(project_id, chunk_number),
        daemon=True,
    )
    t.start()


# ── Bulk image regeneration (all scenes) — Google Imagen 4 Fast ──────────────

def _run_regenerate_all_genaipro(project_id: int) -> None:
    """Re-generate images for ALL scene chunks using Pollinations.

    Uses image_prompt if available, falls back to scene_text.
    Processes up to 5 images in parallel via ThreadPoolExecutor.
    Overwrites image_N.jpg in-place.
    Does NOT touch motion prompts or videos — image only.
    """
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        img_provider = _get_image_provider(db)
        poll_key = _get_pollinations_api_key(db)
        ws_key = _get_wavespeed_api_key(db)
        ref_char = _get_reference_character(db, project)
        ref_style = _get_reference_style(db, project)

        chunks = (
            db.query(Chunk)
            .filter(Chunk.project_id == project_id)
            .order_by(Chunk.chunk_number)
            .all()
        )

        if not chunks:
            _log(db, project_id, "No hay escenas en este proyecto.", stage="regen_all", level="warning")
            return

        total = len(chunks)
        _log(db, project_id,
             f"⚡ Regenerando {total} imágenes con {img_provider.capitalize()} (paralelo)…",
             stage="regen_all")

        # Prepare tasks: resolve prompts and paths upfront
        tasks: list[dict] = []
        skipped: list[str] = []
        for chunk in chunks:
            n = chunk.chunk_number
            img_prompt = (chunk.image_prompt or "").strip()
            if not img_prompt:
                img_prompt = (chunk.scene_text or "").strip()[:800]
                if img_prompt:
                    _log(db, project_id,
                         f"[Regen {n}] ⚠️ Sin image_prompt — usando narración como fallback.",
                         stage="regen_all_progress", level="warning")
            if not img_prompt:
                msg = f"Escena #{n}: sin prompt y sin texto de escena — omitida."
                skipped.append(msg)
                _log(db, project_id, f"⚠️ {msg}", stage="regen_all_progress", level="warning")
                _update_chunk(db, chunk, status=ChunkStatus.error,
                              error_message="Sin prompt visual — genera los prompts primero.")
                continue

            c_dir = chunk_dir(project.slug, n)
            img_path = c_dir / "images" / f"image_{n}.jpg"
            img_path.parent.mkdir(parents=True, exist_ok=True)
            tasks.append({"chunk": chunk, "prompt": img_prompt, "path": img_path, "n": n})

        # Generate images in parallel (max 5 concurrent)
        errors: list[str] = []

        def _gen_one(task: dict) -> tuple[int, str | None]:
            """Generate a single image. Returns (chunk_number, error_or_None)."""
            n = task["n"]
            try:
                print(f"[imagen_{n}] Generando con {img_provider.capitalize()}...")
                _dispatch_generate_image(
                    task["prompt"], task["path"],
                    provider=img_provider, api_key=poll_key, wavespeed_api_key=ws_key,
                    reference_character_path=ref_char, reference_style_path=ref_style,
                )
                print(f"[imagen_{n}] Guardada: image_{n}.jpg")
                return (n, None)
            except Exception as exc:
                return (n, str(exc))

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(_gen_one, t): t for t in tasks}
            done_count = 0
            for future in as_completed(futures):
                task = futures[future]
                n = task["n"]
                chunk = task["chunk"]
                done_count += 1
                n_result, err = future.result()
                if err:
                    errors.append(f"Escena #{n}: {err}")
                    _log(db, project_id,
                         f"❌ Imagen escena #{n} falló: {err}",
                         stage="regen_all_progress", level="error")
                    update_db = SessionLocal()
                    try:
                        c = update_db.query(Chunk).filter(
                            Chunk.project_id == project_id,
                            Chunk.chunk_number == n,
                        ).first()
                        if c:
                            c.status = ChunkStatus.error
                            c.error_message = err
                            c.updated_at = datetime.utcnow()
                            update_db.commit()
                    except Exception:
                        update_db.rollback()
                    finally:
                        update_db.close()
                else:
                    _log(db, project_id,
                         f"✅ Escena #{n} regenerada ({done_count}/{len(tasks)})",
                         stage="regen_all_progress")
                    # Use a fresh session for each DB update to avoid SQLite locking
                    update_db = SessionLocal()
                    try:
                        c = update_db.query(Chunk).filter(
                            Chunk.project_id == project_id,
                            Chunk.chunk_number == n,
                        ).first()
                        if c:
                            c.status = ChunkStatus.done
                            c.image_path = str(task["path"])
                            c.error_message = None
                            c.updated_at = datetime.utcnow()
                            update_db.commit()
                    except Exception as db_exc:
                        update_db.rollback()
                        _log(db, project_id,
                             f"⚠️ Escena #{n}: imagen guardada en disco pero DB falló: {db_exc}",
                             stage="regen_all_progress", level="warning")
                    finally:
                        update_db.close()

        all_errors = skipped + errors
        if all_errors:
            _log(db, project_id,
                 f"⚠️ Regeneración completada con {len(all_errors)} error(es): {'; '.join(all_errors[:3])}",
                 stage="regen_all_done", level="error")
        else:
            _update_project(db, project, status=ProjectStatus.images_ready, error_message=None)
            _log(db, project_id,
                 f"✅ {total} imágenes regeneradas con Pollinations.",
                 stage="regen_all_done")

    except Exception as exc:
        _log(db, project_id,
             f"Error en regeneración masiva: {exc}\n{traceback.format_exc()}",
             stage="regen_all_error", level="error")
    finally:
        db.close()


def start_regenerate_all_genaipro(project_id: int) -> None:
    """Launch bulk image regeneration (Pollinations) in a background daemon thread."""
    t = threading.Thread(
        target=_run_regenerate_all_genaipro,
        args=(project_id,),
        daemon=True,
    )
    t.start()


# ── Phase 3.5: Generación de Motion Prompts ───────────────────────────────────

def _run_generate_motion_prompts(project_id: int) -> None:
    """Iterate over all chunks and generate motion prompts via Claude."""
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        # Status logic: could use generating_motion_prompts. 
        # Using a general 'processing' or sticking to images_ready to keep UI simple.
        _log(db, project_id, "Iniciando generación de Motion Prompts…", stage="motion_prompts")
        
        chunks = (
            db.query(Chunk)
            .filter(Chunk.project_id == project_id, (Chunk.motion_prompt == None) | (Chunk.motion_prompt == ""))
            .order_by(Chunk.chunk_number)
            .all()
        )
        for chunk in chunks:
            if not chunk.scene_text or not chunk.image_prompt:
                continue
            try:
                prompt = motion_service.generate_motion_prompt(chunk.scene_text, chunk.image_prompt)
                _update_chunk(db, chunk, motion_prompt=prompt)
            except Exception as e:
                _log(db, project_id, f"Error generando motion prompt para chunk {chunk.chunk_number}: {e}", stage="motion_prompts", level="error")
                
        _log(db, project_id, "Motion Prompts generados exitosamente.", stage="motion_prompts_done")
    except Exception as exc:
        _log(db, project_id, f"Error en _run_generate_motion_prompts: {exc}", stage="motion_prompts_error", level="error")
    finally:
        db.close()

def start_generate_motion_prompts(project_id: int) -> None:
    t = threading.Thread(target=_run_generate_motion_prompts, args=(project_id,), daemon=True)
    t.start()


# ── Phase 4: Animación con WaveSpeed i2v ───────────────────────────────────────

def _animate_one_scene(project_id: int, chunk_number: int, slug: str, api_key: str) -> tuple[int, str | None]:
    """Animate a single scene with WaveSpeed i2v. Returns (chunk_number, error_or_None)."""
    db = SessionLocal()
    try:
        chunk = db.query(Chunk).filter(
            Chunk.project_id == project_id, Chunk.chunk_number == chunk_number,
        ).first()
        if not chunk or not chunk.image_path:
            return (chunk_number, "Sin imagen")

        n = chunk.chunk_number
        anim_prompt = chunk.motion_prompt or chunk.video_prompt or "Slow cinematic zoom in, subtle camera movement"

        c_dir = chunk_dir(slug, n)
        video_path = c_dir / "videos" / f"video_{n}.mp4"
        video_path.parent.mkdir(parents=True, exist_ok=True)

        print(f"[WaveSpeed {n}] Animando: {anim_prompt[:80]}...")
        wavespeed_service.animate_image(
            Path(chunk.image_path), video_path,
            prompt=anim_prompt, api_key=api_key,
        )

        # Update DB
        chunk.video_path = str(video_path)
        chunk.status = ChunkStatus.done
        chunk.error_message = None
        chunk.updated_at = datetime.utcnow()
        db.commit()
        print(f"[WaveSpeed {n}] Video guardado: video_{n}.mp4 ({video_path.stat().st_size // 1024} KB)")
        return (n, None)

    except Exception as exc:
        db.rollback()
        try:
            chunk = db.query(Chunk).filter(
                Chunk.project_id == project_id, Chunk.chunk_number == chunk_number,
            ).first()
            if chunk:
                chunk.status = ChunkStatus.error
                chunk.error_message = str(exc)
                chunk.updated_at = datetime.utcnow()
                db.commit()
        except Exception:
            pass
        return (chunk_number, str(exc))
    finally:
        db.close()


def _run_animate_scenes(project_id: int) -> None:
    """Animate all scenes that have images but no videos using WaveSpeed i2v.

    Runs max 2 concurrent animations (video generation takes 1-3 min each).
    """
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        ws_key = _get_wavespeed_api_key(db)
        if not ws_key:
            _log(db, project_id, "WAVESPEED_API_KEY no configurado.", stage="animate", level="error")
            return

        chunks = (
            db.query(Chunk)
            .filter(
                Chunk.project_id == project_id,
                Chunk.image_path != None,
                (Chunk.video_path == None) | (Chunk.video_path == ""),
            )
            .order_by(Chunk.chunk_number)
            .all()
        )

        if not chunks:
            _log(db, project_id, "No hay escenas pendientes de animacion.", stage="animate")
            return

        total = len(chunks)
        _log(db, project_id,
             f"Animando {total} escenas con WaveSpeed i2v (max 2 simultaneas)...",
             stage="animate")

        slug = project.slug
        chunk_numbers = [c.chunk_number for c in chunks]

        errors: list[str] = []
        done_count = 0
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                pool.submit(_animate_one_scene, project_id, cn, slug, ws_key): cn
                for cn in chunk_numbers
            }
            for future in as_completed(futures):
                cn = futures[future]
                done_count += 1
                n_result, err = future.result()
                if err:
                    errors.append(f"Escena #{cn}: {err}")
                    _log(db, project_id,
                         f"Animacion escena #{cn} fallo: {err}",
                         stage="animate_progress", level="error")
                else:
                    _log(db, project_id,
                         f"Escena #{cn} animada ({done_count}/{total})",
                         stage="animate_progress")

        if errors:
            _log(db, project_id,
                 f"Animacion completada con {len(errors)} error(es): {'; '.join(errors[:3])}",
                 stage="animate_done", level="error")
        else:
            _log(db, project_id,
                 f"{total} escenas animadas con WaveSpeed.",
                 stage="animate_done")

    except Exception as exc:
        _log(db, project_id,
             f"Error en animación masiva: {exc}\n{traceback.format_exc()}",
             stage="animate_error", level="error")
    finally:
        db.close()


def start_animate_scenes(project_id: int) -> None:
    t = threading.Thread(target=_run_animate_scenes, args=(project_id,), daemon=True)
    t.start()
