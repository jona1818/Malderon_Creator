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


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.replicate_api_token}",
        "Content-Type": "application/json",
        "Prefer": "wait",          # ask Replicate to wait up to 60 s before returning
    }


def _run_model(model_id: str, input_payload: dict, max_wait: int = 600) -> list:
    """
    Call the Replicate predictions API, poll until done, return output list.

    `model_id` examples:
      "bytedance/seedream-4.5"
      "lightricks/ltx-video"
    """
    resp = requests.post(
        f"{_BASE}/models/{model_id}/predictions",
        json={"input": input_payload},
        headers=_headers(),
        timeout=90,
    )
    resp.raise_for_status()
    prediction = resp.json()

    # If Prefer:wait worked we may already have a result
    status = prediction.get("status", "")
    if status == "succeeded":
        return _normalise_output(prediction.get("output"))

    # Otherwise poll
    poll_url = prediction.get("urls", {}).get("get") or f"{_BASE}/predictions/{prediction['id']}"
    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(4)
        r = requests.get(poll_url, headers=_headers(), timeout=30)
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
    width: int = 1280,
    height: int = 720,
    steps: int = 30,
) -> Path:
    """Generate an image with ByteDance SeedDream 4.5 and save to disk."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    urls = _run_model(
        "bytedance/seedream-4.5",
        {
            "prompt": prompt,
            "width": width,
            "height": height,
            "num_inference_steps": steps,
            "guidance_scale": 7.5,
        },
    )
    _download_file(urls[0], output_path)
    return output_path


# ── Video Animation ───────────────────────────────────────────────────────────

def animate_image(
    image_path: Path,
    output_path: Path,
    prompt: str = "",
    duration_seconds: float = 5.0,
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
