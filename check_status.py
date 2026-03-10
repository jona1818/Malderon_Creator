from app.database import SessionLocal
from app.models import Chunk

db = SessionLocal()
total = db.query(Chunk).filter(Chunk.project_id == 4).count()
with_mp = db.query(Chunk).filter(Chunk.project_id == 4, Chunk.motion_prompt.isnot(None), Chunk.motion_prompt != "").count()
with_vid = db.query(Chunk).filter(Chunk.project_id == 4, Chunk.video_path.isnot(None), Chunk.video_path != "").count()
print(f"Total: {total} | Con motion_prompt: {with_mp} | Con video: {with_vid}")
db.close()
