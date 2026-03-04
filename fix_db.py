from app.database import SessionLocal
from app.models import Project, Chunk, ChunkStatus, ProjectStatus
import os

db = SessionLocal()
try:
    # Get the most recent project
    project = db.query(Project).order_by(Project.created_at.desc()).first()
    if project:
        print(f"Fixing project: {project.title} (id={project.id})")
        
        # Update project status
        project.status = ProjectStatus.images_ready
        project.error_message = None
        
        # Update chunks that have an image path but are in error status
        chunks = db.query(Chunk).filter(Chunk.project_id == project.id).all()
        for c in chunks:
            if c.image_path and os.path.exists(c.image_path):
                if c.status == ChunkStatus.error:
                    print(f"  Fixing chunk {c.chunk_number}")
                    c.status = ChunkStatus.done
                    c.error_message = None
        
        db.commit()
        print("Done!")
    else:
        print("No project found.")
finally:
    db.close()
