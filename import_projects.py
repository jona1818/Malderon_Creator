"""Import existing project folders into the database."""
import os
from pathlib import Path
from datetime import datetime
from app.database import SessionLocal
from app.models import Project, Chunk, ProjectStatus, ChunkStatus, VideoMode

PROJECTS_DIR = Path("./projects")

def slug_to_title(slug: str) -> str:
    return slug.replace("-", " ").title()

db = SessionLocal()
try:
    for project_dir in sorted(PROJECTS_DIR.iterdir()):
        if not project_dir.is_dir():
            continue
        slug = project_dir.name

        # Skip if already in DB
        existing = db.query(Project).filter(Project.slug == slug).first()
        if existing:
            print(f"[SKIP] {slug} already in DB")
            continue

        title = slug_to_title(slug)
        voiceover_mp3 = project_dir / "voiceover" / "audio-completo.mp3"
        has_voiceover = voiceover_mp3.exists()
        chunk_dirs = sorted([d for d in project_dir.iterdir() if d.is_dir() and d.name.startswith("chunk_")],
                            key=lambda d: int(d.name.split("_")[1]))
        has_chunks = len(chunk_dirs) > 0

        # Determine status
        if has_chunks:
            status = ProjectStatus.images_ready
        elif has_voiceover:
            status = ProjectStatus.awaiting_audio_approval
        else:
            status = ProjectStatus.queued

        # Reference images
        ref_char = project_dir / "reference_character.jpg"
        ref_style = project_dir / "reference_style.jpg"

        project = Project(
            title=title,
            slug=slug,
            mode=VideoMode.animated,
            status=status,
            voiceover_path=str(voiceover_mp3) if has_voiceover else None,
            reference_character_path=str(ref_char) if ref_char.exists() else None,
            reference_style_path=str(ref_style) if ref_style.exists() else None,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(project)
        db.flush()  # get project.id

        # Import chunks
        for chunk_dir in chunk_dirs:
            chunk_num = int(chunk_dir.name.split("_")[1])
            images_dir = chunk_dir / "images"
            image_path = None
            if images_dir.exists():
                imgs = list(images_dir.glob("*.jpg")) + list(images_dir.glob("*.png")) + list(images_dir.glob("*.webp"))
                if imgs:
                    image_path = str(sorted(imgs)[0])

            audio_path = project_dir / "voiceover" / f"audio-chunk-{chunk_num}.mp3"

            chunk = Chunk(
                project_id=project.id,
                chunk_number=chunk_num,
                status=ChunkStatus.done if image_path else ChunkStatus.pending,
                image_path=image_path,
                audio_path=str(audio_path) if audio_path.exists() else None,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            db.add(chunk)

        db.commit()
        print(f"[OK] {slug} | status={status.value} | chunks={len(chunk_dirs)}")

    print("\nDone!")
finally:
    db.close()
