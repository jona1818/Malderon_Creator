"""
GenAIPro Veo — image and video generation via Server-Sent Events (SSE).

BASE URL : https://genaipro.vn/api/v1
AUTH     : Authorization: Bearer {api_key}   (same key as TTS)

IMPORTANT — request format:
  Both endpoints must use application/x-www-form-urlencoded (form-encoded),
  NOT application/json.  JSON bodies return 400 "Prompt is required".

Image generation
  POST /veo/create-image  (form-encoded)
  Fields: prompt, aspect_ratio=IMAGE_ASPECT_RATIO_LANDSCAPE, number_of_images=1
  Response: SSE stream with typed events:
    event:image_generation_status / data:generating
    event:image_generation_status / data:{"status":"finished","url":"..."}
    event:error                   / data:{"code":500,"error":"..."}

Video generation
  POST /veo/frames-to-video  (form-encoded)
  Fields: start_image=<base64 data URI>, prompt, aspect_ratio, number_of_videos=1
  Response: same SSE format

Credit errors:
  If the API returns "insufficient balance" / "not enough credits" the error
  is logged to console only — no balance amount is surfaced in the UI.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import requests

BASE_URL     = "https://genaipro.vn/api/v1"
SSE_TIMEOUT  = 600   # seconds — video can take several minutes

# Model names — leave None to omit the field and use the API default.
IMAGE_MODEL: str | None = None
VIDEO_MODEL: str | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _auth_headers(api_key: str) -> dict:
    """Headers for form-encoded SSE requests (no Content-Type — requests sets it)."""
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept":        "text/event-stream",
    }


def _is_credit_error(text: str) -> bool:
    lowered = text.lower()
    return any(k in lowered for k in (
        "insufficient balance", "insufficient credits", "not enough credits",
        "not enough balance", "no credits", "credit limit",
    ))


def _consume_sse(resp: requests.Response, label: str = "veo") -> dict:
    """Read a typed SSE stream (event: / data: pairs) and return the terminal data.

    Handles the Genaipro format:
      event:image_generation_status
      data:generating                   ← non-JSON progress string, ignored

      event:image_generation_status
      data:{"status":"finished","url":"https://..."}   ← terminal

      event:error
      data:{"code":500,"error":"..."}   ← raise RuntimeError
    """
    last_data: dict = {}
    current_event: str = ""
    terminal_statuses = {"finished", "completed", "success", "done", "finish"}
    error_statuses    = {"failed", "error", "cancelled", "canceled"}

    for raw in resp.iter_lines(decode_unicode=True):
        if not raw:
            current_event = ""   # blank line resets event context
            continue

        # Track event type from "event:" lines
        if raw.startswith("event:"):
            current_event = raw[6:].strip().lower()
            print(f"[{label}] SSE event type: {current_event!r}")
            continue

        if not raw.startswith("data:"):
            continue  # id:, retry:, comments — skip

        text = raw[5:].strip()
        if not text or text == "[DONE]":
            continue

        # If the current event is "error", raise immediately
        if current_event in error_statuses:
            try:
                err_data = json.loads(text)
            except json.JSONDecodeError:
                err_data = {"raw": text}
            raw_msg = str(err_data)
            if _is_credit_error(raw_msg):
                raise RuntimeError(
                    f"Genaipro: créditos insuficientes — recarga tu cuenta en genaipro.vn"
                )
            raise RuntimeError(f"GenAIPro {label} falló: {err_data}")

        # Try to parse as JSON
        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            # Non-JSON data (e.g. "generating") — just a progress string, skip
            print(f"[{label}] SSE progress: {text[:100]}")
            continue

        # Unwrap { "type": "result", "data": {...} } envelope if present
        inner = event.get("data") if isinstance(event.get("data"), dict) else event

        # Determine status from JSON content or current event type
        status = str(
            inner.get("status", "") or event.get("type", "") or current_event
        ).lower()

        print(f"[{label}] SSE data status={status!r} keys={list(inner.keys())}")
        last_data = inner

        if status in terminal_statuses:
            print(f"[{label}] ✓ Generación completada (status={status!r})")
            return inner

        if status in error_statuses:
            raw_msg = str(inner)
            if _is_credit_error(raw_msg):
                raise RuntimeError(
                    f"Genaipro: créditos insuficientes — recarga tu cuenta en genaipro.vn"
                )
            raise RuntimeError(f"GenAIPro {label} falló: {inner}")

    # Stream ended without terminal event
    if last_data:
        print(f"[{label}] Stream cerrado. Usando último evento recibido.")
        return last_data

    raise RuntimeError(f"GenAIPro {label}: stream terminó sin datos de resultado")


def _extract_url(data: dict, candidates: list[str]) -> str:
    """Search candidate key names for a result URL, handling nested lists/dicts."""
    for key in candidates:
        val = data.get(key)
        if not val:
            continue
        if isinstance(val, str) and val.startswith("http"):
            return val
        if isinstance(val, list) and val:
            item = val[0]
            if isinstance(item, str) and item.startswith("http"):
                return item
            if isinstance(item, dict):
                for sub in ("url", "image_url", "video_url", "output", "result", "src"):
                    u = item.get(sub, "")
                    if isinstance(u, str) and u.startswith("http"):
                        return u

    # Deep search — any string value that looks like a URL
    for v in data.values():
        if isinstance(v, str) and v.startswith("http") and ("." in v):
            return v

    raise RuntimeError(
        f"No se encontró URL de resultado en la respuesta GenAIPro. "
        f"Claves recibidas: {list(data.keys())}"
    )


# ── Image generation ──────────────────────────────────────────────────────────

def generate_image(
    prompt: str,
    output_path: Path,
    api_key: str,
    aspect_ratio: str = "IMAGE_ASPECT_RATIO_LANDSCAPE",
    model: str | None = IMAGE_MODEL,
) -> Path:
    """Call /veo/create-image via form-encoded SSE, download result, save as JPEG."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError(
            "generate_image: el prompt está vacío. "
            "Genera el prompt visual de la escena antes de llamar a Genaipro."
        )

    # Form-encoded payload (NOT json=) — required by Genaipro API
    form: dict = {
        "prompt":           prompt,
        "aspect_ratio":     aspect_ratio,
        "number_of_images": "1",
    }
    if model:
        form["model"] = model

    print(f"[GenAIPro Veo] POST /veo/create-image  aspect={aspect_ratio}")
    print(f"[GenAIPro Veo] Prompt ({len(prompt)} chars): {prompt[:120]}{'…' if len(prompt) > 120 else ''}")

    with requests.post(
        f"{BASE_URL}/veo/create-image",
        headers=_auth_headers(api_key),
        data=form,          # form-encoded, not json
        stream=True,
        timeout=SSE_TIMEOUT,
    ) as resp:
        if not resp.ok:
            body = resp.text[:500]
            if _is_credit_error(body):
                raise RuntimeError(
                    "Genaipro /veo/create-image: créditos insuficientes — "
                    "recarga tu cuenta en genaipro.vn"
                )
            raise RuntimeError(
                f"GenAIPro /veo/create-image error {resp.status_code}: {body}"
            )
        data = _consume_sse(resp, label="veo/image")

    image_url = _extract_url(data, [
        "url", "image_url", "output", "result",
        "images", "generated_images", "data", "src",
    ])

    print(f"[GenAIPro Veo] Descargando imagen…")
    img_resp = requests.get(image_url, timeout=120)
    img_resp.raise_for_status()
    output_path.write_bytes(img_resp.content)
    print(
        f"[GenAIPro Veo] ✓ Imagen guardada: {output_path.name} "
        f"({len(img_resp.content):,} bytes)"
    )
    return output_path


# ── Video generation ──────────────────────────────────────────────────────────

def animate_image(
    image_path: Path,
    output_path: Path,
    api_key: str,
    prompt: str = "",
    aspect_ratio: str = "VIDEO_ASPECT_RATIO_LANDSCAPE",
    model: str | None = VIDEO_MODEL,
) -> Path:
    """Call /veo/frames-to-video via form-encoded SSE, download MP4."""
    image_path  = Path(image_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    img_bytes   = image_path.read_bytes()
    b64         = base64.b64encode(img_bytes).decode()
    start_image = f"data:image/jpeg;base64,{b64}"

    motion_prompt = (prompt or "").strip() or "Slow cinematic pan, smooth camera movement"

    # Form-encoded payload (NOT json=)
    form: dict = {
        "start_image":      start_image,
        "prompt":           motion_prompt,
        "aspect_ratio":     aspect_ratio,
        "number_of_videos": "1",
    }
    if model:
        form["model"] = model

    print(
        f"[GenAIPro Veo] POST /veo/frames-to-video  "
        f"image={image_path.name} ({len(img_bytes):,} bytes)"
    )

    with requests.post(
        f"{BASE_URL}/veo/frames-to-video",
        headers=_auth_headers(api_key),
        data=form,          # form-encoded, not json
        stream=True,
        timeout=SSE_TIMEOUT,
    ) as resp:
        if not resp.ok:
            body = resp.text[:500]
            if _is_credit_error(body):
                raise RuntimeError(
                    "Genaipro /veo/frames-to-video: créditos insuficientes — "
                    "recarga tu cuenta en genaipro.vn"
                )
            raise RuntimeError(
                f"GenAIPro /veo/frames-to-video error {resp.status_code}: {body}"
            )
        data = _consume_sse(resp, label="veo/video")

    video_url = _extract_url(data, [
        "url", "video_url", "output", "result",
        "videos", "generated_videos", "data", "src",
    ])

    print(f"[GenAIPro Veo] Descargando video…")
    vid_resp = requests.get(video_url, timeout=300)
    vid_resp.raise_for_status()
    output_path.write_bytes(vid_resp.content)
    print(
        f"[GenAIPro Veo] ✓ Video guardado: {output_path.name} "
        f"({len(vid_resp.content):,} bytes)"
    )
    return output_path
