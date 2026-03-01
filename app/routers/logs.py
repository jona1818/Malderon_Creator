"""Log endpoints – polling + SSE streaming."""
import asyncio
import json
from typing import List, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ..database import get_db, SessionLocal
from ..models import Log
from ..schemas import LogOut

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("/{project_id}", response_model=List[LogOut])
def get_logs(
    project_id: int,
    since_id: Optional[int] = None,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    """Return recent logs for a project. Use `since_id` for incremental polling."""
    q = db.query(Log).filter(Log.project_id == project_id)
    if since_id is not None:
        q = q.filter(Log.id > since_id)
    logs = q.order_by(Log.id.asc()).limit(limit).all()
    return logs


@router.get("/{project_id}/stream")
async def stream_logs(project_id: int, request: Request):
    """
    SSE endpoint – streams new log lines as they appear.
    Frontend connects once and receives events until the connection closes.
    """
    async def event_generator():
        last_id = 0
        consecutive_empty = 0
        while True:
            if await request.is_disconnected():
                break

            # Query DB synchronously (SQLite with WAL is safe for this)
            db = SessionLocal()
            try:
                logs = (
                    db.query(Log)
                    .filter(Log.project_id == project_id, Log.id > last_id)
                    .order_by(Log.id.asc())
                    .limit(50)
                    .all()
                )
            finally:
                db.close()

            for log in logs:
                last_id = log.id
                data = json.dumps(
                    {
                        "id": log.id,
                        "level": log.level,
                        "stage": log.stage,
                        "message": log.message,
                        "timestamp": log.timestamp.isoformat(),
                    }
                )
                yield f"data: {data}\n\n"
                consecutive_empty = 0

            if not logs:
                consecutive_empty += 1
                # Stop streaming after project is done (no new logs for ~30s)
                if consecutive_empty > 60:
                    break
                yield ": heartbeat\n\n"

            await asyncio.sleep(0.5)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
