"""
Pipeline orchestrator.

Modes:
  - animated: Claude → TTS → Whisper → ImagePrompt → SeedDream → LTX → NCA
  - stock:    Claude → TTS → Whisper → Keywords → Pexels/Pixabay → NCA

Chunk processing runs in a thread pool. Progress is persisted to SQLite
so the frontend can poll for updates.
"""
from __future__ import annotations

import os
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
from .openai_service import generate_tts, transcribe_to_srt
from . import replicate_service, pexels_service, pixabay_service, nca_service

MAX_WORKERS = settings.max_workers


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


def start_pipeline_phase3(project_id: int):
    """Phase 3: generate images/videos and render all chunks (audio already exists)."""
    t = threading.Thread(target=_run_pipeline_phase3, args=(project_id,), daemon=True)
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

        # SRT: use existing (from GenAIPro) or transcribe via Whisper
        if chunk.srt_path and Path(chunk.srt_path).exists():
            srt_path = Path(chunk.srt_path)
            _log(db, project_id, f"[Chunk {n}] Usando SRT existente.", stage=f"chunk_{n}_srt")
        else:
            srt_path = audio_path.with_suffix(".srt")
            _log(db, project_id, f"[Chunk {n}] Transcribiendo audio con Whisper…", stage=f"chunk_{n}_srt")
            transcribe_to_srt(audio_path, srt_path)
            _update_chunk(db, chunk, srt_path=str(srt_path))

        if mode == VideoMode.animated:
            video_path = _animated_branch(db, project_id, chunk, n, slug, narration, visual_desc, reference_character, c_dir)
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
                # SRT may have been saved alongside by GenAIPro provider
                srt_path = audio_path.with_suffix(".srt")
                srt_path_str = str(srt_path) if srt_path.exists() else None
                _update_chunk(db, chunk, audio_path=str(audio_path), srt_path=srt_path_str, status=ChunkStatus.done)
                _log(
                    db, project_id,
                    f"[Chunk {chunk.chunk_number}] Audio generado ({size_kb} KB).",
                    stage=f"chunk_{chunk.chunk_number}_tts",
                )
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


# ── Per-chunk processing ──────────────────────────────────────────────────────

def _process_chunk(
    project_id: int,
    chunk_id: int,
    slug: str,
    mode: VideoMode,
    reference_character: str | None,
):
    """Process one scene chunk end-to-end. Each thread opens its own DB session."""
    db = SessionLocal()
    try:
        chunk = db.query(Chunk).filter(Chunk.id == chunk_id).first()
        _update_chunk(db, chunk, status=ChunkStatus.processing)
        n = chunk.chunk_number
        narration = chunk.scene_text
        visual_desc = chunk.image_prompt or ""  # reused field temporarily

        _log(db, project_id, f"[Chunk {n}] Starting…", stage=f"chunk_{n}")

        # Paths
        vo_dir = voiceover_dir(slug)
        c_dir = chunk_dir(slug, n)
        r_dir = rendered_dir(slug)
        f_dir = final_dir(slug)
        for d in (vo_dir, c_dir / "images", c_dir / "videos", r_dir, f_dir):
            d.mkdir(parents=True, exist_ok=True)

        audio_path = vo_dir / f"audio-chunk-{n}.mp3"
        srt_path = vo_dir / f"audio-chunk-{n}.srt"

        # ── 3a. TTS ────────────────────────────────────────────────────────
        _log(db, project_id, f"[Chunk {n}] Generating voiceover…", stage=f"chunk_{n}_tts")
        generate_tts(narration, audio_path)
        _update_chunk(db, chunk, audio_path=str(audio_path))

        # ── 3b. Whisper → SRT ──────────────────────────────────────────────
        _log(db, project_id, f"[Chunk {n}] Transcribing audio…", stage=f"chunk_{n}_srt")
        transcribe_to_srt(audio_path, srt_path)
        _update_chunk(db, chunk, srt_path=str(srt_path))

        if mode == VideoMode.animated:
            video_path = _animated_branch(db, project_id, chunk, n, slug, narration, visual_desc, reference_character, c_dir)
        else:
            video_path = _stock_branch(db, project_id, chunk, n, slug, narration, visual_desc, c_dir)

        # ── 3e. Render with NCA (video + audio + subtitles) ────────────────
        _log(db, project_id, f"[Chunk {n}] Rendering chunk with NCA…", stage=f"chunk_{n}_render")
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
        _log(db, project_id, f"[Chunk {chunk_id}] Error: {exc}", stage="chunk_error", level="error")
        raise
    finally:
        db.close()


def _animated_branch(db, project_id, chunk, n, slug, narration, visual_desc, reference_character, c_dir) -> Path:
    """Animated mode: generate image → animate → return video path."""
    # ── 3c-i. Generate image prompt ────────────────────────────────────────
    _log(db, project_id, f"[Chunk {n}] Generating image prompt…", stage=f"chunk_{n}_imgprompt")
    img_prompt = generate_image_prompt(narration, visual_desc, reference_character or "")
    _update_chunk(db, chunk, image_prompt=img_prompt)

    # ── 3c-ii. Generate image ──────────────────────────────────────────────
    _log(db, project_id, f"[Chunk {n}] Generating image with SeedDream…", stage=f"chunk_{n}_image")
    img_path = c_dir / "images" / f"image_{n}.jpg"
    replicate_service.generate_image(img_prompt, img_path)
    _update_chunk(db, chunk, image_path=str(img_path))

    # ── 3c-iii. Animate image ──────────────────────────────────────────────
    _log(db, project_id, f"[Chunk {n}] Animating with LTX Video…", stage=f"chunk_{n}_animate")
    video_path = c_dir / "videos" / f"video_{n}.mp4"
    replicate_service.animate_image(img_path, video_path, prompt=img_prompt)
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
