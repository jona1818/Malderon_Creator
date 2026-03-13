"""
AI prompt generation service — batch image & video prompts via OpenRouter.

Uses OpenRouter (Gemini) for all AI calls, same as claude_service.py.
Image generation itself is handled by Pollinations (pollinations_service.py).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from openai import OpenAI
from ..config import settings

# ── OpenRouter client (shared config with claude_service) ────────────────────

_client = OpenAI(
    api_key=settings.openrouter_api_key,
    base_url="https://openrouter.ai/api/v1",
)
_MODEL_FAST = "google/gemini-2.0-flash-lite-001"


def _chat(system: str, user: str, max_tokens: int = 4096) -> str:
    resp = _client.chat.completions.create(
        model=_MODEL_FAST,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    content = resp.choices[0].message.content
    if content is None:
        raise RuntimeError("OpenRouter returned empty content (None)")
    return content.strip()


# ── Batch Image Prompt Generation ────────────────────────────────────────────

_BATCH_PROMPT_SYSTEM = """You are an expert visual prompt engineer for cinematic AI image generation.
You will receive the FULL SCRIPT of a video for context, and a list of scenes.
For each scene, generate a detailed image prompt that reflects the EXACT moment in the story.

IMPORTANT: Use the full script to understand WHO the characters are, WHERE the story takes place,
WHAT TIME PERIOD it is set in, and WHAT IS HAPPENING narratively. Each image must feel like it
belongs to this specific story, not a generic image.

CRITICAL RULES FOR VISUAL CONSISTENCY:
- Every prompt must share the SAME visual style: cinematic, dark moody lighting, rich color grading.
- Use consistent color palette across all scenes (deep shadows, warm highlights, desaturated midtones).
- Camera style: professional documentary cinematography (wide establishing shots, medium close-ups, aerial views).
- Lighting: dramatic natural light, golden hour, volumetric fog, rim lighting, chiaroscuro.
- NO people, NO characters, NO faces, NO human figures unless the narration explicitly describes a specific person.
- Focus on: landscapes, architecture, objects, environments, abstract concepts, aerial views, macro details.
- Aspect ratio: 16:9 widescreen. No text, no watermarks, no logos, no borders.
- Each prompt must be self-contained (describe everything needed to generate the image).
- Include story-specific details: locations, objects, symbols, time period, atmosphere from the script.

For each scene, produce a rich, comma-separated description including:
- Subject and composition (specific to the story moment)
- Lighting and color palette
- Camera angle and lens (e.g., wide-angle, telephoto, drone shot)
- Mood and atmosphere (matching the narrative tone)
- Textures, details, and story-specific elements

Return ONLY valid JSON — no markdown fences, no extra text."""

_BATCH_PROMPT_TEMPLATE = """Generate detailed cinematic image prompts for the scenes listed below.

VISUAL STYLE: {reference_style}

══════════════════════════════════════
FULL SCRIPT (for narrative context — read this to understand the story, characters, locations, and time period):
══════════════════════════════════════
{full_script}

══════════════════════════════════════
SCENES TO GENERATE PROMPTS FOR:
══════════════════════════════════════
{scenes_block}

For each scene, generate an image prompt that captures the EXACT narrative moment described.
Use specific details from the script (locations, objects, atmosphere, time period) — NOT generic images.

Return JSON:
{{
  "prompts": [
    {{
      "scene_number": 1,
      "image_prompt": "Detailed cinematic prompt for scene 1 with story-specific details..."
    }},
    ...
  ]
}}"""

# Max words before we chunk the batch into groups of 10
_MAX_SCRIPT_WORDS_SINGLE_BATCH = 3000
_SCENES_PER_BATCH = 10


def batch_generate_image_prompts(
    scenes: list[dict],
    reference_character: str = "",
    full_script: str = "",
) -> dict[int, str]:
    """Send scenes + full script context to Gemini and return {scene_number: prompt}.

    If the script is long (>3000 words), scenes are processed in batches of 10
    but the full script is always included for context.
    """
    style = reference_character or "cinematic, photorealistic, documentary, dark moody lighting, no people"
    script_text = (full_script or "").strip()
    if not script_text:
        script_text = "(No full script provided — use each scene's narration as context.)"

    # Truncate script to ~4000 words max to avoid token limits
    script_words = script_text.split()
    if len(script_words) > 4000:
        script_text = " ".join(script_words[:4000]) + "\n\n[... script truncated for length ...]"

    word_count = len(script_words)
    need_chunking = word_count > _MAX_SCRIPT_WORDS_SINGLE_BATCH and len(scenes) > _SCENES_PER_BATCH

    if need_chunking:
        # Process in batches of 10 scenes, each batch gets the full script
        all_results: dict[int, str] = {}
        for i in range(0, len(scenes), _SCENES_PER_BATCH):
            batch = scenes[i:i + _SCENES_PER_BATCH]
            print(f"[ImagePrompts] Batch {i // _SCENES_PER_BATCH + 1}: scenes {batch[0]['scene_number']}-{batch[-1]['scene_number']}")
            batch_result = _generate_batch(batch, style, script_text)
            all_results.update(batch_result)
        return all_results
    else:
        return _generate_batch(scenes, style, script_text)


def _generate_batch(scenes: list[dict], style: str, script_text: str) -> dict[int, str]:
    """Generate image prompts for a batch of scenes with full script context."""
    scenes_block = "\n".join(
        f"Scene {s['scene_number']}:\n"
        f"  Narration: {s['narration'][:400]}"
        for s in scenes
    )

    prompt = _BATCH_PROMPT_TEMPLATE.format(
        reference_style=style,
        full_script=script_text,
        scenes_block=scenes_block,
    )

    raw = _chat(_BATCH_PROMPT_SYSTEM, prompt, max_tokens=8192)
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    data = json.loads(raw)

    return {int(item["scene_number"]): item["image_prompt"] for item in data["prompts"]}


# ── Batch Video Prompt Generation (Motion Instructions) ──────────────────────

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
    scenes: list[dict],
) -> dict[int, str]:
    """Send all scenes to generate LTX motion instructions via OpenRouter."""
    scenes_block = "\n".join(
        f"Scene {s['scene_number']}:\n"
        f"  Narration (what's happening): {s['narration'][:300]}\n"
        f"  Visual Setting (already known): {s.get('image_prompt', '')[:150]}"
        for s in scenes
    )

    prompt = _BATCH_VIDEO_TEMPLATE.format(scenes_block=scenes_block)
    raw = _chat(_BATCH_VIDEO_SYSTEM, prompt, max_tokens=4096)
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    data = json.loads(raw)

    return {int(item["scene_number"]): item["video_prompt"] for item in data["prompts"]}


# ── Image generation — Imagen 3.0 (kept for compatibility but Pollinations is primary) ──

def generate_image(
    prompt: str,
    output_path: Path,
    aspect_ratio: str = "16:9",
    safety_filter_level: str = "block_only_high",
    person_generation: str = "allow_adult",
) -> Path:
    """Generate one image with Imagen 3.0 (requires GOOGLE_API_KEY with credits)."""
    from google import genai
    from google.genai import types

    key = settings.google_api_key
    if not key:
        raise RuntimeError("GOOGLE_API_KEY no configurado.")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    client = genai.Client(api_key=key)
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
        raise RuntimeError("Google Imagen 3 no devolvió imágenes.")

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
    """Stub — Veo video generation not yet implemented."""
    import shutil
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(image_path), str(output_path))
    print(f"[Google Veo] STUB — imagen copiada como video estático: {output_path}")
    return output_path
