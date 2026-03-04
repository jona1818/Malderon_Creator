"""
Replicate REST API service – image generation (SeedDream 4.5) + video animation (LTX Video).

Uses the Replicate HTTP API directly (no SDK) to avoid pydantic-v1 incompatibility
with Python 3.14.

Docs: https://replicate.com/docs/reference/http
"""
import time
import base64
import requests
from pathlib import Path
from ..config import settings

_BASE = "https://api.replicate.com/v1"


def _headers(api_key: str = "") -> dict:
    token = api_key or settings.replicate_api_token
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Prefer": "wait",          # ask Replicate to wait up to 60 s before returning
    }


def _run_model(model_id: str, input_payload: dict, max_wait: int = 600, api_key: str = "") -> list:
    """
    Call the Replicate predictions API, poll until done, return output list.

    `model_id` examples:
      "bytedance/seedream-4.5"
      "lightricks/ltx-video"
    """
    max_retries = 3
    retry_delay = 5  # seconds
    
    for attempt in range(max_retries):
        resp = requests.post(
            f"{_BASE}/models/{model_id}/predictions",
            json={"input": input_payload},
            headers=_headers(api_key),
            timeout=90,
        )
        if resp.status_code == 429:
            print(f"[Replicate] 429 - Too many requests. Attempt {attempt+1}/{max_retries}. Retrying in {retry_delay}s...")
            time.sleep(retry_delay)
            retry_delay *= 2  # exponential backoff
            continue
        resp.raise_for_status()
        prediction = resp.json()
        break
    else:
        # If we exhausted retries
        resp.raise_for_status()

    # If Prefer:wait worked we may already have a result
    status = prediction.get("status", "")
    if status == "succeeded":
        return _normalise_output(prediction.get("output"))

    # Otherwise poll
    poll_url = prediction.get("urls", {}).get("get") or f"{_BASE}/predictions/{prediction['id']}"
    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(3)
        r = requests.get(poll_url, headers=_headers(api_key), timeout=30)
        r.raise_for_status()
        p = r.json()
        status = p.get("status", "")
        if status == "succeeded":
            return _normalise_output(p.get("output"))
        if status in ("failed", "canceled"):
            raise RuntimeError(f"Replicate prediction {p['id']} {status}: {p.get('error')}")

    raise TimeoutError(f"Replicate prediction timed out after {max_wait}s")


def _normalise_output(output) -> list:
    """Always return a list of URLs."""
    if output is None:
        raise RuntimeError("Replicate returned no output")
    if isinstance(output, str):
        return [output]
    if isinstance(output, list):
        return [str(o) for o in output]
    return [str(output)]


# ── Image Generation ──────────────────────────────────────────────────────────

def generate_image(
    prompt: str,
    output_path: Path,
    api_key: str = "",
    width: int = 1920,
    height: int = 1080,
    steps: int = 30,
) -> Path:
    """Generate an image with ByteDance SeedDream 4.5 and save to disk."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"[Replicate Seedream] Generating image {width}x{height} for: {prompt[:80]}…")
    urls = _run_model(
        "bytedance/seedream-4.5",
        {
            "prompt": prompt,
            "width": width,
            "height": height,
            "size": "2K",
            "num_inference_steps": steps,
            "guidance_scale": 7.5,
        },
        api_key=api_key,
    )
    _download_file(urls[0], output_path)
    print(f"[Replicate Seedream] Image saved: {output_path}")
    return output_path


# ── Video Animation ───────────────────────────────────────────────────────────

def animate_image(
    image_path: Path,
    output_path: Path,
    prompt: str = "",
    duration_seconds: float = 5.0,
    api_key: str = "",
) -> Path:
    """Animate a still image into a short video clip using LTX Video."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Encode image as base64 data URI so we don't need to upload it separately
    image_path = Path(image_path)
    suffix = image_path.suffix.lstrip(".").lower() or "jpeg"
    mime = f"image/{suffix}"
    b64 = base64.b64encode(image_path.read_bytes()).decode()
    image_data_uri = f"data:{mime};base64,{b64}"

    urls = _run_model(
        "lightricks/ltx-video",
        {
            "image": image_data_uri,
            "prompt": prompt or "cinematic motion, smooth camera movement",
            "num_frames": max(25, int(duration_seconds * 25)),
            "guidance_scale": 3.0,
            "num_inference_steps": 30,
        },
        api_key=api_key
    )
    _download_file(urls[0], output_path)
    return output_path


# ── Helpers ───────────────────────────────────────────────────────────────────

def _download_file(url: str, destination: Path, timeout: int = 120) -> None:
    """Download a URL to a local file."""
    response = requests.get(url, timeout=timeout, stream=True)
    response.raise_for_status()
    with open(destination, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
