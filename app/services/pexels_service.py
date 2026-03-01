"""Pexels API – search and download stock videos and photos."""
import requests
from pathlib import Path
from typing import Optional
from ..config import settings

BASE_URL = "https://api.pexels.com"
HEADERS = {"Authorization": settings.pexels_api_key}


def search_video(query: str, per_page: int = 5) -> Optional[str]:
    """Search for a stock video; returns the first HD download URL or None."""
    resp = requests.get(
        f"{BASE_URL}/videos/search",
        headers=HEADERS,
        params={"query": query, "per_page": per_page, "orientation": "landscape"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    videos = data.get("videos", [])
    if not videos:
        return None

    # Pick the best HD or SD file
    for video in videos:
        files = video.get("video_files", [])
        # Prefer 1280x720
        for vf in sorted(files, key=lambda f: f.get("width", 0), reverse=True):
            if vf.get("width", 0) <= 1920 and vf.get("file_type") == "video/mp4":
                return vf["link"]

    return None


def search_photo(query: str, per_page: int = 5) -> Optional[str]:
    """Search for a stock photo; returns the large image URL or None."""
    resp = requests.get(
        f"{BASE_URL}/v1/search",
        headers=HEADERS,
        params={"query": query, "per_page": per_page, "orientation": "landscape"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    photos = data.get("photos", [])
    if not photos:
        return None
    return photos[0]["src"]["large2x"]


def download_media(url: str, destination: Path) -> Path:
    """Download any media URL to disk."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    resp = requests.get(url, timeout=60, stream=True)
    resp.raise_for_status()
    with open(destination, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
    return destination
