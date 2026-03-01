"""Project CRUD endpoints."""
import json
import re
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Project, Chunk, ProjectStatus
from ..schemas import ProjectCreate, ProjectOut, ProjectListItem, ScriptApprovalPayload, ResplitPayload, VoiceConfigPayload
from ..services.pipeline_service import start_pipeline, start_pipeline_phase2, start_regenerate_script, start_resplit_chunks, start_generate_voiceover, start_pipeline_phase3

router = APIRouter(prefix="/api/projects", tags=["projects"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[\s_-]+", "-", text).strip("-")
    return text[:60]


def _unique_slug(db: Session, base: str) -> str:
    slug = base
    counter = 1
    while db.query(Project).filter(Project.slug == slug).first():
        slug = f"{base}-{counter}"
        counter += 1
    return slug


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/", response_model=List[ProjectListItem])
def list_projects(db: Session = Depends(get_db)):
    projects = (
        db.query(Project)
        .order_by(Project.created_at.desc())
        .all()
    )
    result = []
    for p in projects:
        total = db.query(Chunk).filter(Chunk.project_id == p.id).count()
        done = (
            db.query(Chunk)
            .filter(Chunk.project_id == p.id, Chunk.status == "done")
            .count()
        )
        result.append(
            ProjectListItem(
                id=p.id,
                title=p.title,
                slug=p.slug,
                mode=p.mode,
                status=p.status,
                created_at=p.created_at,
                updated_at=p.updated_at,
                chunk_count=total,
                chunks_done=done,
            )
        )
    return result


@router.post("/", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(payload: ProjectCreate, db: Session = Depends(get_db)):
    base_slug = _slugify(payload.title)
    slug = _unique_slug(db, base_slug)

    project = Project(
        title=payload.title,
        slug=slug,
        mode=payload.mode,
        topic=payload.topic,
        video_type=payload.video_type,
        duration=payload.duration,
        reference_character=payload.reference_character,
        reference_transcripts=payload.reference_transcripts,
        target_chunk_size=payload.target_chunk_size,
        status=ProjectStatus.queued,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(project)
    db.commit()
    db.refresh(project)

    # Launch background pipeline
    start_pipeline(project.id)
    return project


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: int, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(project_id: int, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    db.delete(project)
    db.commit()


@router.post("/{project_id}/approve-script", response_model=ProjectOut)
def approve_script(project_id: int, payload: ScriptApprovalPayload, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.status != ProjectStatus.awaiting_approval:
        raise HTTPException(status_code=400, detail="Project is not awaiting script approval")

    # Save the (possibly edited) final script + chunk size config
    final = payload.script_final.strip() if payload.script_final else project.script
    project.script_approved = True
    project.script_final = final
    project.target_chunk_size = payload.target_chunk_size
    project.status = ProjectStatus.queued
    project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(project)

    # Launch phase 2 in background
    start_pipeline_phase2(project.id)
    return project


@router.post("/{project_id}/regenerate-script", response_model=ProjectOut)
def regenerate_script(project_id: int, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.status != ProjectStatus.awaiting_approval:
        raise HTTPException(status_code=400, detail="Project is not awaiting script approval")
    if not project.outline:
        raise HTTPException(status_code=400, detail="No outline available to regenerate from")

    project.status = ProjectStatus.queued
    project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(project)

    start_regenerate_script(project.id)
    return project


@router.post("/{project_id}/resplit", response_model=ProjectOut)
def resplit_chunks(project_id: int, payload: ResplitPayload, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.status not in (ProjectStatus.done, ProjectStatus.awaiting_voice_config, ProjectStatus.awaiting_audio_approval):
        raise HTTPException(status_code=400, detail="Resplit only available when project is done, awaiting voice config, or awaiting audio approval")

    start_resplit_chunks(project.id, payload.target_chunk_size)
    return project


@router.post("/{project_id}/voice-config", response_model=ProjectOut)
def save_voice_config(project_id: int, payload: VoiceConfigPayload, db: Session = Depends(get_db)):
    """Save TTS voice configuration. Status stays awaiting_voice_config until a TTS API is connected."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.status != ProjectStatus.awaiting_voice_config:
        raise HTTPException(status_code=400, detail="Project is not awaiting voice configuration")

    project.tts_provider = payload.tts_provider
    project.tts_api_key = payload.tts_api_key
    project.tts_voice_id = payload.tts_voice_id
    project.tts_config = payload.tts_config
    project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(project)
    return project


@router.post("/{project_id}/test-voice")
def test_voice(
    project_id: int,
    payload: VoiceConfigPayload,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Generate a short test clip (first 200 chars) and stream it back as audio/mpeg."""
    from ..services.tts import get_provider

    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    script = project.script_final or project.script or ""
    test_text = script[:200].strip()
    if not test_text:
        raise HTTPException(status_code=400, detail="No hay script disponible para la vista previa")

    # Reconstruct config dict (voice_id lives in tts_voice_id, put it back)
    config = json.loads(payload.tts_config or "{}")
    if payload.tts_voice_id:
        config["voice_id"] = payload.tts_voice_id

    try:
        provider = get_provider(payload.tts_provider, payload.tts_api_key, config)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Write audio to a temp file, return it, delete it after response
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()

    try:
        provider.test(test_text, tmp_path)
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=502, detail=f"Error de TTS: {exc}")

    background_tasks.add_task(tmp_path.unlink, True)  # missing_ok=True
    return FileResponse(str(tmp_path), media_type="audio/mpeg", filename="preview.mp3")


@router.post("/{project_id}/generate-voiceover", response_model=ProjectOut)
def generate_voiceover(project_id: int, payload: VoiceConfigPayload, db: Session = Depends(get_db)):
    """Save voice config and launch background TTS generation for all chunks."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.status != ProjectStatus.awaiting_voice_config:
        raise HTTPException(status_code=400, detail="El proyecto no está esperando configuración de voz")

    # Persist voice config
    project.tts_provider = payload.tts_provider
    project.tts_api_key  = payload.tts_api_key
    project.tts_voice_id = payload.tts_voice_id
    project.tts_config   = payload.tts_config
    project.updated_at   = datetime.utcnow()
    db.commit()
    db.refresh(project)

    start_generate_voiceover(project.id)
    return project


@router.get("/{project_id}/voiceover/audio")
def get_voiceover_audio(project_id: int, db: Session = Depends(get_db)):
    """Serve the concatenated voiceover MP3 for playback."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not project.voiceover_path:
        raise HTTPException(status_code=404, detail="No hay voiceover generado")
    path = Path(project.voiceover_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Archivo de audio no encontrado en disco")
    return FileResponse(str(path), media_type="audio/mpeg", filename="audio-completo.mp3")


@router.post("/{project_id}/approve-audio", response_model=ProjectOut)
def approve_audio(project_id: int, db: Session = Depends(get_db)):
    """Approve the generated voiceover and start video processing (phase 3)."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.status != ProjectStatus.awaiting_audio_approval:
        raise HTTPException(status_code=400, detail="El proyecto no está esperando aprobación de audio")

    project.status = ProjectStatus.queued
    project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(project)

    start_pipeline_phase3(project.id)
    return project


@router.post("/{project_id}/regenerate-voiceover", response_model=ProjectOut)
def regenerate_voiceover_endpoint(project_id: int, db: Session = Depends(get_db)):
    """Discard current voiceover and go back to voice configuration."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.status != ProjectStatus.awaiting_audio_approval:
        raise HTTPException(status_code=400, detail="El proyecto no está en estado de aprobación de audio")

    project.status = ProjectStatus.awaiting_voice_config
    project.voiceover_path = None
    project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(project)
    return project


@router.post("/{project_id}/retry", response_model=ProjectOut)
def retry_project(project_id: int, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.status not in (ProjectStatus.error,):
        raise HTTPException(status_code=400, detail="Only failed projects can be retried")

    # Reset project and chunks
    project.status = ProjectStatus.queued
    project.error_message = None
    project.final_video_path = None
    project.updated_at = datetime.utcnow()

    for chunk in project.chunks:
        db.delete(chunk)
    db.commit()
    db.refresh(project)

    start_pipeline(project.id)
    return project
