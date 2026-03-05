"""
Google AI Studio service — Imagen 3 image generation ("Nano Banana").

API key:  GOOGLE_API_KEY in .env
Model:    imagen-3.0-generate-002  (16:9 native, 1024×576 → upscaled)
Aspect:   16:9  (YouTube landscape)
Output:   /projects/{slug}/chunk_N/images/image_N.jpg

This is the PRIMARY image generation engine for the whole pipeline.
All batch image prompts are generated with Gemini 1.5 Flash before calling
Imagen so that only one AI call is needed per scene instead of two.

Veo (video animation) is NOT yet implemented — animate_image() is a no-op stub
that copies the image so NCA can treat it as a still.
"""
from __future__ import annotations

from pathlib import Path

from ..config import settings


# ── Auth helper ───────────────────────────────────────────────────────────────

def _api_key() -> str:
    key = settings.google_api_key
    if not key:
        raise RuntimeError(
            "GOOGLE_API_KEY no está configurado en .env. "
            "Agrega: GOOGLE_API_KEY=tu_clave_aqui"
        )
    return key


# ── Batch Image Prompt Generation — Gemini 1.5 Flash ─────────────────────────

_BATCH_PROMPT_SYSTEM = """You are a visual prompt engineer for AI image generation.
Create detailed, cinematic image prompts optimized for Google Imagen 4.
For each scene, produce a rich, comma-separated description including:
- Composition and framing
- Lighting and color palette
- Camera angle
- Mood and atmosphere
- Key visual elements
Style: cinematic, documentary, photorealistic. 16:9 aspect ratio. No text, no watermarks.
Return ONLY valid JSON — no markdown fences, no extra text."""

_BATCH_PROMPT_TEMPLATE = """Generate detailed visual image prompts for all scenes in this video.

Reference style/character: {reference_character}

Scenes:
{scenes_block}

Return JSON:
{{
  "prompts": [
    {{
      "scene_number": 1,
      "image_prompt": "Detailed photorealistic prompt for scene 1..."
    }},
    ...
  ]
}}"""


def batch_generate_image_prompts(
    scenes: list[dict],  # list of {"scene_number": int, "narration": str, "visual_description": str}
    reference_character: str = "",
) -> dict[int, str]:
    """Send all scenes to Gemini 1.5 Flash in ONE call and return {scene_number: prompt}.

    This replaces per-scene Claude calls: much cheaper, faster, and avoids 429/529 errors.
    """
    import json
    import re
    from google import genai

    scenes_block = "\n".join(
        f"Scene {s['scene_number']}:\n"
        f"  Narration: {s['narration'][:300]}\n"
        f"  Visual description: {s.get('visual_description', '')[:200]}"
        for s in scenes
    )

    prompt = _BATCH_PROMPT_TEMPLATE.format(
        reference_character=reference_character or "cinematic, photorealistic, documentary",
        scenes_block=scenes_block,
    )

    client = genai.Client(api_key=_api_key())
    response = client.models.generate_content(
        model="gemini-1.5-flash-002",
        contents=f"{_BATCH_PROMPT_SYSTEM}\n\n{prompt}",
    )

    raw = response.text.strip()
    # Strip markdown fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    data = json.loads(raw)

    return {item["scene_number"]: item["image_prompt"] for item in data["prompts"]}


# ── Batch Video Prompt Generation (Motion Instructions) — Gemini 1.5 Flash ───

_BATCH_VIDEO_SYSTEM = """You are an AI video motion director.
Write extremely concise, literal MOTION instructions for the LTX Video AI model based on the scene's narration.
Do NOT describe the setting or subject (that's the image prompt's job).
ONLY describe the camera movement, action, and physics. Max 10-15 words per scene.
Examples:
- "Slow pan right across the room, soft dust particles floating."
- "Fast zoom into reporter's face, wind blowing hair."
- "Subtle camera shake, character turns head slowly to the left."
Return ONLY valid JSON."""

_BATCH_VIDEO_TEMPLATE = """Generate short motion/animation instructions for each scene.

Scenes:
{scenes_block}

Return JSON:
{{
  "prompts": [
    {{
      "scene_number": 1,
      "video_prompt": "Camera pushes in slowly, subtle breathing movement."
    }},
    ...
  ]
}}"""

def batch_generate_video_prompts(
    scenes: list[dict],  # list of {"scene_number": int, "narration": str, "image_prompt": str}
) -> dict[int, str]:
    """Send all scenes to Gemini 1.5 Flash to generate LTX motion instructions."""
    import json
    import re
    from google import genai

    scenes_block = "\n".join(
        f"Scene {s['scene_number']}:\n"
        f"  Narration (what's happening): {s['narration'][:300]}\n"
        f"  Visual Setting (already known): {s.get('image_prompt', '')[:150]}"
        for s in scenes
    )

    prompt = _BATCH_VIDEO_TEMPLATE.format(scenes_block=scenes_block)

    client = genai.Client(api_key=_api_key())
    response = client.models.generate_content(
        model="gemini-1.5-flash-002",
        contents=f"{_BATCH_VIDEO_SYSTEM}\n\n{prompt}",
    )

    raw = response.text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    data = json.loads(raw)

    return {item["scene_number"]: item["video_prompt"] for item in data["prompts"]}



# ── Image generation — Imagen 3.0 ─────────────────────────────────────────────

def generate_image(
    prompt: str,
    output_path: Path,
    aspect_ratio: str = "16:9",
    safety_filter_level: str = "block_only_high",
    person_generation: str = "allow_adult",
) -> Path:
    """Generate one image with Imagen 3.0 and save it to output_path (JPEG).

    Uses the google-genai SDK (not the deprecated google-generativeai).
    Model: imagen-3.0-generate-002
    """
    from google import genai
    from google.genai import types

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    client = genai.Client(api_key=_api_key())

    response = client.models.generate_images(
        model="imagen-3.0-generate-002",
        prompt=prompt,
        config=types.GenerateImageConfig(
            number_of_images=1,
            aspect_ratio=aspect_ratio,
            safety_filter_level=safety_filter_level,
            person_generation=person_generation,
        ),
    )

    if not response.generated_images:
        raise RuntimeError(
            "Google Imagen 3 no devolvió imágenes. "
            "Revisa los filtros de seguridad o el prompt."
        )

    image_bytes = response.generated_images[0].image.image_bytes
    if not image_bytes:
        raise RuntimeError("Google Imagen 3: imagen recibida pero sin bytes.")

    output_path.write_bytes(image_bytes)
    print(f"[Google Imagen] Imagen guardada: {output_path} ({len(image_bytes):,} bytes)")
    return output_path


# ── Video animation — Veo (stub) ──────────────────────────────────────────────

def animate_image(
    image_path: Path,
    output_path: Path,
    prompt: str = "",
) -> Path:
    """Stub — Veo video generation not yet implemented.

    Copies the source image to output_path so that NCA can use it as a
    static background. Replace this with a real Veo call when ready.
    """
    import shutil

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(image_path), str(output_path))
    print(f"[Google Veo] STUB — imagen copiada como video estático: {output_path}")
    return output_path
