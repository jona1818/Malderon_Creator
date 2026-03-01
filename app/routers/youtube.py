"""YouTube transcript extraction endpoint."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services.youtube_service import get_transcript

router = APIRouter(prefix="/api/youtube", tags=["youtube"])


class TranscriptRequest(BaseModel):
    url: str


@router.post("/transcript")
def fetch_transcript(payload: TranscriptRequest):
    try:
        result = get_transcript(payload.url)
        return result
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching transcript: {e}")
