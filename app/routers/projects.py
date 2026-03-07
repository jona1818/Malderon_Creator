"""Project CRUD endpoints."""
import json
import re
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Project, Chunk, ProjectStatus, AppSetting
from ..schemas import ProjectCreate, ProjectOut, ProjectListItem, ScriptApprovalPayload, ResplitPayload, VoiceConfigPayload
from ..config import PROJECTS_PATH, settings as app_settings
from ..services.pipeline_service import start_pipeline, start_pipeline_phase2, start_regenerate_script, start_generate_voiceover, start_pipeline_phase3, start_create_scenes_from_srt, start_generate_images, start_retry_chunk_image, start_generate_motion_prompts, start_animate_scenes, start_regenerate_image_genaipro, start_regenerate_all_genaipro
from pydantic import BaseModel

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


def _resolve_tts_api_key(provided: str, db: Session) -> str:
    """Return the Genaipro API key: use provided value, else DB settings, else .env."""
    if provided:
        return provided
    row = db.query(AppSetting).filter(AppSetting.key == "genaipro_api_key").first()
    if row and row.value:
        return row.value
    return app_settings.genaipro_api_key or ""


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

    project.status = ProjectStatus.queued
    project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(project)

    start_regenerate_script(project.id)
    return project


class EditScriptPayload(BaseModel):
    prompt: str


@router.post("/{project_id}/edit-script")
def edit_script(project_id: int, payload: EditScriptPayload, db: Session = Depends(get_db)):
    """Use Claude to revise the current script based on the user's instruction.
    Returns the revised script text without persisting it (user must approve)."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.status != ProjectStatus.awaiting_approval:
        raise HTTPException(status_code=400, detail="Project is not awaiting script approval")

    current = (project.script_final or project.script or "").strip()
    if not current:
        raise HTTPException(status_code=400, detail="No hay script disponible para editar")
    if not payload.prompt.strip():
        raise HTTPException(status_code=400, detail="El prompt no puede estar vacío")

    from ..services.claude_service import edit_script_with_prompt
    try:
        revised = edit_script_with_prompt(current, payload.prompt.strip())
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Error al editar script con Claude: {exc}")

    return {"script": revised}


@router.post("/{project_id}/resplit", response_model=ProjectOut)
def resplit_chunks(project_id: int, payload: ResplitPayload, db: Session = Depends(get_db)):
    raise HTTPException(status_code=501, detail="Resplit no disponible. Las escenas se dividen automaticamente con Claude + SRT.")


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

    api_key = _resolve_tts_api_key(payload.tts_api_key, db)
    try:
        provider = get_provider(payload.tts_provider or "genaipro", api_key, config)
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
    project.tts_provider = payload.tts_provider or "genaipro"
    project.tts_api_key  = _resolve_tts_api_key(payload.tts_api_key, db)
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
    return FileResponse(
        str(path), media_type="audio/mpeg", filename="audio-completo.mp3",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@router.post("/{project_id}/approve-audio", response_model=ProjectOut)
def approve_audio(project_id: int, db: Session = Depends(get_db)):
    """Mark the voiceover as approved. Does not start phase 3 yet."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.status != ProjectStatus.awaiting_audio_approval:
        raise HTTPException(status_code=400, detail="El proyecto no está esperando aprobación de audio")

    project.status = ProjectStatus.audio_approved
    project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(project)
    return project


@router.post("/{project_id}/recover-srt")
def recover_srt(project_id: int, db: Session = Depends(get_db)):
    """Re-download the SRT from GenAIPro by creating a new TTS task for the chunk text.

    Saves result to voiceover/subtitles.srt.
    Returns {saved: bool, path: str, bytes: int, subtitle_url: str}.
    """
    import json as _json, requests as _req
    from pathlib import Path as _P
    from ..services.pipeline_service import voiceover_dir

    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not project.tts_provider or project.tts_provider != "genaipro":
        raise HTTPException(status_code=400, detail="Solo soportado para proyectos con GenAIPro TTS")
    if not project.tts_api_key:
        raise HTTPException(status_code=400, detail="API key de GenAIPro no configurada")

    # Get all chunk texts concatenated
    chunks = (
        db.query(Chunk)
        .filter(Chunk.project_id == project_id)
        .order_by(Chunk.chunk_number)
        .all()
    )
    text = " ".join(c.scene_text or "" for c in chunks).strip()
    if not text:
        raise HTTPException(status_code=400, detail="No hay texto en los chunks")

    tts_config = _json.loads(project.tts_config or "{}")
    voice_id   = project.tts_voice_id or tts_config.get("voice_id", "")
    if not voice_id:
        raise HTTPException(status_code=400, detail="voice_id no configurado")

    api_key  = project.tts_api_key
    base_url = "https://genaipro.vn/api/v1"
    headers  = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    # Create a new TTS task
    payload = {
        "input":      text,
        "voice_id":   voice_id,
        "model_id":   tts_config.get("model_id", "eleven_multilingual_v2"),
        "speed":      float(tts_config.get("speed", 1.0)),
        "stability":  float(tts_config.get("stability", 0.5)),
        "similarity": float(tts_config.get("similarity", 0.75)),
        "style":      float(tts_config.get("style", 0.0)),
    }
    resp = _req.post(f"{base_url}/labs/task", headers=headers, json=payload, timeout=60)
    if not resp.ok:
        raise HTTPException(status_code=502, detail=f"GenAIPro error: {resp.text[:300]}")
    task_id = resp.json().get("task_id") or resp.json().get("id")
    if not task_id:
        raise HTTPException(status_code=502, detail=f"No task_id en respuesta: {resp.json()}")

    # Poll until completed
    import time as _time
    deadline = _time.time() + 600
    subtitle_url = None
    full_data = {}
    while _time.time() < deadline:
        pr = _req.get(f"{base_url}/labs/task/{task_id}", headers={"Authorization": f"Bearer {api_key}"}, timeout=30)
        pr.raise_for_status()
        full_data = pr.json()
        status = full_data.get("status", "").lower()
        if status == "completed":
            subtitle_url = (
                full_data.get("subtitle")
                or full_data.get("subtitle_url")
                or full_data.get("srt")
                or full_data.get("srt_url")
            )
            break
        if status in ("failed", "error", "cancelled"):
            raise HTTPException(status_code=502, detail=f"Task falló: {full_data}")
        _time.sleep(5)
    else:
        raise HTTPException(status_code=504, detail="Timeout esperando GenAIPro")

    if not subtitle_url:
        return {
            "saved": False,
            "path": None,
            "bytes": 0,
            "subtitle_url": None,
            "response_keys": list(full_data.keys()),
            "message": "GenAIPro no retornó URL de subtitulos en esta respuesta",
        }

    # Download SRT
    srt_resp = _req.get(subtitle_url, timeout=60)
    srt_resp.raise_for_status()
    srt_bytes = srt_resp.content

    vo = voiceover_dir(project.slug)
    vo.mkdir(parents=True, exist_ok=True)
    srt_path = vo / "subtitles.srt"
    srt_path.write_bytes(srt_bytes)

    return {
        "saved": True,
        "path": str(srt_path),
        "bytes": len(srt_bytes),
        "subtitle_url": subtitle_url,
        "message": f"SRT descargado y guardado: {len(srt_bytes)} bytes",
    }


@router.post("/{project_id}/create-scenes-from-srt", response_model=ProjectOut)
def create_scenes_from_srt(project_id: int, db: Session = Depends(get_db)):
    """Parse global SRT → create scene chunks → start video generation (phase 3)."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not project.voiceover_path:
        raise HTTPException(status_code=400, detail="No hay voiceover generado para este proyecto")

    project.status = ProjectStatus.queued
    project.error_message = None
    project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(project)

    start_create_scenes_from_srt(project.id)
    return project


@router.post("/{project_id}/reset-to-audio-approved", response_model=ProjectOut)
def reset_to_audio_approved(project_id: int, db: Session = Depends(get_db)):
    """Reset a stuck/errored project to audio_approved so the user can retry scene creation."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not project.voiceover_path:
        raise HTTPException(status_code=400, detail="No hay voiceover generado para este proyecto")

    # Clear all chunks so create-scenes-from-srt starts fresh
    db.query(Chunk).filter(Chunk.project_id == project_id).delete()

    project.status = ProjectStatus.audio_approved
    project.error_message = None
    project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(project)
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


@router.post("/{project_id}/generate-images", response_model=ProjectOut)
def generate_images(project_id: int, db: Session = Depends(get_db)):
    """Launch Google Imagen 4 Fast image generation for all scene chunks."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.status not in (ProjectStatus.scenes_ready, ProjectStatus.images_ready, ProjectStatus.error):
        raise HTTPException(status_code=400, detail="El proyecto no está en un estado válido para generar imágenes")

    project.status = ProjectStatus.queued
    project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(project)

    start_generate_images(project.id)
    return project


@router.get("/{project_id}/chunk/{chunk_number}/image")
def get_chunk_image(project_id: int, chunk_number: int, db: Session = Depends(get_db)):
    """Serve the generated image for a specific scene chunk."""
    chunk = db.query(Chunk).filter(
        Chunk.project_id == project_id,
        Chunk.chunk_number == chunk_number,
    ).first()
    if not chunk:
        raise HTTPException(status_code=404, detail="Chunk no encontrado")
    if not chunk.image_path:
        raise HTTPException(status_code=404, detail="No hay imagen generada para esta escena")
    img_path = Path(chunk.image_path)
    if not img_path.exists():
        raise HTTPException(status_code=404, detail=f"Archivo de imagen no encontrado: {chunk.image_path}")
    # Detect mime type from extension
    suffix = img_path.suffix.lower()
    media_types = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
    media_type = media_types.get(suffix, "image/jpeg")
    return FileResponse(str(img_path), media_type=media_type)


@router.get("/{project_id}/chunk/{chunk_number}/video")
def get_chunk_video(project_id: int, chunk_number: int, db: Session = Depends(get_db)):
    """Serve the generated video for a specific scene chunk."""
    chunk = db.query(Chunk).filter(
        Chunk.project_id == project_id,
        Chunk.chunk_number == chunk_number,
    ).first()
    if not chunk:
        raise HTTPException(status_code=404, detail="Chunk no encontrado")
    if not chunk.video_path:
        raise HTTPException(status_code=404, detail="No hay video generado para esta escena")
    vid_path = Path(chunk.video_path)
    if not vid_path.exists():
        raise HTTPException(status_code=404, detail=f"Archivo de video no encontrado: {chunk.video_path}")
    return FileResponse(str(vid_path), media_type="video/mp4")


@router.post("/{project_id}/retry-chunk-image/{chunk_number}", response_model=ProjectOut)
def retry_chunk_image(project_id: int, chunk_number: int, db: Session = Depends(get_db)):
    """Re-generate the image for a single scene chunk using Google Imagen 4 Fast."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.status not in (ProjectStatus.images_ready, ProjectStatus.generating_images, ProjectStatus.scenes_ready):
        raise HTTPException(status_code=400, detail="El proyecto debe estar en estado images_ready, generating_images o scenes_ready")

    chunk = db.query(Chunk).filter(Chunk.project_id == project_id, Chunk.chunk_number == chunk_number).first()
    if not chunk:
        raise HTTPException(status_code=404, detail=f"Chunk {chunk_number} no encontrado")

    start_retry_chunk_image(project.id, chunk_number)
    return project

class MotionPromptUpdate(BaseModel):
    motion_prompt: str

@router.put("/{project_id}/chunk/{chunk_number}/motion-prompt", response_model=ProjectOut)
def update_chunk_motion_prompt(project_id: int, chunk_number: int, payload: MotionPromptUpdate, db: Session = Depends(get_db)):
    """Manually update the motion prompt for a specific chunk."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    chunk = db.query(Chunk).filter(Chunk.project_id == project_id, Chunk.chunk_number == chunk_number).first()
    if not chunk:
        raise HTTPException(status_code=404, detail=f"Chunk {chunk_number} no encontrado")

    chunk.motion_prompt = payload.motion_prompt
    chunk.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(project)
    return project

@router.post("/{project_id}/generate-motion-prompts", response_model=ProjectOut)
def generate_motion_prompts_manually(project_id: int, db: Session = Depends(get_db)):
    """Trigger the motion prompts generation step manually for all chunks."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
        
    start_generate_motion_prompts(project.id)
    return project

@router.post("/{project_id}/start-animation", response_model=ProjectOut)
def start_animation(project_id: int, db: Session = Depends(get_db)):
    """Trigger the mass animation phase using Meta AI."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    start_animate_scenes(project.id)
    return project


@router.post("/{project_id}/scenes/{chunk_number}/regenerate-genaipro", response_model=ProjectOut)
def regenerate_scene_image_genaipro(project_id: int, chunk_number: int, db: Session = Depends(get_db)):
    """Re-generate the image for one scene using Genaipro Veo (uses existing image_prompt)."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    chunk = (
        db.query(Chunk)
        .filter(Chunk.project_id == project_id, Chunk.chunk_number == chunk_number)
        .first()
    )
    if not chunk:
        raise HTTPException(status_code=404, detail=f"Chunk {chunk_number} no encontrado")
    if not chunk.image_prompt:
        raise HTTPException(
            status_code=400,
            detail="Esta escena no tiene image_prompt guardado. Genera las imágenes completas primero."
        )

    start_regenerate_image_genaipro(project_id, chunk_number)
    return project


@router.post("/{project_id}/regenerate-all-genaipro", response_model=ProjectOut)
def regenerate_all_images_genaipro(project_id: int, db: Session = Depends(get_db)):
    """Re-generate images for ALL scenes that have an image_prompt using Genaipro Veo."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    start_regenerate_all_genaipro(project_id)
    return project


# ── Reference Images (character + style) ──────────────────────────────────

@router.post("/{project_id}/reference-character", response_model=ProjectOut)
async def upload_reference_character(
    project_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload a character reference image for kontext consistency."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    proj_dir = PROJECTS_PATH / project.slug
    proj_dir.mkdir(parents=True, exist_ok=True)
    ref_path = proj_dir / "reference_character.jpg"

    content = await file.read()
    ref_path.write_bytes(content)

    project.reference_character_path = str(ref_path)
    project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(project)
    return project


@router.delete("/{project_id}/reference-character", response_model=ProjectOut)
def delete_reference_character(project_id: int, db: Session = Depends(get_db)):
    """Remove the character reference image."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if project.reference_character_path:
        ref = Path(project.reference_character_path)
        if ref.exists():
            ref.unlink()

    project.reference_character_path = None
    project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(project)
    return project


@router.get("/{project_id}/reference-character")
def get_reference_character(project_id: int, db: Session = Depends(get_db)):
    """Serve the character reference image."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project or not project.reference_character_path:
        raise HTTPException(status_code=404, detail="No hay imagen de personaje")
    ref = Path(project.reference_character_path)
    if not ref.exists():
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    return FileResponse(str(ref), media_type="image/jpeg")


@router.post("/{project_id}/reference-style", response_model=ProjectOut)
async def upload_reference_style(
    project_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload a style reference image for kontext consistency."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    proj_dir = PROJECTS_PATH / project.slug
    proj_dir.mkdir(parents=True, exist_ok=True)
    ref_path = proj_dir / "reference_style.jpg"

    content = await file.read()
    ref_path.write_bytes(content)

    project.reference_style_path = str(ref_path)
    project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(project)
    return project


@router.delete("/{project_id}/reference-style", response_model=ProjectOut)
def delete_reference_style(project_id: int, db: Session = Depends(get_db)):
    """Remove the style reference image."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if project.reference_style_path:
        ref = Path(project.reference_style_path)
        if ref.exists():
            ref.unlink()

    project.reference_style_path = None
    project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(project)
    return project


@router.get("/{project_id}/reference-style")
def get_reference_style(project_id: int, db: Session = Depends(get_db)):
    """Serve the style reference image."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project or not project.reference_style_path:
        raise HTTPException(status_code=404, detail="No hay imagen de estilo")
    ref = Path(project.reference_style_path)
    if not ref.exists():
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    return FileResponse(str(ref), media_type="image/jpeg")

