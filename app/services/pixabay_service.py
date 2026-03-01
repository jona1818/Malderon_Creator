"""Pixabay API – search and download stock videos and images."""
import requests
from pathlib import Path
from typing import Optional
from ..config import settings

BASE_URL = "https://pixabay.com/api"


def search_video(query: str, per_page: int = 5) -> Optional[str]:
    """Search for a stock video; returns the first download URL or None."""
    resp = requests.get(
        f"{BASE_URL}/videos/",
        params={
            "key": settings.pixabay_api_key,
            "q": query,
            "per_page": per_page,
            "video_type": "film",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    hits = data.get("hits", [])
    if not hits:
        return None

    # Prefer large > medium > small
    for hit in hits:
        videos = hit.get("videos", {})
        for size in ("large", "medium", "small"):
            url = videos.get(size, {}).get("url")
            if url:
                return url
    return None


def search_photo(query: str, per_page: int = 5) -> Optional[str]:
    """Search for a stock photo; returns the large image URL or None."""
    resp = requests.get(
        f"{BASE_URL}/",
        params={
            "key": settings.pixabay_api_key,
            "q": query,
            "per_page": per_page,
            "image_type": "photo",
            "orientation": "horizontal",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    hits = data.get("hits", [])
    if not hits:
        return None
    return hits[0].get("largeImageURL")


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
