"""Grok (xAI) API service — Plan B animation fallback.

When Meta AI fails 2× in a row this service:
  1. Uses Grok Vision (grok-2-vision-1212) to analyze the still image and
     produce an *enhanced* animation prompt.
  2. Sends that improved prompt to Replicate LTX Video to generate the clip.

API docs: https://docs.x.ai/docs
Base URL : https://api.x.ai/v1  (OpenAI-compatible)

The Grok API key is read from the AppSetting DB row with key="grok_api_key".
Store it via the Settings panel in the UI.
"""
from __future__ import annotations

import base64
from pathlib import Path


# ── Grok client ───────────────────────────────────────────────────────────────

def _grok_client(api_key: str):
    """Return an OpenAI-compatible client pointed at xAI's endpoint."""
    from openai import OpenAI
    return OpenAI(api_key=api_key, base_url="https://api.x.ai/v1")


# ── Vision: enhance motion prompt ─────────────────────────────────────────────

def enhance_motion_prompt(image_path: str, motion_prompt: str, api_key: str) -> str:
    """
    Ask Grok Vision to analyze the image and refine the animation instruction.

    Returns a single-sentence improved prompt suitable for Replicate LTX Video.
    Falls back to the original motion_prompt if the API call fails.
    """
    img = Path(image_path)
    suffix = img.suffix.lstrip(".").lower() or "jpeg"
    b64 = base64.b64encode(img.read_bytes()).decode()

    client = _grok_client(api_key)
    resp = client.chat.completions.create(
        model="grok-2-vision-1212",
        max_tokens=80,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/{suffix};base64,{b64}",
                            "detail": "high",
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            f"Animation instruction: '{motion_prompt}'\n"
                            "Based on this image and instruction, write ONE sentence "
                            "(max 20 words) describing the camera movement and subject motion "
                            "for a video AI model. Return only the prompt, no explanation."
                        ),
                    },
                ],
            }
        ],
    )
    return resp.choices[0].message.content.strip()


# ── Main fallback function ─────────────────────────────────────────────────────

def animate_with_grok_fallback(
    image_path: str,
    motion_prompt: str,
    output_path: str,
    grok_api_key: str,
    replicate_api_key: str = "",
) -> str:
    """
    Plan-B animation pipeline:
      Grok Vision (prompt enhancement) → Replicate LTX Video (actual generation)

    Parameters
    ----------
    image_path       : path to the source image (JPEG/PNG)
    motion_prompt    : original motion prompt from the UI / motion_service
    output_path      : where to save the output MP4
    grok_api_key     : xAI Grok API key
    replicate_api_key: Replicate token (falls back to settings if empty)

    Returns output_path on success. Raises RuntimeError on failure.
    """
    # Step 1 — enhance prompt with Grok Vision
    try:
        enhanced = enhance_motion_prompt(image_path, motion_prompt, grok_api_key)
        print(f"[Grok] Enhanced prompt: {enhanced}")
    except Exception as exc:
        print(f"[Grok] Vision enhancement failed ({exc}); using original prompt.")
        enhanced = motion_prompt

    # Step 2 — generate video with Replicate LTX Video
    from .. import replicate_service

    replicate_service.animate_image(
        image_path=Path(image_path),
        output_path=Path(output_path),
        prompt=enhanced,
        api_key=replicate_api_key,
    )
    return output_path
