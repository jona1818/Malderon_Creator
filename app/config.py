from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    anthropic_api_key: str = ""
    openrouter_api_key: str = ""
    openai_api_key: str = ""
    pexels_api_key: str = ""
    pixabay_api_key: str = ""
    nca_toolkit_url: str = "http://localhost:8090"
    nca_api_key: str = ""
    google_api_key: str = ""
    genaipro_api_key: str = ""   # Used for TTS and video animation
    pollinations_api_key: str = ""  # Free image generation via Pollinations.ai
    wavespeed_api_key: str = ""    # WaveSpeed.ai images + animation
    image_provider: str = "pollinations"  # "pollinations" or "wavespeed"
    max_workers: int = 3
    projects_dir: str = "./projects"
    database_url: str = "sqlite:///./videocreator.db"

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
PROJECTS_PATH = Path(settings.projects_dir)
PROJECTS_PATH.mkdir(exist_ok=True)
