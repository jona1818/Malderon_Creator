"""NCA Toolkit API – render subtitle overlay onto video chunk + concatenate."""
import time
import requests
from pathlib import Path
from typing import List, Optional
from ..config import settings

NCA_URL = settings.nca_toolkit_url.rstrip("/")
NCA_KEY = settings.nca_api_key


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if NCA_KEY:
        h["x-api-key"] = NCA_KEY
    return h


# ── Render chunk (video + audio + SRT subtitles) ──────────────────────────────

def render_chunk(
    video_url_or_path: str,
    audio_url_or_path: str,
    srt_url_or_path: str,
    output_filename: str,
    subtitle_style: Optional[dict] = None,
) -> str:
    """
    Ask NCA Toolkit to combine video + audio + subtitles into one clip.
    Returns the URL of the rendered file on the NCA server.
    """
    payload = {
        "video_url": video_url_or_path,
        "audio_url": audio_url_or_path,
        "srt_url": srt_url_or_path,
        "output_filename": output_filename,
        "subtitle_style": subtitle_style or _default_subtitle_style(),
    }

    resp = requests.post(
        f"{NCA_URL}/v1/video/caption",
        json=payload,
        headers=_headers(),
        timeout=300,
    )
    resp.raise_for_status()
    data = resp.json()
    return _poll_job(data)


def concatenate_chunks(chunk_urls: List[str], output_filename: str) -> str:
    """
    Ask NCA Toolkit to concatenate multiple video clips into one final video.
    Returns the URL of the final video on the NCA server.
    """
    payload = {
        "video_urls": chunk_urls,
        "output_filename": output_filename,
    }

    resp = requests.post(
        f"{NCA_URL}/v1/video/concatenate",
        json=payload,
        headers=_headers(),
        timeout=300,
    )
    resp.raise_for_status()
    data = resp.json()
    return _poll_job(data)


def download_from_nca(nca_url: str, destination: Path) -> Path:
    """Download a file from the NCA Toolkit storage to local disk."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    resp = requests.get(nca_url, timeout=120, stream=True)
    resp.raise_for_status()
    with open(destination, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
    return destination


# ── Job polling ───────────────────────────────────────────────────────────────

def _poll_job(initial_response: dict, max_wait: int = 600) -> str:
    """
    NCA Toolkit jobs may be async. Poll until done and return the output URL.
    If the initial response already contains the URL, return it directly.
    """
    # Synchronous response
    if "output_url" in initial_response:
        return initial_response["output_url"]
    if "url" in initial_response:
        return initial_response["url"]

    job_id = initial_response.get("job_id") or initial_response.get("id")
    if not job_id:
        raise RuntimeError(f"NCA Toolkit: unexpected response: {initial_response}")

    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(5)
        resp = requests.get(
            f"{NCA_URL}/v1/jobs/{job_id}",
            headers=_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status", "").lower()
        if status in ("completed", "done", "success"):
            url = data.get("output_url") or data.get("url") or data.get("result")
            if url:
                return url
            raise RuntimeError(f"NCA job {job_id} completed but no URL returned: {data}")
        if status in ("failed", "error"):
            raise RuntimeError(f"NCA job {job_id} failed: {data.get('error', data)}")

    raise TimeoutError(f"NCA job {job_id} timed out after {max_wait}s")


def _default_subtitle_style() -> dict:
    return {
        "font_size": 24,
        "font_color": "white",
        "outline_color": "black",
        "outline_width": 2,
        "position": "bottom",
        "margin_v": 40,
    }
