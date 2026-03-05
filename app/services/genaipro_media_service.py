"""
GenAIPro Veo — image and video generation via SSE stream.

BASE URL : https://genaipro.vn/api/v1
AUTH     : Authorization: Bearer {api_key}   (same key as TTS)

IMPORTANT — request format:
  Both endpoints use application/x-www-form-urlencoded (form-encoded),
  NOT application/json.  JSON bodies return 400 "Prompt is required".

Image generation
  POST /veo/create-image  (form-encoded, stream=True)
  Fields: prompt, aspect_ratio=IMAGE_ASPECT_RATIO_LANDSCAPE, number_of_images=1
  Response: SSE stream OR plain JSON with file_urls:
    {"id":"...","file_urls":["https://files.genaipro.vn/image_xxx.png"],
     "status":"completed","created_at":"..."}
  _consume_sse() handles both SSE events and plain JSON lines.

Video generation
  POST /veo/frames-to-video  (form-encoded, stream=True)
  Fields: start_image=<file upload>, prompt, aspect_ratio, number_of_videos=1
  Response: same format

Retry: 3 attempts, 5s sleep between retries (not for credit errors).
"""
from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path

import requests

BASE_URL     = "https://genaipro.vn/api/v1"
SSE_TIMEOUT  = 600   # seconds — video can take several minutes

# Model names — leave None to omit the field and use the API default.
IMAGE_MODEL: str | None = None
VIDEO_MODEL: str | None = "veo-2.0-generate-001"


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


def _sanitize_prompt(prompt: str, max_chars: int = 800) -> str:
    """Clean and truncate a prompt for Genaipro Veo.

    Genaipro's model rejects very long prompts and markdown formatting.
    Steps:
      1. Strip markdown symbols (*, _, #, `, [, ], (, ))
      2. Collapse whitespace
      3. Truncate at the last sentence boundary ≤ max_chars
    """
    prompt = re.sub(r"[*_#`\[\]()\|]", " ", prompt)
    prompt = re.sub(r"\s+", " ", prompt).strip()
    if len(prompt) <= max_chars:
        return prompt
    # Truncate at last sentence boundary
    truncated = prompt[:max_chars]
    last_dot = max(truncated.rfind("."), truncated.rfind("!"), truncated.rfind("?"))
    if last_dot > max_chars * 0.6:
        truncated = truncated[: last_dot + 1]
    return truncated.strip()


def _consume_sse(resp: requests.Response, label: str = "veo") -> dict:
    """Read the Genaipro SSE stream and return the terminal data dict.

    Handles both SSE format (event:/data: lines) and plain-JSON responses:

      SSE format:
        event:image_generation_status
        data:generating                   ← progress string, ignored
        event:image_generation_status
        data:{"status":"finished","url":"https://...","file_urls":[...]}
        event:error
        data:{"code":500,"error":"..."}   ← raise RuntimeError

      Plain JSON (fallback):
        {"id":"...","file_urls":["https://..."],"status":"completed"}
    """
    last_data: dict = {}
    current_event: str = ""
    terminal_statuses = {"finished", "completed", "success", "done", "finish"}
    error_statuses    = {"failed", "error", "cancelled", "canceled"}
    all_lines: list[str] = []

    for raw in resp.iter_lines(decode_unicode=True):
        all_lines.append(raw or "")

        if not raw:
            current_event = ""   # blank line resets event context
            continue

        # Track event type from "event:" lines
        if raw.startswith("event:"):
            current_event = raw[6:].strip().lower()
            print(f"[{label}] SSE event type: {current_event!r}")
            continue

        # ── Plain JSON line (no data: prefix) ─────────────────────────────
        if not raw.startswith("data:"):
            # Could be a plain JSON response instead of SSE
            try:
                event = json.loads(raw)
                if isinstance(event, dict):
                    print(f"[{label}] Plain JSON line: {str(event)[:200]}")
                    inner = event.get("data") if isinstance(event.get("data"), dict) else event
                    status = str(inner.get("status", "") or event.get("type", "") or "").lower()
                    if event.get("error"):
                        raw_msg = str(event)
                        if _is_credit_error(raw_msg):
                            raise RuntimeError("Genaipro: créditos insuficientes — recarga tu cuenta en genaipro.vn")
                        raise RuntimeError(f"GenAIPro {label} falló: {event.get('error')}")
                    if status in terminal_statuses or event.get("file_urls"):
                        print(f"[{label}] ✓ Respuesta JSON directa recibida.")
                        return inner
                    last_data = inner
            except json.JSONDecodeError:
                pass  # id:, retry:, comments — skip
            continue

        # ── SSE data: line ─────────────────────────────────────────────────
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
            print(f"[{label}] ❌ ERROR del servidor: {raw_msg}")  # full detail for diagnosis
            if _is_credit_error(raw_msg):
                raise RuntimeError(
                    "Genaipro: créditos insuficientes — recarga tu cuenta en genaipro.vn"
                )
            raise RuntimeError(f"GenAIPro {label} falló: {err_data}")

        # Try to parse data: payload as JSON
        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            print(f"[{label}] SSE progress: {text[:100]}")
            continue

        # Unwrap { "type": "result", "data": {...} } envelope if present
        inner = event.get("data") if isinstance(event.get("data"), dict) else event

        # Determine status
        status = str(
            inner.get("status", "") or event.get("type", "") or current_event
        ).lower()

        print(f"[{label}] SSE data status={status!r} keys={list(inner.keys())}")
        last_data = inner

        # A response with file_urls is always terminal
        if inner.get("file_urls") or event.get("file_urls"):
            print(f"[{label}] ✓ file_urls encontrado — generación completada.")
            return inner

        if status in terminal_statuses:
            print(f"[{label}] ✓ Generación completada (status={status!r})")
            return inner

        if status in error_statuses:
            raw_msg = str(inner)
            if _is_credit_error(raw_msg):
                raise RuntimeError(
                    "Genaipro: créditos insuficientes — recarga tu cuenta en genaipro.vn"
                )
            raise RuntimeError(f"GenAIPro {label} falló: {inner}")

    # Stream ended without terminal event
    if last_data:
        print(f"[{label}] Stream cerrado. Usando último evento recibido.")
        return last_data

    # Log everything received for diagnosis
    print(f"[{label}] ⚠️ Stream completo recibido ({len(all_lines)} líneas):")
    for ln in all_lines[:30]:
        print(f"  | {ln[:200]}")
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
    """Call /veo/create-image via SSE stream, download result, save as JPEG.

    Retry strategy (3 attempts):
      1 & 2 — full sanitized prompt + all form fields
      3     — minimal form (prompt-only, first sentence ≤ 150 chars)
              to diagnose whether extra fields or long prompts cause 500s.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError(
            "generate_image: el prompt está vacío. "
            "Genera el prompt visual de la escena antes de llamar a Genaipro."
        )

    # Sanitize: remove markdown symbols, collapse whitespace, truncate to 800 chars
    prompt = _sanitize_prompt(prompt, max_chars=800)
    # Ultra-short fallback used on attempt 3 if the full prompt keeps failing
    prompt_short = (prompt.split(".")[0][:150].strip() or prompt[:150]).strip()

    print(f"[GenAIPro Veo] POST /veo/create-image  aspect={aspect_ratio}")
    print(f"[GenAIPro Veo] Prompt ({len(prompt)} chars): {prompt[:200]}")

    last_exc: Exception | None = None
    data: dict = {}

    # ── Three genuinely different strategies ──────────────────────────────────
    #
    # Strategy A (attempt 1): url-encoded form, NO Accept header → server may
    #   return plain synchronous JSON without the SSE wrapper.
    # Strategy B (attempt 2): multipart form + Accept: text/event-stream → SSE.
    # Strategy C (attempt 3): minimal prompt (first sentence ≤150 chars),
    #   url-encoded, no Accept header — rules out prompt-length issues.
    #
    for attempt in range(1, 4):
        try:
            if attempt == 1:
                # ── Strategy A: sync JSON via url-encoded form ─────────────
                form: dict = {
                    "prompt":           prompt,
                    "aspect_ratio":     aspect_ratio,
                    "number_of_images": "1",
                }
                if model:
                    form["model"] = model
                print(
                    f"[GenAIPro Veo] Intento 1 (sync url-encoded, sin Accept-SSE), "
                    f"prompt={len(prompt)} chars"
                )
                with requests.post(
                    f"{BASE_URL}/veo/create-image",
                    headers={"Authorization": f"Bearer {api_key}"},
                    data=form,
                    stream=True,      # still stream so we can read SSE if server ignores Accept
                    timeout=SSE_TIMEOUT,
                ) as resp:
                    print(
                        f"[GenAIPro Veo] HTTP {resp.status_code} "
                        f"Content-Type={resp.headers.get('Content-Type', '')!r}"
                    )
                    if not resp.ok:
                        body = resp.text[:500]
                        print(f"[GenAIPro Veo] Body: {body}")
                        if _is_credit_error(body):
                            raise RuntimeError(
                                "Genaipro /veo/create-image: créditos insuficientes — "
                                "recarga tu cuenta en genaipro.vn"
                            )
                        raise RuntimeError(
                            f"GenAIPro /veo/create-image error {resp.status_code}: {body}"
                        )
                    data = _consume_sse(resp, label="veo/image[A]")

            elif attempt == 2:
                # ── Strategy B: SSE via multipart form (original approach) ─
                multipart = {
                    "prompt":           (None, prompt),
                    "aspect_ratio":     (None, aspect_ratio),
                    "number_of_images": (None, "1"),
                }
                if model:
                    multipart["model"] = (None, model)
                print(
                    f"[GenAIPro Veo] Intento 2 (multipart + Accept-SSE), "
                    f"prompt={len(prompt)} chars"
                )
                with requests.post(
                    f"{BASE_URL}/veo/create-image",
                    headers={"Authorization": f"Bearer {api_key}",
                             "Accept": "text/event-stream"},
                    files=multipart,
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
                    data = _consume_sse(resp, label="veo/image[B]")

            else:
                # ── Strategy C: minimal prompt, url-encoded, no Accept ──────
                print(
                    f"[GenAIPro Veo] ⚠️ Intento 3 (prompt corto, url-encoded), "
                    f"{len(prompt_short)} chars: {prompt_short[:80]!r}"
                )
                with requests.post(
                    f"{BASE_URL}/veo/create-image",
                    headers={"Authorization": f"Bearer {api_key}"},
                    data={"prompt": prompt_short},
                    stream=True,
                    timeout=SSE_TIMEOUT,
                ) as resp:
                    print(
                        f"[GenAIPro Veo] HTTP {resp.status_code} "
                        f"Content-Type={resp.headers.get('Content-Type', '')!r}"
                    )
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
                    data = _consume_sse(resp, label="veo/image[C]")

            break  # success — exit retry loop

        except RuntimeError as exc:
            last_exc = exc
            if "créditos insuficientes" in str(exc):
                raise  # never retry credit errors
            if attempt < 3:
                print(
                    f"[GenAIPro Veo] ⚠️ Intento {attempt}/3 falló: "
                    f"{str(exc)[:120]}. Reintentando en 5s…"
                )
                time.sleep(5)
    else:
        raise last_exc  # all 3 attempts exhausted

    image_url = _extract_url(data, [
        "file_urls", "url", "image_url", "output", "result",
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

    img_bytes     = image_path.read_bytes()
    motion_prompt = (prompt or "").strip() or "Slow cinematic pan, smooth camera movement"

    print(
        f"[GenAIPro Veo] POST /veo/frames-to-video  "
        f"image={image_path.name} ({len(img_bytes):,} bytes)"
    )

    last_exc_v: Exception | None = None
    data: dict = {}
    for attempt in range(1, 4):
        # Build multipart payload — start_image as file upload
        multipart_v: dict = {
            "start_image":      (image_path.name, img_bytes, "image/jpeg"),
            "prompt":           (None, motion_prompt),
            "aspect_ratio":     (None, aspect_ratio),
            "number_of_videos": (None, "1"),
        }
        if model:
            multipart_v["model"] = (None, model)
        try:
            with requests.post(
                f"{BASE_URL}/veo/frames-to-video",
                headers={"Authorization": f"Bearer {api_key}",
                         "Accept": "text/event-stream"},
                files=multipart_v,   # multipart/form-data
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
            break  # success
        except RuntimeError as exc:
            last_exc_v = exc
            if "créditos insuficientes" in str(exc):
                raise
            if attempt < 3:
                print(f"[GenAIPro Veo] Intento {attempt}/3 fallo: {str(exc)[:120]}. Reintentando en 5s...")
                time.sleep(5)
    else:
        raise last_exc_v

    video_url = _extract_url(data, [
        "file_urls", "url", "video_url", "output", "result",
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
