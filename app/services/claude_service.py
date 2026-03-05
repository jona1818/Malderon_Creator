"""Claude API service - script generation, image prompts, keyword extraction."""
import json
import re
from pathlib import Path
from typing import Dict, List, Optional
from anthropic import Anthropic
from ..config import settings

client = Anthropic(api_key=settings.anthropic_api_key)

# ── Root path (two levels up from this file: app/services/ → root) ────────────
_ROOT = Path(__file__).resolve().parent.parent.parent

# ── Duration config ────────────────────────────────────────────────────────────

# Scene count range per duration (each scene ~80-100 words / 25-30 sec)
DURATION_SCENES = {
    "6-8":   (15, 20),
    "10-12": (25, 30),
    "18-20": (45, 55),
    "30-40": (75, 95),
}

# Talking point ranges for outline generation
DURATION_TALKING_POINTS = {
    "6-8":   (4, 5),
    "10-12": (6, 12),
    "18-20": (12, 18),
    "30-40": (20, 30),
}

# Target word counts per duration
DURATION_WORD_COUNTS = {
    "6-8":   (900, 1200, 8),
    "10-12": (1500, 1800, 12),
    "18-20": (2500, 3000, 20),
    "30-40": (4500, 6000, 40),
}


# ── Prompt guide files ─────────────────────────────────────────────────────────

def _read_guide(filename: str) -> str:
    """Read a .txt guide file from the project root. Returns empty string if missing."""
    path = _ROOT / filename
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


# ── Main script generation (single-call pipeline) ─────────────────────────────

def generate_script_full(
    title: str,
    transcripts: Optional[List[dict]] = None,
    video_type: str = "top10",
    duration: str = "6-8",
) -> str:
    """
    Single-call pipeline:
      1. Internally generates an outline (not shown to user)
      2. Expands it into the final narration script
    Returns ONLY the final narration script, plain text, ready for TTS.
    """
    # ── Load guide files ───────────────────────────────────────────────────────
    promptguide = _read_guide("promptguide.txt")
    if video_type == "documental":
        style_guide = _read_guide("documentary.txt")
    else:
        style_guide = _read_guide("top10style.txt")

    # ── Talking points range ───────────────────────────────────────────────────
    tp_min, tp_max = DURATION_TALKING_POINTS.get(duration, (4, 5))
    n_points = (tp_min + tp_max) // 2  # pick middle value

    # ── Word count target ──────────────────────────────────────────────────────
    min_w, max_w, dur_min = DURATION_WORD_COUNTS.get(duration, (900, 1200, 8))

    # ── Build system prompt ────────────────────────────────────────────────────
    system_parts = [
        "You are an expert YouTube video scriptwriter.",
        "",
        "=== VIDRUSH PROMPTING GUIDE ===",
        promptguide,
        "",
        "=== VIDEO STYLE GUIDE ===",
        style_guide,
        "",
        "=== OUTPUT RULES (STRICTLY ENFORCED) ===",
        "- Return ONLY the final narration script. Plain text, no markdown.",
        "- Do NOT include the outline, talking points header, or any labels.",
        "- Do NOT include: 'NARRATOR:', 'Scene:', '[Music]', '[Pause]', timestamps.",
        "- Do NOT use bold (**), italic (*), or headers (#).",
        "- The output must be pure spoken narration, ready for text-to-speech.",
        f"- Target length: {min_w} to {max_w} words (approximately {dur_min} minutes).",
    ]
    system_prompt = "\n".join(system_parts)

    # ── Build transcripts block ────────────────────────────────────────────────
    transcript_block = ""
    if transcripts:
        parts = []
        for t in transcripts:
            title_ref = t.get("title", "Reference Video")
            text = t.get("transcript", "").strip()
            if text:
                parts.append(f"Video reference: {title_ref}\n{text}")
        if parts:
            transcript_block = (
                "\n\n=== REFERENCE TRANSCRIPTS ===\n"
                "Use the following transcripts as style and structure references ONLY. "
                "Do NOT copy content from them.\n\n"
                + "\n\n---\n\n".join(parts)
            )

    # ── Build user prompt ──────────────────────────────────────────────────────
    video_type_label = "Top 10 countdown" if video_type != "documental" else "documentary"
    user_prompt = f"""VIDEO TITLE: {title}
VIDEO TYPE: {video_type_label}
VIDEO DURATION: {dur_min} minutes (~{min_w}-{max_w} words)
{transcript_block}

TASK:
Step 1 (internal only - DO NOT output): Generate an outline with {n_points} detailed talking points about "{title}". Use the reference transcripts (if provided) as a guide for structure and style.

Step 2 (this is what you return): Using the outline you just created internally, write the complete final narration script for this video. Follow all the style guides and output rules above.

RETURN ONLY THE FINAL NARRATION SCRIPT. Nothing else."""

    # ── Call Claude ────────────────────────────────────────────────────────────
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return response.content[0].text.strip()


# ── Image prompt generation ────────────────────────────────────────────────────

IMAGE_PROMPT_SYSTEM = """You are a visual prompt engineer for AI image generation.
Create detailed, cinematic image prompts optimized for Google Imagen 4.
Return ONLY valid JSON - no markdown fences, no extra text."""

IMAGE_PROMPT_TEMPLATE = """Create a detailed visual image prompt for Google Imagen 4 for this video scene:

Scene narration: {narration}
Visual description: {visual_description}
Reference character/style: {reference_character}

Style: cinematic, documentary, photorealistic. 16:9 aspect ratio, 1920x1080. No text, no watermarks, no borders.

Return JSON:
{{
  "image_prompt": "Detailed photorealistic image prompt. Include scene composition, lighting, camera angle, color palette, mood, and visual style. Comma-separated descriptive terms."
}}"""

KEYWORDS_SYSTEM = """You are a stock footage search specialist.
Extract the best search keywords for finding relevant stock footage.
Return ONLY valid JSON - no markdown fences, no extra text."""

KEYWORDS_TEMPLATE = """Extract stock footage search keywords for this scene:

Narration: {narration}
Visual description: {visual_description}

Return JSON:
{{
  "primary_keyword": "Best 2-3 word search query",
  "secondary_keywords": ["alt1", "alt2", "alt3"]
}}"""


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def generate_image_prompt(
    narration: str, visual_description: str, reference_character: str = ""
) -> str:
    prompt = IMAGE_PROMPT_TEMPLATE.format(
        narration=narration,
        visual_description=visual_description,
        reference_character=reference_character or "cinematic, photorealistic",
    )
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=IMAGE_PROMPT_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return _extract_json(response.content[0].text)["image_prompt"]


def generate_search_keywords(narration: str, visual_description: str) -> Dict:
    prompt = KEYWORDS_TEMPLATE.format(narration=narration, visual_description=visual_description)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=KEYWORDS_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return _extract_json(response.content[0].text)


# ── Legacy clean_script (kept for compatibility) ───────────────────────────────

def clean_script(text: str) -> str:
    """
    Strip ALL non-narration content from a Claude-generated script.
    Returns only the spoken narration paragraphs, ready for TTS.
    """
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'__(.+?)__', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'\*(.+?)\*', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'_(.+?)_', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'\[.*?\]', '', text)

    _REMOVE_LINE_RE = re.compile(
        r'^('
        r'#{1,6}\s'
        r'|[-=—–]{2,}\s*$'
        r'|YouTube Video Script'
        r'|Runtime\b'
        r'|Words?\s*:\s*\d'
        r'|Word Count\s*:'
        r'|Estimated (Runtime|Duration)\s*:'
        r'|Total Words?\s*:'
        r'|Script\s*:\s*$'
        r'|Title\s*:\s*\S'
        r'|Topic\s*:\s*\S'
        r'|COLD OPEN[:\s]*$'
        r'|ACT \d+[:\s]*$'
        r'|INTRO[:\s]*$'
        r'|OUTRO[:\s]*$'
        r'|HOOK[:\s]*$'
        r'|CONCLUSION[:\s]*$'
        r'|OPENING[:\s]*$'
        r'|CLOSING[:\s]*$'
        r'|SECTION \d+[:\s]*$'
        r'|PART \d+[:\s]*$'
        r'|SCENE \d+[:\s]*$'
        r'|CHAPTER \d+[:\s]*$'
        r'|TALKING POINT \d+[:\s]*$'
        r'|CTA[:\s]*$'
        r'|FADE (IN|OUT)[:\s]*$'
        r'|MUSIC[:\s]*$'
        r'|PAUSE[:\s]*$'
        r')',
        re.IGNORECASE,
    )

    cleaned_lines = []
    for line in text.split('\n'):
        stripped = line.strip()
        if not stripped:
            cleaned_lines.append('')
            continue
        if _REMOVE_LINE_RE.match(stripped):
            continue
        cleaned_lines.append(line)

    text = '\n'.join(cleaned_lines)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# ── Legacy generate_script (kept in case referenced elsewhere) ─────────────────

def generate_script(topic: str, video_type: str = "top10", duration: str = "6-8") -> Dict:
    """Legacy: generates script from a topic string. Kept for backward compatibility."""
    return generate_script_full(
        title=topic,
        transcripts=None,
        video_type=video_type,
        duration=duration,
    )


# ── Legacy outline functions (now no-ops, kept for import compatibility) ────────

def generate_outline(title: str, transcripts: list = None) -> str:
    """Deprecated: outline generation is now internal to generate_script_full()."""
    return f"[Outline for: {title}]"


def generate_script_from_outline(outline: str, duration: str = "6-8") -> str:
    """Deprecated: script is now generated directly by generate_script_full()."""
    return outline


# Scene count range per duration  (each scene ~80-100 words / 25-30 sec)
