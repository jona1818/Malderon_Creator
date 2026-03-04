"""Global application settings (API keys, defaults)."""
from typing import Optional
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import AppSetting
from ..schemas import SettingsPayload, SettingsOut

router = APIRouter(prefix="/api/settings", tags=["settings"])

# Keys that store API keys — their values are masked when returned to the client
_API_KEY_KEYS = {
    "anthropic_api_key",
    "genaipro_api_key",
    "replicate_api_key",
    "pexels_api_key",
    "pixabay_api_key",
}


def _mask(key: str, value: Optional[str]) -> str:
    """Return '••••••••' for API key fields (so the client knows one is saved)."""
    if key in _API_KEY_KEYS and value:
        return "••••••••"
    return value or ""


@router.get("/", response_model=SettingsOut)
def get_settings(db: Session = Depends(get_db)):
    rows = db.query(AppSetting).all()
    data = {row.key: _mask(row.key, row.value) for row in rows}
    return SettingsOut(data=data)


@router.post("/", response_model=SettingsOut)
def save_settings(payload: SettingsPayload, db: Session = Depends(get_db)):
    """Upsert all provided key-value pairs.
    Values equal to '••••••••' (the mask) are skipped so existing keys are preserved.
    """
    for key, value in payload.data.items():
        if value == "••••••••":
            # Client sent back the masked placeholder — don't overwrite the real key
            continue
        existing = db.query(AppSetting).filter(AppSetting.key == key).first()
        if existing:
            existing.value = value if value else None
        else:
            db.add(AppSetting(key=key, value=value if value else None))
    db.commit()

    rows = db.query(AppSetting).all()
    data = {row.key: _mask(row.key, row.value) for row in rows}
    return SettingsOut(data=data)


@router.get("/raw/{key}")
def get_raw_setting(key: str, db: Session = Depends(get_db)):
    """Internal endpoint: returns the real (unmasked) value of a single key.
    Used by the backend to retrieve API keys at runtime.
    NOTE: this endpoint is only called server-side (pipeline), not exposed to the UI.
    """
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    return {"key": key, "value": row.value if row else None}


@router.post("/meta-login")
async def trigger_meta_login():
    """Launch the browser automation to login to Meta AI manually."""
    import subprocess
    import sys
    import os
    from pathlib import Path

    # Run setup_meta_login in a separate process to avoid FastAPI/asyncio loop conflicts on Windows.
    script_content = """import asyncio
from app.services.video import meta_bot

if __name__ == '__main__':
    asyncio.run(meta_bot.setup_meta_login())
"""
    
    script_path = Path("run_meta_login.py")
    script_path.write_text(script_content, encoding="utf-8")
    
    # Launch in background (detached process if possible on Windows or just pipe stdout)
    CREATE_NO_WINDOW = 0x08000000
    subprocess.Popen(
        [sys.executable, str(script_path)],
        cwd=os.getcwd(),
        creationflags=CREATE_NO_WINDOW
    )

    return {"status": "started", "message": "Revisa la ventana del navegador que se acaba de abrir."}
