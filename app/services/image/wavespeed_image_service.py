"""
WaveSpeed.ai image generation service.

Models:
  - wavespeed-ai/flux-kontext-dev/multi: When reference image(s) exist —
    supports image input for character/style consistency (~$0.025/image).
  - flux-dev-ultra-fast: Default fast model when no reference images (~$0.005/image).

Flow:
  1. Encode reference images as base64 data URIs (if any).
  2. POST to /api/v3/{model_path} → get request_id.
  3. Poll GET /api/v3/predictions/{request_id}/result until completed.
  4. Download the output image to output_path.
"""
from __future__ import annotations

import base64
import time
from pathlib import Path

import requests

BASE_URL = "https://api.wavespeed.ai/api/v3"
MODEL_KONTEXT = "wavespeed-ai/flux-kontext-dev/multi"  # with reference images
MODEL_FAST = "wavespeed-ai/flux-dev-ultra-fast"           # without reference images

POLL_INTERVAL = 5       # seconds between status checks
POLL_TIMEOUT = 300      # max seconds to wait for completion


def _image_to_data_uri(image_path: Path) -> str:
    """Read an image file and return a base64 data URI."""
    img_bytes = image_path.read_bytes()
    suffix = image_path.suffix.lower()
    mime = "image/jpeg" if suffix in (".jpg", ".jpeg") else "image/png"
    b64 = base64.b64encode(img_bytes).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _resolve_ref(path: str | Path | None) -> Path | None:
    """Return a Path if the file exists, else None."""
    if not path:
        return None
    p = Path(path)
    return p if p.exists() else None


def generate_image(
    prompt: str,
    output_path: str | Path,
    api_key: str = "",
    reference_character_path: str | Path | None = None,
    reference_style_path: str | Path | None = None,
    width: int = 1920,
    height: int = 1080,
) -> Path:
    """Generate an image with WaveSpeed and save to disk.

    When reference images are provided, uses flux-kontext-dev/multi.
    Otherwise uses flux-dev-ultra-fast.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not api_key:
        raise ValueError("WaveSpeed API key is required")

    char_ref = _resolve_ref(reference_character_path)
    style_ref = _resolve_ref(reference_style_path)
    has_refs = char_ref is not None or style_ref is not None

    # Build prompt with reference instructions
    parts = []
    if char_ref and style_ref:
        parts.append(
            "Maintain the exact same character appearance from the reference image "
            "and use a consistent visual style and color palette."
        )
    elif char_ref:
        parts.append("Maintain the exact same character appearance as the reference image.")
    elif style_ref:
        parts.append("Maintain the exact same visual style and color palette as the reference image.")
    parts.append(prompt)
    full_prompt = " ".join(parts)

    # Model and payload
    if has_refs:
        model_path = MODEL_KONTEXT
        images = []
        if char_ref:
            images.append(_image_to_data_uri(char_ref))
        if style_ref:
            images.append(_image_to_data_uri(style_ref))
        payload = {
            "prompt": full_prompt,
            "images": images,
            "num_images": 1,
        }
        print(f"[WaveSpeed img] kontext-dev/multi con {len(images)} ref(s) — prompt: {full_prompt[:120]}...")
    else:
        model_path = MODEL_FAST
        payload = {
            "prompt": full_prompt,
            "num_images": 1,
        }
        print(f"[WaveSpeed img] flux-dev-ultra-fast — prompt: {full_prompt[:120]}...")

    # Submit job
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    resp = requests.post(
        f"{BASE_URL}/{model_path}",
        headers=headers,
        json=payload,
        timeout=120,
    )

    if resp.status_code != 200:
        raise RuntimeError(
            f"WaveSpeed image submit error {resp.status_code}: {resp.text[:500]}"
        )

    data = resp.json()
    request_id = data.get("data", {}).get("id") or data.get("id") or data.get("request_id")
    if not request_id:
        raise RuntimeError(f"WaveSpeed image: no request_id in response: {data}")

    print(f"[WaveSpeed img] Job submitted: {request_id}")

    # Poll for result
    poll_url = f"{BASE_URL}/predictions/{request_id}/result"
    t0 = time.time()
    status = "processing"

    while time.time() - t0 < POLL_TIMEOUT:
        time.sleep(POLL_INTERVAL)

        poll_resp = requests.get(poll_url, headers=headers, timeout=30)
        if poll_resp.status_code != 200:
            print(f"[WaveSpeed img] Poll error {poll_resp.status_code}: {poll_resp.text[:200]}")
            continue

        result = poll_resp.json()
        status = result.get("data", {}).get("status") or result.get("status") or "unknown"
        elapsed = time.time() - t0

        if status == "completed":
            # Extract output URL — WaveSpeed returns outputs as a list
            output_url = (
                result.get("data", {}).get("outputs")
                or result.get("data", {}).get("output")
                or result.get("output")
            )
            if isinstance(output_url, list) and output_url:
                output_url = output_url[0]
            if not output_url or not isinstance(output_url, str):
                raise RuntimeError(f"WaveSpeed image: no image URL in completed result: {result}")

            print(f"[WaveSpeed img] Completed in {elapsed:.0f}s. Downloading image...")

            # Download image
            img_resp = requests.get(output_url, timeout=120)
            img_resp.raise_for_status()
            output_path.write_bytes(img_resp.content)
            size_kb = len(img_resp.content) // 1024
            print(f"[WaveSpeed img] Saved: {output_path.name} ({size_kb} KB)")
            return output_path

        elif status in ("failed", "error", "cancelled"):
            error_msg = result.get("data", {}).get("error") or result.get("error") or "unknown error"
            raise RuntimeError(f"WaveSpeed image job failed: {error_msg}")

        else:
            if int(elapsed) % 30 == 0 or elapsed < 10:
                print(f"[WaveSpeed img] Status: {status} ({elapsed:.0f}s elapsed)")

    raise RuntimeError(
        f"WaveSpeed image: timeout after {POLL_TIMEOUT}s (last status: {status})"
    )
