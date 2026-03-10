"""Sync video files on disk to database for the Vatican project."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.database import SessionLocal
from app.models import Chunk, ChunkStatus
from datetime import datetime

PROJECT_ID = 4  # el-vaticano
SLUG = "el-vaticano-oculto-esto-durante-anos-la-verdad-del-cadaver-d"
PROJECTS_DIR = Path(__file__).parent / "projects" / SLUG

db = SessionLocal()
try:
    chunks = db.query(Chunk).filter(Chunk.project_id == PROJECT_ID).all()
    updated = 0
    for chunk in chunks:
        n = chunk.chunk_number
        video_file = PROJECTS_DIR / f"chunk_{n}" / "videos" / f"video_{n}.mp4"
        if video_file.exists() and video_file.stat().st_size > 0:
            if not chunk.video_path:
                chunk.video_path = str(video_file)
                chunk.status = ChunkStatus.done
                chunk.error_message = None
                chunk.updated_at = datetime.utcnow()
                updated += 1
                print(f"  Chunk #{n}: video guardado ({video_file.stat().st_size // 1024} KB)")
    db.commit()

    total = len(chunks)
    with_video = db.query(Chunk).filter(
        Chunk.project_id == PROJECT_ID,
        Chunk.video_path != None,
        Chunk.video_path != ""
    ).count()
    print(f"\nResultado: {updated} videos nuevos sincronizados")
    print(f"Total: {total} escenas | Con video: {with_video} | Sin video: {total - with_video}")
finally:
    db.close()
