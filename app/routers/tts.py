"""TTS utility endpoints (voice listing, etc.)."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/tts", tags=["tts"])


class VoiceListRequest(BaseModel):
    tts_provider: str
    tts_api_key: str
    search: str = ""
    gender: str = ""
    language: str = ""


@router.post("/voices")
def list_voices(payload: VoiceListRequest):
    """Return available TTS voices for the given provider + API key."""
    if payload.tts_provider == "genaipro":
        from ..services.tts.genaipro import GenAIProTTS
        try:
            voices = GenAIProTTS.list_voices(
                payload.tts_api_key,
                search=payload.search,
                gender=payload.gender,
                language=payload.language,
            )
            return {"voices": voices}
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Error cargando voces: {exc}")

    raise HTTPException(
        status_code=501,
        detail=f"Listado de voces no soportado para el proveedor '{payload.tts_provider}'",
    )
