"""
WaveSpeed.ai image-to-video animation service.

Model: wan-2.2/i2v-5b-720p (image-to-video, 5s clips, 720p)

Flow:
  1. Read source image and encode as base64 data URI.
  2. POST to /api/v3/wavespeed-ai/wan-2.2/i2v-5b-720p  → get request_id.
  3. Poll GET /api/v3/predictions/{request_id}/result until completed.
  4. Download the output video to output_path.
"""
from __future__ import annotations

import base64
import time
from pathlib import Path

import requests

BASE_URL = "https://api.wavespeed.ai/api/v3"
MODEL_PATH = "wavespeed-ai/wan-2.2/i2v-5b-720p"

# Polling config
POLL_INTERVAL = 5       # seconds between status checks
POLL_TIMEOUT = 600      # max seconds to wait for completion


def _image_to_data_uri(image_path: Path) -> str:
    """Read an image file and return a base64 data URI."""
    img_bytes = image_path.read_bytes()
    suffix = image_path.suffix.lower()
    mime = "image/jpeg" if suffix in (".jpg", ".jpeg") else "image/png"
    b64 = base64.b64encode(img_bytes).decode("ascii")
    return f"data:{mime};base64,{b64}"


def animate_image(
    image_path: str | Path,
    output_path: str | Path,
    prompt: str = "Slow cinematic zoom in, subtle camera movement",
    api_key: str = "",
    duration: int = 5,
) -> Path:
    """Animate a still image into a short video clip using WaveSpeed.

    Parameters
    ----------
    image_path : path to the source image (jpg/png)
    output_path : where to save the generated .mp4
    prompt : motion/animation description
    api_key : WaveSpeed API key (required)
    duration : video duration in seconds (default: 5)

    Returns
    -------
    Path to the saved video file.
    """
    image_path = Path(image_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not image_path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")
    if not api_key:
        raise ValueError("WaveSpeed API key is required")

    # Step 1: encode image as data URI
    image_data_uri = _image_to_data_uri(image_path)
    print(
        f"[WaveSpeed] Submitting i2v job: "
        f"image={image_path.name} ({image_path.stat().st_size // 1024} KB) "
        f"duration={duration}s"
    )

    # Step 2: submit job
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "image": image_data_uri,
        "prompt": prompt,
        "duration": duration,
    }

    resp = requests.post(
        f"{BASE_URL}/{MODEL_PATH}",
        headers=headers,
        json=payload,
        timeout=60,
    )

    if resp.status_code != 200:
        raise RuntimeError(
            f"WaveSpeed submit error {resp.status_code}: {resp.text[:500]}"
        )

    data = resp.json()
    request_id = data.get("data", {}).get("id") or data.get("id") or data.get("request_id")
    if not request_id:
        raise RuntimeError(f"WaveSpeed: no request_id in response: {data}")

    print(f"[WaveSpeed] Job submitted: {request_id}")

    # Step 3: poll for result
    poll_url = f"{BASE_URL}/predictions/{request_id}/result"
    t0 = time.time()
    status = "processing"

    while time.time() - t0 < POLL_TIMEOUT:
        time.sleep(POLL_INTERVAL)

        poll_resp = requests.get(poll_url, headers=headers, timeout=30)
        if poll_resp.status_code != 200:
            print(f"[WaveSpeed] Poll error {poll_resp.status_code}: {poll_resp.text[:200]}")
            continue

        result = poll_resp.json()
        status = result.get("data", {}).get("status") or result.get("status") or "unknown"
        elapsed = time.time() - t0

        if status == "completed":
            # Extract output URL
            output_url = (
                result.get("data", {}).get("outputs")
                or result.get("data", {}).get("output", {}).get("video")
                or result.get("data", {}).get("output")
                or result.get("output")
            )
            # Handle list output
            if isinstance(output_url, list) and output_url:
                output_url = output_url[0]
            if not output_url or not isinstance(output_url, str):
                raise RuntimeError(f"WaveSpeed: no video URL in completed result: {result}")

            print(f"[WaveSpeed] Completed in {elapsed:.0f}s. Downloading video...")

            # Step 4: download video
            vid_resp = requests.get(output_url, timeout=120)
            vid_resp.raise_for_status()
            output_path.write_bytes(vid_resp.content)
            size_kb = len(vid_resp.content) // 1024
            print(f"[WaveSpeed] Saved: {output_path.name} ({size_kb} KB)")
            return output_path

        elif status in ("failed", "error", "cancelled"):
            error_msg = result.get("data", {}).get("error") or result.get("error") or "unknown error"
            raise RuntimeError(f"WaveSpeed job failed: {error_msg}")

        else:
            if int(elapsed) % 30 == 0 or elapsed < 10:
                print(f"[WaveSpeed] Status: {status} ({elapsed:.0f}s elapsed)")

    raise RuntimeError(
        f"WaveSpeed: timeout after {POLL_TIMEOUT}s (last status: {status})"
    )
