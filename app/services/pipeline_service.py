"""
Pipeline orchestrator.

Modes:
  - animated: Claude → TTS → ImagePrompt → SeedDream → LTX → NCA
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
    generate_script,
    generate_outline,
    generate_script_from_outline,
    clean_script,
    generate_image_prompt,
    generate_search_keywords,
    DURATION_SCENES,
)
from .openai_service import generate_tts
from . import replicate_service, pexels_service, pixabay_service, nca_service, google_service, genaipro_media_service
from .video import motion_service, meta_bot, grok_service

MAX_WORKERS = settings.max_workers


# ── DB setting helper ─────────────────────────────────────────────────────────

def _get_db_setting(db, key: str) -> str:
    """Fetch a value from the AppSetting table. Returns empty string if not found."""
    from ..models import AppSetting
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    return (row.value or "") if row else ""


# ── Logging helper ────────────────────────────────────────────────────────────

def _log(db: Session, project_id: int, message: str, stage: str = "", level: str = "info"):
    from ..models import Log
    print(f"[{level.upper()}][{stage}] {message}")
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


# ── Script splitting ──────────────────────────────────────────────────────────

def _find_sentence_break(text: str, target: int) -> int:
    """Find the nearest sentence-ending position (.?!) around target index."""
    start = min(target, len(text) - 1)
    # Search backwards up to 400 chars
    for i in range(start, max(start - 400, 0), -1):
        if text[i] in '.?!' and (i + 1 >= len(text) or text[i + 1] in ' \n\t\r'):
            return i + 1
    # Search forwards up to 400 chars
    for i in range(start, min(start + 400, len(text))):
        if text[i] in '.?!' and (i + 1 >= len(text) or text[i + 1] in ' \n\t\r'):
            return i + 1
    # Hard fallback
    return target


def _split_script_by_chars(text: str, target_size: int = 1500) -> list:
    """
    Split text into chunks of ~target_size characters.
    Never cuts in the middle of a sentence: finds the nearest .?! boundary.
    """
    chunks = []
    remaining = text.strip()
    num = 1
    while remaining:
        if len(remaining) <= target_size:
            if remaining.strip():
                chunks.append({"chunk_number": num, "narration": remaining.strip()})
            break
        break_at = _find_sentence_break(remaining, target_size)
        chunk_text = remaining[:break_at].strip()
        if chunk_text:
            chunks.append({"chunk_number": num, "narration": chunk_text})
            num += 1
        remaining = remaining[break_at:].strip()
    return chunks


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

        # ── 1. Generate outline ────────────────────────────────────────────
        _log(db, project_id, "Generating outline with Claude…", stage="outline")
        import json as _json
        transcripts = []
        if project.reference_transcripts:
            try:
                transcripts = _json.loads(project.reference_transcripts)
            except Exception:
                transcripts = []
        outline = generate_outline(project.title, transcripts or None)
        _update_project(db, project, outline=outline)
        _log(db, project_id, "Outline generated successfully", stage="outline")

        # ── 2. Generate script from outline ────────────────────────────────
        _log(db, project_id, "Generating script from outline…", stage="script")
        script_text = generate_script_from_outline(outline, project.duration or "6-8")
        script_text = clean_script(script_text)
        _update_project(db, project, script=script_text)
        _log(db, project_id, "Script generated. Awaiting manual approval.", stage="script")

        # ── 3. Pause — wait for user approval ─────────────────────────────
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

        _update_project(db, project, status=ProjectStatus.processing)
        _log(db, project_id, "Regenerating script from existing outline…", stage="script")

        outline = project.outline
        if not outline:
            raise RuntimeError("No outline found. Cannot regenerate script.")

        script_text = generate_script_from_outline(outline, project.duration or "6-8")
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
    """Split the approved script into character-based chunks and save them to DB."""
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        _update_project(db, project, status=ProjectStatus.processing)
        _log(db, project_id, "Dividiendo script en chunks…", stage="chunks")

        script_text = project.script_final or project.script
        if not script_text:
            raise RuntimeError("No hay script disponible para dividir.")

        target_size = project.target_chunk_size or 1500
        chunks_data = _split_script_by_chars(script_text, target_size)
        _log(db, project_id, f"Script dividido en {len(chunks_data)} chunks (target {target_size} chars)", stage="chunks")

        # Delete any existing chunks, then insert fresh ones
        db.query(Chunk).filter(Chunk.project_id == project_id).delete()
        db.commit()

        for c in chunks_data:
            db.add(Chunk(
                project_id=project_id,
                chunk_number=c["chunk_number"],
                status=ChunkStatus.queued,
                scene_text=c["narration"],
            ))
        db.commit()

        _update_project(db, project, status=ProjectStatus.awaiting_voice_config)
        _log(db, project_id, f"Chunks creados: {len(chunks_data)} — configurar API de voz para continuar.", stage="done")

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


# ── Re-split chunks with a new target size ────────────────────────────────────

def _resplit_chunks_thread(project_id: int, target_size: int):
    """Delete existing chunks and re-split script with a new character target size."""
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        script_text = project.script_final or project.script
        if not script_text:
            raise RuntimeError("No hay script para re-dividir.")

        _log(db, project_id, f"Re-dividiendo script con target={target_size} chars…", stage="chunks")

        chunks_data = _split_script_by_chars(script_text, target_size)

        db.query(Chunk).filter(Chunk.project_id == project_id).delete()
        db.commit()

        for c in chunks_data:
            db.add(Chunk(
                project_id=project_id,
                chunk_number=c["chunk_number"],
                status=ChunkStatus.queued,
                scene_text=c["narration"],
            ))
        _update_project(db, project, target_chunk_size=target_size, status=ProjectStatus.awaiting_voice_config)
        db.commit()

        _log(db, project_id, f"Re-dividido en {len(chunks_data)} chunks.", stage="done")

    except _ProjectGoneError:
        pass
    except Exception as exc:
        db.rollback()
        _log(db, project_id, f"Error al re-dividir: {exc}", stage="error", level="error")
    finally:
        db.close()


def start_resplit_chunks(project_id: int, target_size: int):
    """Launch chunk re-splitting in a daemon thread."""
    t = threading.Thread(target=_resplit_chunks_thread, args=(project_id, target_size), daemon=True)
    t.start()


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
    3. Global SRT from GenAIPro: vo_dir/subtitles.srt
    4. Synthetic SRT generated from the chunk text
    """
    # 1. Already resolved in DB
    if chunk.srt_path and Path(chunk.srt_path).exists():
        _log(db, project_id, f"[Chunk {n}] Usando SRT existente (DB).", stage=f"chunk_{n}_srt")
        return Path(chunk.srt_path)

    # 2. Per-chunk SRT file on disk (GenAIPro saves alongside the MP3)
    per_chunk_srt = vo_dir / f"audio-chunk-{n}.srt"
    if per_chunk_srt.exists():
        _log(db, project_id, f"[Chunk {n}] Usando SRT por chunk de GenAIPro.", stage=f"chunk_{n}_srt")
        _update_chunk(db, chunk, srt_path=str(per_chunk_srt))
        return per_chunk_srt

    # 3. Global subtitles.srt from GenAIPro
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
    """Divide the voiceover into 5-second scenes and pause at scenes_ready.

    SRT source priority (no external APIs):
    1. voiceover/subtitles.srt
    2. voiceover/audio-chunk-N.srt (concatenated)
    3. Duration estimated from audio-completo.mp3 via mutagen
    """
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        slug = project.slug
        vo = voiceover_dir(slug)
        interval = 5  # seconds per scene

        # ── Resolve SRT entries ────────────────────────────────────────────────
        srt_file, entries = _find_srt_for_project(slug)

        if entries:
            _log(db, project_id,
                 f"SRT encontrado: {Path(srt_file).name} ({len(entries)} entradas). Creando escenas…",
                 stage="srt_scenes")
            total_duration = max(end for _, end, _ in entries)
            use_srt = True
        else:
            # No SRT — estimate duration from audio file size
            _log(db, project_id,
                 "No se encontró SRT. Estimando duración desde audio-completo.mp3…",
                 stage="srt_scenes")
            total_duration, entries = _synthetic_entries_from_audio(slug, db, project_id)
            use_srt = False

        num_scenes = max(1, round(total_duration / interval))
        _log(db, project_id,
             f"Duración total: {total_duration:.1f}s → {num_scenes} escenas de {interval}s.",
             stage="srt_scenes")

        # ── Collect existing chunk texts for fallback ──────────────────────────
        old_chunks = (
            db.query(Chunk)
            .filter(Chunk.project_id == project_id)
            .order_by(Chunk.chunk_number)
            .all()
        )
        full_text = " ".join(c.scene_text or "" for c in old_chunks).strip()

        # ── Delete old chunks ──────────────────────────────────────────────────
        db.query(Chunk).filter(Chunk.project_id == project_id).delete()
        db.commit()

        # ── Create new chunks ──────────────────────────────────────────────────
        words = full_text.split() if not use_srt else []
        for i in range(num_scenes):
            if use_srt:
                scene_start = i * interval
                scene_end   = (i + 1) * interval
                scene_texts = [
                    text for (start, end, text) in entries
                    if start < scene_end and end > scene_start
                ]
                scene_text = " ".join(scene_texts).strip() or f"[Escena {i + 1}]"
            else:
                # Distribute words evenly across scenes
                chunk_start = int(len(words) * i / num_scenes)
                chunk_end   = int(len(words) * (i + 1) / num_scenes)
                scene_text  = " ".join(words[chunk_start:chunk_end]) or f"[Escena {i + 1}]"

            chunk = Chunk(
                project_id=project_id,
                chunk_number=i + 1,
                status=ChunkStatus.pending,
                scene_text=scene_text,
                srt_path=str(srt_file) if srt_file else None,
            )
            db.add(chunk)

        db.commit()
        _log(db, project_id,
             f"{num_scenes} escenas creadas.",
             stage="srt_scenes")

        # ── Slice audio-completo.mp3 into per-scene segments ───────────────────
        # Each new scene chunk needs its own ~5s audio file so that NCA renders
        # the correct portion of the voiceover for that scene.
        audio_complete = vo / "audio-completo.mp3"
        if audio_complete.exists():
            _log(db, project_id,
                 f"Dividiendo audio-completo.mp3 en {num_scenes} segmentos de {interval}s…",
                 stage="srt_scenes")
            new_chunks = (
                db.query(Chunk)
                .filter(Chunk.project_id == project_id)
                .order_by(Chunk.chunk_number)
                .all()
            )
            import shutil as _shutil
            for scene_chunk in new_chunks:
                n = scene_chunk.chunk_number
                start_sec = (n - 1) * interval
                scene_audio = vo / f"audio-chunk-{n}.mp3"
                try:
                    _slice_mp3(audio_complete, scene_audio, start_sec, float(interval))
                    _log(db, project_id,
                         f"[Escena {n}] Audio cortado ({start_sec:.0f}s – {start_sec + interval:.0f}s).",
                         stage="srt_scenes")
                except Exception as exc:
                    _log(db, project_id,
                         f"[Escena {n}] ffmpeg no disponible, copiando audio completo: {exc}",
                         stage="srt_scenes", level="warning")
                    _shutil.copy2(str(audio_complete), str(scene_audio))
                _update_chunk(db, scene_chunk, audio_path=str(scene_audio))
        else:
            _log(db, project_id,
                 "AVISO: audio-completo.mp3 no encontrado — las escenas no tendrán audio.",
                 stage="srt_scenes", level="warning")

        _update_project(db, project, status=ProjectStatus.scenes_ready)
        _log(db, project_id,
             f"✅ {num_scenes} escenas listas. Esperando instrucciones para generar imágenes.",
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
    """Parse global SRT → create scene chunks → start phase 3. Runs in background thread."""
    t = threading.Thread(target=_run_create_scenes_from_srt, args=(project_id,), daemon=True)
    t.start()


# ── Media generation (Genaipro Veo — image + video per scene) ─────────────────

def _generate_media_for_chunk(
    project_id: int,
    chunk_id: int,
    slug: str,
    reference_character: str | None,
    api_key: str,
) -> None:
    """Generate image then video for one scene chunk using Genaipro Veo.

    Steps
    -----
    1. Use pre-generated Gemini image prompt, or fall back to Claude.
    2. Call Genaipro /veo/create-image  → save image_N.jpg.
    3. Get or generate a motion prompt (motion_service / fallback).
    4. Call Genaipro /veo/frames-to-video  → save video_N.mp4.
       If video generation fails the chunk is still marked done with the
       static image so NCA can render it as a still.
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
            _log(db, project_id, f"[Genaipro {n}] ✓ Prompt pre-generado listo.", stage=f"media_{n}")
        else:
            _log(db, project_id, f"[Genaipro {n}] Generando prompt con Claude…", stage=f"media_{n}")
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
                     f"[Genaipro {n}] ⚠️ Claude no generó prompt — usando narración como fallback.",
                     stage=f"media_{n}", level="warning")
            if not img_prompt:
                raise RuntimeError(f"Escena {n} no tiene texto — no se puede generar imagen.")
            _update_chunk(db, chunk, image_prompt=img_prompt)

        print(f"DEBUG [Genaipro escena {n}] Enviando prompt a Genaipro: {img_prompt[:150]}")

        # ── Step 2: image generation ──────────────────────────────────────────
        _log(db, project_id, f"[GenAIPro {n}] 🎨 Creando imagen…", stage=f"media_{n}_img")
        img_path = c_dir / "images" / f"image_{n}.jpg"
        img_path.parent.mkdir(parents=True, exist_ok=True)

        genaipro_media_service.generate_image(img_prompt, img_path, api_key)
        _update_chunk(db, chunk, image_path=str(img_path))
        _log(db, project_id, f"[GenAIPro {n}] ✓ Imagen guardada ({img_path.stat().st_size // 1024} KB).", stage=f"media_{n}_img_done")

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
                     f"[GenAIPro {n}] ⚠️ Motion prompt falló ({mp_exc}), usando fallback.",
                     stage=f"media_{n}", level="warning")
                _update_chunk(db, chunk, motion_prompt=motion)
        
        # We stop here for the image phase.
        # Phase 4 (Meta AI Engine) handles video animation separately.
        _update_chunk(db, chunk, status=ChunkStatus.done)

    except Exception as exc:
        db.rollback()
        db.expire_all()
        chunk = db.query(Chunk).filter(Chunk.id == chunk_id).first()
        if chunk:
            _update_chunk(db, chunk, status=ChunkStatus.error, error_message=str(exc))
        _log(db, project_id, f"[Genaipro chunk {chunk_id}] Error: {exc}", stage="media_error", level="error")
        raise
    finally:
        db.close()


def _run_generate_images(project_id: int) -> None:
    """Generate image + video for every scene chunk using Genaipro Veo (sequential)."""
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        # GenAIPro API key: DB setting → .env fallback
        api_key = (
            _get_db_setting(db, "genaipro_api_key")
            or settings.genaipro_api_key
            or ""
        )
        if not api_key:
            raise RuntimeError(
                "GENAIPRO_API_KEY no configurado. "
                "Agrega la clave en Ajustes → GenAIPro API Keys."
            )

        _update_project(db, project, status=ProjectStatus.generating_images)
        _log(db, project_id, "🎨 Iniciando generación de imágenes con GenAIPro Veo…", stage="media")

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
                     f"✅ {len(prompt_map)} prompts generados. Iniciando Genaipro Veo…",
                     stage="media")
            except Exception as exc:
                _log(db, project_id,
                     f"⚠️ Batch Gemini falló ({exc}). Prompts se generarán por escena.",
                     stage="media", level="warning")

        # ── STEP 2: Image + video generation — sequential (one at a time) ──────
        errors: list[str] = []
        for idx, chunk in enumerate(chunks, 1):
            _log(db, project_id,
                 f"⏳ Procesando escena {idx} de {total} (chunk #{chunk.chunk_number})…",
                 stage="media_progress")
            try:
                _generate_media_for_chunk(
                    project_id, chunk.id, project.slug,
                    project.reference_character, api_key,
                )
                _log(db, project_id,
                     f"✅ Escena {idx}/{total} lista.",
                     stage="media_progress")
            except Exception as exc:
                errors.append(f"Escena #{chunk.chunk_number}: {exc}")
                _log(db, project_id,
                     f"❌ Escena {idx}/{total} (chunk #{chunk.chunk_number}) falló: {exc}",
                     stage="media_progress", level="error")

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
                 f"✅ {total} escenas procesadas (imagen + video) con Genaipro Veo.",
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
    """Launch Genaipro image+video generation in a background daemon thread."""
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

        # SRT: use existing SRT (GenAIPro) or generate synthetic — never calls Whisper
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
        _log(db, project_id, "Iniciando generación de voiceover con TTS…", stage="tts")

        if not project.tts_provider or not project.tts_api_key:
            raise RuntimeError("Proveedor TTS o API key no configurados.")

        tts_config = _json.loads(project.tts_config or "{}")
        if project.tts_voice_id:
            tts_config["voice_id"] = project.tts_voice_id

        try:
            provider = get_provider(project.tts_provider, project.tts_api_key, tts_config)
        except ValueError as exc:
            raise RuntimeError(str(exc))

        chunks = (
            db.query(Chunk)
            .filter(Chunk.project_id == project_id)
            .order_by(Chunk.chunk_number)
            .all()
        )

        if not chunks:
            raise RuntimeError("No hay chunks disponibles para generar audio.")

        vo_dir = voiceover_dir(project.slug)
        vo_dir.mkdir(parents=True, exist_ok=True)

        errors: list[str] = []
        for chunk in chunks:
            try:
                _update_chunk(db, chunk, status=ChunkStatus.processing)
                audio_path = vo_dir / f"audio-chunk-{chunk.chunk_number}.mp3"
                _log(
                    db, project_id,
                    f"[Chunk {chunk.chunk_number}] Generando audio TTS…",
                    stage=f"chunk_{chunk.chunk_number}_tts",
                )
                provider.generate(chunk.scene_text or "", audio_path)
                size_kb = audio_path.stat().st_size // 1024
                _log(
                    db, project_id,
                    f"[Chunk {chunk.chunk_number}] Audio generado ({size_kb} KB).",
                    stage=f"chunk_{chunk.chunk_number}_tts",
                )

                # SRT: use GenAIPro's file if returned, otherwise generate from script
                srt_path = audio_path.with_suffix(".srt")
                if srt_path.exists():
                    srt_path_str = str(srt_path)
                    _log(db, project_id,
                         f"[Chunk {chunk.chunk_number}] SRT descargado desde GenAIPro.",
                         stage=f"chunk_{chunk.chunk_number}_tts")
                else:
                    # GenAIPro didn't return subtitles for this voice —
                    # generate SRT locally from the script text + audio duration.
                    # Text is the exact script that was spoken, so this is correct.
                    try:
                        srt_content = _make_script_srt(chunk.scene_text or "", audio_path)
                        srt_path.write_text(srt_content, encoding="utf-8")
                        srt_path_str = str(srt_path)
                        _log(db, project_id,
                             f"[Chunk {chunk.chunk_number}] SRT generado desde texto del script.",
                             stage=f"chunk_{chunk.chunk_number}_tts")
                    except Exception as srt_exc:
                        srt_path_str = None
                        _log(db, project_id,
                             f"[Chunk {chunk.chunk_number}] No se pudo generar SRT: {srt_exc}",
                             stage=f"chunk_{chunk.chunk_number}_tts",
                             level="warning")

                _update_chunk(db, chunk, audio_path=str(audio_path), srt_path=srt_path_str, status=ChunkStatus.done)
            except Exception as exc:
                _update_chunk(db, chunk, status=ChunkStatus.error, error_message=str(exc))
                _log(
                    db, project_id,
                    f"[Chunk {chunk.chunk_number}] Error TTS: {exc}",
                    stage=f"chunk_{chunk.chunk_number}_tts",
                    level="error",
                )
                errors.append(f"Chunk {chunk.chunk_number}: {exc}")

        if errors:
            _update_project(
                db, project,
                status=ProjectStatus.error,
                error_message=f"TTS errors: {'; '.join(errors)}",
            )
            _log(db, project_id, f"Voiceover completado con errores: {'; '.join(errors)}", stage="tts_done", level="error")
        else:
            # Concatenate all chunk MP3s → audio-completo.mp3
            complete_path = vo_dir / "audio-completo.mp3"
            try:
                audio_files = [
                    vo_dir / f"audio-chunk-{c.chunk_number}.mp3"
                    for c in chunks
                ]
                with open(complete_path, "wb") as out:
                    for af in audio_files:
                        if af.exists():
                            out.write(af.read_bytes())
                _log(db, project_id, f"Audio completo generado: {complete_path.stat().st_size // 1024} KB", stage="tts_done")
            except Exception as exc:
                _log(db, project_id, f"Warning: no se pudo concatenar audio: {exc}", stage="tts_done", level="warning")
                complete_path = None

            # Merge per-chunk SRTs → subtitles.srt (adjusting timestamps)
            _merge_chunk_srts(db, project_id, chunks, vo_dir)

            _update_project(
                db, project,
                status=ProjectStatus.awaiting_audio_approval,
                voiceover_path=str(complete_path) if complete_path and complete_path.exists() else None,
            )
            _log(db, project_id, f"Voiceover generado exitosamente para {len(chunks)} chunks. Esperando aprobación de audio.", stage="tts_done")

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
    """Animated mode: generate image prompt → Genaipro image → Genaipro video → return video path."""
    # Genaipro API key: prefer explicit arg, then DB setting, then .env
    gp_key = api_key or _get_db_setting(db, "genaipro_api_key") or settings.genaipro_api_key or ""
    if not gp_key:
        raise RuntimeError("GENAIPRO_API_KEY no configurado para _animated_branch.")

    # ── 3c-i. Generate image prompt ────────────────────────────────────────
    _log(db, project_id, f"[Chunk {n}] Generando prompt de imagen…", stage=f"chunk_{n}_imgprompt")
    img_prompt = (chunk.image_prompt or "").strip()
    if not img_prompt:
        img_prompt = (generate_image_prompt(narration, visual_desc, reference_character or "") or "").strip()
    if not img_prompt:
        img_prompt = (narration or "").strip()[:800]
    _update_chunk(db, chunk, image_prompt=img_prompt)
    print(f"DEBUG [animated_branch escena {n}] Prompt: {img_prompt[:150]}")

    # ── 3c-ii. Generate image with Genaipro Veo ────────────────────────────
    _log(db, project_id, f"[Chunk {n}] 🎨 Generando imagen con Genaipro Veo…", stage=f"chunk_{n}_image")
    img_path = c_dir / "images" / f"image_{n}.jpg"
    img_path.parent.mkdir(parents=True, exist_ok=True)
    genaipro_media_service.generate_image(img_prompt, img_path, gp_key)
    _update_chunk(db, chunk, image_path=str(img_path))

    # ── 3c-iii. Animate image with Genaipro Veo ────────────────────────────
    anim_prompt = chunk.motion_prompt or chunk.video_prompt or "Slow cinematic zoom in, subtle camera movement"
    _log(db, project_id, f"[Chunk {n}] 🎬 Animando imagen con Genaipro Veo…", stage=f"chunk_{n}_animate")
    video_path = c_dir / "videos" / f"video_{n}.mp4"
    video_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        genaipro_media_service.animate_image(img_path, video_path, gp_key, prompt=anim_prompt)
    except Exception as vid_exc:
        _log(db, project_id,
             f"[Chunk {n}] ⚠️ Video falló: {vid_exc}. Usando imagen estática como respaldo.",
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
    """Re-generate image + video for a single scene chunk using Genaipro Veo."""
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

        api_key = _get_db_setting(db, "genaipro_api_key") or settings.genaipro_api_key or ""
        if not api_key:
            _log(db, project_id, "GENAIPRO_API_KEY no configurado.", stage="retry_media", level="error")
            return

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


# ── Per-chunk Genaipro image-only regeneration ────────────────────────────────

def _run_regenerate_image_genaipro(project_id: int, chunk_number: int) -> None:
    """Re-generate ONLY the image for one scene chunk using Genaipro Veo.

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

        api_key = _get_db_setting(db, "replicate_api_key") or settings.replicate_api_token or ""
        if not api_key:
            _log(db, project_id, "REPLICATE_API_KEY no configurado.", stage="regen_img", level="error")
            return

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

        print(f"DEBUG [Regen escena {n}] Enviando prompt a Genaipro: {img_prompt[:150]}")
        _log(db, project_id,
             f"[Regen {n}] Prompt ({len(img_prompt)} chars): {img_prompt[:100]}…",
             stage=f"regen_img_{n}")

        c_dir = chunk_dir(project.slug, n)
        img_path = c_dir / "images" / f"image_{n}.jpg"
        img_path.parent.mkdir(parents=True, exist_ok=True)

        _log(db, project_id,
             f"[Regen {n}] 🎨 Regenerando imagen con Replicate Seedream…",
             stage=f"regen_img_{n}")

        replicate_service.generate_image(img_prompt, img_path, api_key)
        _update_chunk(db, chunk, status=ChunkStatus.done, image_path=str(img_path), error_message=None)
        
        # Also clear project-level error if this was a manual retry that succeeded
        _update_project(db, project, status=ProjectStatus.images_ready, error_message=None)

        _log(db, project_id,
             f"[Regen {n}] ✓ Imagen regenerada ({img_path.stat().st_size // 1024} KB).",
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
    """Launch single-chunk Genaipro image regeneration in a background daemon thread."""
    t = threading.Thread(
        target=_run_regenerate_image_genaipro,
        args=(project_id, chunk_number),
        daemon=True,
    )
    t.start()


# ── Bulk Genaipro image regeneration (all scenes) ─────────────────────────────

def _run_regenerate_all_genaipro(project_id: int) -> None:
    """Re-generate images for ALL scene chunks using Genaipro Veo.

    Uses image_prompt if available, falls back to scene_text.
    Processes sequentially. Overwrites image_N.jpg in-place.
    Does NOT touch motion prompts or videos — image only.
    """
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        api_key = _get_db_setting(db, "genaipro_api_key") or settings.genaipro_api_key or ""
        if not api_key:
            _log(db, project_id, "GENAIPRO_API_KEY no configurado.", stage="regen_all", level="error")
            return

        # Query ALL chunks — fallback to scene_text handles missing image_prompt
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
             f"⚡ Regenerando {total} imágenes con Replicate Seedream 4.5…",
             stage="regen_all")

        errors: list[str] = []
        for idx, chunk in enumerate(chunks, 1):
            n = chunk.chunk_number
            _log(db, project_id,
                 f"⏳ Regenerando imagen {idx}/{total} (escena #{n})…",
                 stage="regen_all_progress")
            try:
                # Resolve prompt: prefer image_prompt, fall back to scene_text
                img_prompt = (chunk.image_prompt or "").strip()
                if not img_prompt:
                    img_prompt = (chunk.scene_text or "").strip()[:800]
                    if img_prompt:
                        _log(db, project_id,
                             f"[Regen {n}] ⚠️ Sin image_prompt — usando narración como fallback.",
                             stage="regen_all_progress", level="warning")

                if not img_prompt:
                    msg = f"Escena #{n}: sin prompt y sin texto de escena — omitida."
                    errors.append(msg)
                    _log(db, project_id, f"⚠️ {msg}", stage="regen_all_progress", level="warning")
                    _update_chunk(db, chunk, status=ChunkStatus.error,
                                  error_message="Sin prompt visual — genera los prompts primero.")
                    continue

                print(f"DEBUG [Regen ALL escena {n}] Prompt: {img_prompt[:150]}")

                c_dir = chunk_dir(project.slug, n)
                img_path = c_dir / "images" / f"image_{n}.jpg"
                img_path.parent.mkdir(parents=True, exist_ok=True)

                genaipro_media_service.generate_image(img_prompt, img_path, api_key)
                _update_chunk(db, chunk, status=ChunkStatus.done, image_path=str(img_path), error_message=None)

                _log(db, project_id,
                     f"✅ Imagen {idx}/{total} regenerada ({img_path.stat().st_size // 1024} KB).",
                     stage="regen_all_progress")
            except Exception as exc:
                errors.append(f"Escena #{n}: {exc}")
                _log(db, project_id,
                     f"❌ Imagen {idx}/{total} (escena #{n}) falló: {exc}",
                     stage="regen_all_progress", level="error")
                try:
                    db.expire_all()
                    _update_chunk(db, chunk, status=ChunkStatus.error, error_message=str(exc))
                except Exception:
                    pass

        if errors:
            _log(db, project_id,
                 f"⚠️ Regeneración completada con {len(errors)} error(es): {'; '.join(errors[:3])}",
                 stage="regen_all_done", level="error")
        else:
            _update_project(db, project, status=ProjectStatus.images_ready, error_message=None)
            _log(db, project_id,
                 f"✅ {total} imágenes regeneradas con GenAIPro Veo.",
                 stage="regen_all_done")

    except Exception as exc:
        _log(db, project_id,
             f"Error en regeneración masiva: {exc}\n{traceback.format_exc()}",
             stage="regen_all_error", level="error")
    finally:
        db.close()


def start_regenerate_all_genaipro(project_id: int) -> None:
    """Launch bulk Genaipro image regeneration in a background daemon thread."""
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


# ── Phase 4: Animación Masiva (Meta AI) ───────────────────────────────────────

async def _animate_scene_task(db, project, chunk, chunk_n: int, c_dir: Path):
    """
    Animate one scene: tries Meta AI (2 attempts), then Grok+Replicate as Plan B.

    Plan B is activated automatically when Meta AI fails twice and a Grok API
    key is stored in the DB settings (key = 'grok_api_key').
    """
    if not chunk.image_path or not chunk.motion_prompt:
        return

    out_dir = c_dir / "videos"
    out_dir.mkdir(parents=True, exist_ok=True)
    video_path = out_dir / f"video_{chunk_n}.mp4"

    _log(db, project.id, f"[Animación {chunk_n}] Iniciando Meta AI…", stage=f"animate_{chunk_n}")

    # ── Meta AI: 2 attempts ────────────────────────────────────────────────────
    meta_errors: list[str] = []
    for attempt in range(2):
        try:
            await meta_bot.animate_scene(chunk.image_path, chunk.motion_prompt, str(video_path))
            _update_chunk(db, chunk, video_path=str(video_path))
            _log(db, project.id, f"[Animación {chunk_n}] ✓ Meta AI éxito.", stage=f"animate_{chunk_n}_done")
            return
        except Exception as exc:
            meta_errors.append(str(exc))
            _log(
                db, project.id,
                f"[Animación {chunk_n}] Meta AI intento {attempt + 1}/2 falló: {exc}",
                stage=f"animate_{chunk_n}_error",
                level="warning",
            )
            if attempt < 1:
                await asyncio.sleep(5)

    # ── Plan B: Grok Vision → Replicate LTX Video ─────────────────────────────
    grok_key = _get_db_setting(db, "grok_api_key")
    replicate_key = _get_db_setting(db, "replicate_api_key") or settings.replicate_api_token or ""

    if grok_key:
        _log(
            db, project.id,
            f"[Animación {chunk_n}] ⚠️ Meta AI falló 2×. Activando Plan B: Grok + Replicate LTX…",
            stage=f"animate_{chunk_n}_grok",
        )
        try:
            # grok_service is synchronous — run it in a thread so we don't block the loop
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: grok_service.animate_with_grok_fallback(
                    image_path=chunk.image_path,
                    motion_prompt=chunk.motion_prompt,
                    output_path=str(video_path),
                    grok_api_key=grok_key,
                    replicate_api_key=replicate_key,
                ),
            )
            _update_chunk(db, chunk, video_path=str(video_path))
            _log(
                db, project.id,
                f"[Animación {chunk_n}] ✓ Plan B (Grok + Replicate) éxito.",
                stage=f"animate_{chunk_n}_done",
            )
            return
        except Exception as grok_exc:
            _log(
                db, project.id,
                f"[Animación {chunk_n}] Plan B también falló: {grok_exc}",
                stage=f"animate_{chunk_n}_error",
                level="error",
            )
    else:
        _log(
            db, project.id,
            (
                f"[Animación {chunk_n}] ⚠️ Meta AI falló 2×. "
                "Grok API key no configurada — Plan B no disponible. "
                "Agrega 'grok_api_key' en Ajustes para activar el fallback automático."
            ),
            stage=f"animate_{chunk_n}_error",
            level="warning",
        )

    # ── All options exhausted ──────────────────────────────────────────────────
    error_summary = " | ".join(meta_errors[:2])
    _update_chunk(
        db, chunk,
        status=ChunkStatus.error,
        error_message=f"Meta AI (2 intentos) falló; Grok Plan B {'también falló' if grok_key else 'no configurado'}. Último error: {error_summary}",
    )

async def _animate_batch(db, project, chunks):
    """Run Meta AI animations with a semaphore limiting to 3 concurrent tasks."""
    semaphore = asyncio.Semaphore(3)
    
    async def _sem_worker(chunk):
        async with semaphore:
            chunk_n = chunk.chunk_number
            c_dir = chunk_dir(project.slug, chunk_n)
            await _animate_scene_task(db, project, chunk, chunk_n, c_dir)
            
    tasks = [_sem_worker(c) for c in chunks]
    await asyncio.gather(*tasks)

def _run_animate_scenes(project_id: int) -> None:
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        _log(db, project_id, "Iniciando Motor de Animación Masiva (Meta AI)…", stage="animate_mass")
        
        # Select chunks that have images but no videos yet
        chunks = (
            db.query(Chunk)
            .filter(Chunk.project_id == project_id, Chunk.image_path != None, Chunk.video_path == None, Chunk.status != ChunkStatus.error)
            .order_by(Chunk.chunk_number)
            .all()
        )
        
        if not chunks:
            _log(db, project_id, "No hay escenas pendientes de animación.", stage="animate_mass")
            return
            
        asyncio.run(_animate_batch(db, project, chunks))
        _log(db, project_id, "Animación masiva completada.", stage="animate_mass_done")
        
    except Exception as exc:
        _log(db, project_id, f"Error en animación masiva: {exc}", stage="animate_mass_error", level="error")
    finally:
        db.close()

def start_animate_scenes(project_id: int) -> None:
    t = threading.Thread(target=_run_animate_scenes, args=(project_id,), daemon=True)
    t.start()
