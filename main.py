"""YouTube Video Creator – FastAPI entry point."""
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.database import init_db
from app.routers import projects, logs, workers
from app.routers import youtube
from app.routers import tts as tts_router

# ── App setup ─────────────────────────────────────────────────────────────────
app = FastAPI(
    title="YouTube Video Creator",
    description="Automated YouTube video generation with AI",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Static files & templates ──────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# Serve project output files (videos, images) at /media/
from app.config import PROJECTS_PATH
app.mount("/media", StaticFiles(directory=PROJECTS_PATH, html=False), name="media")

templates = Jinja2Templates(directory=BASE_DIR / "templates")

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(projects.router)
app.include_router(logs.router)
app.include_router(workers.router)
app.include_router(youtube.router)
app.include_router(tts_router.router)


# ── Frontend routes ───────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ── Startup ───────────────────────────────────────────────────────────────────
@app.on_event("startup")
def startup():
    init_db()
    print("[OK] Database initialised")
    print("[OK] YouTube Video Creator running at http://localhost:8000")
