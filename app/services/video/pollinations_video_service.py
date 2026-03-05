"""
Pollinations.ai video generation service (image-to-video).

Model: grok-video (FREE) via GET https://gen.pollinations.ai/video/{prompt}

Flow:
  1. Upload source image to catbox.moe to get a public URL.
  2. Call Pollinations /video/{prompt}?model=grok-video&image={url}
  3. Stream the response bytes into output_path.

The API blocks until the video is ready (can take 2-5 minutes).
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import quote

import requests

# ── catbox.moe upload cache (shared with image service) ─────────────────────
_upload_cache: dict[str, str] = {}


def _upload_to_catbox(image_path: Path) -> str:
    """Upload a local image to catbox.moe and return the public URL."""
    file_hash = hashlib.md5(image_path.read_bytes()).hexdigest()
    if file_hash in _upload_cache:
        return _upload_cache[file_hash]

    with open(str(image_path), "rb") as f:
        resp = requests.post(
            "https://catbox.moe/user/api.php",
            data={"reqtype": "fileupload"},
            files={"fileToUpload": (image_path.name, f, "image/jpeg")},
            timeout=60,
        )
    resp.raise_for_status()
    url = resp.text.strip()
    if not url.startswith("http"):
        raise RuntimeError(f"catbox.moe upload failed: {url[:200]}")

    _upload_cache[file_hash] = url
    print(f"[Pollinations Video] Image uploaded: {url}")
    return url


def animate_image(
    image_path: str | Path,
    output_path: str | Path,
    prompt: str = "Slow cinematic zoom in, subtle camera movement",
    api_key: str = "",
    model: str = "grok-video",
) -> Path:
    """Animate a still image into a short video clip using Pollinations.

    Parameters
    ----------
    image_path : path to the source image (jpg/png)
    output_path : where to save the generated .mp4
    prompt : motion/animation description
    api_key : Pollinations API key (optional, for priority queue)
    model : video model name (default: grok-video, free tier)

    Returns
    -------
    Path to the saved video file.
    """
    image_path = Path(image_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not image_path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")

    # Step 1: upload image to get a public URL
    image_url = _upload_to_catbox(image_path)

    # Step 2: call Pollinations video endpoint
    encoded_prompt = quote(prompt)
    url = f"https://gen.pollinations.ai/video/{encoded_prompt}"
    params: dict = {
        "model": model,
        "image": image_url,
    }
    if api_key:
        params["key"] = api_key

    print(
        f"[Pollinations Video] GET /video/... model={model} "
        f"image={image_path.name} ({image_path.stat().st_size // 1024} KB)"
    )

    resp = requests.get(url, params=params, timeout=600, stream=True)

    if resp.status_code == 402:
        raise RuntimeError(
            f"Pollinations {model}: requires paid balance (402). "
            "Try a different model or add credits."
        )

    if resp.status_code != 200:
        body = resp.text[:500]
        raise RuntimeError(
            f"Pollinations video error {resp.status_code}: {body}"
        )

    # Step 3: stream video bytes to disk
    total = 0
    with open(str(output_path), "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
            total += len(chunk)

    if total < 5000:
        raise RuntimeError(
            f"Pollinations {model}: response too small ({total} bytes). "
            f"Prompt: {prompt[:120]}"
        )

    print(
        f"[Pollinations Video] Saved: {output_path.name} "
        f"({total // 1024} KB)"
    )
    return output_path
