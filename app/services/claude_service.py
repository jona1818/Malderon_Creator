"""Claude API service - script generation, image prompts, keyword extraction."""
import json
import re
from typing import Dict
from anthropic import Anthropic
from ..config import settings

client = Anthropic(api_key=settings.anthropic_api_key)

# Scene count range per duration  (each scene ~80-100 words / 25-30 sec)
DURATION_SCENES = {
    "6-8":   (15, 20),
    "10-12": (25, 30),
    "18-20": (45, 55),
    "30-40": (75, 95),
}

SCRIPT_SYSTEM = """You are a professional YouTube video scriptwriter.
Write engaging scripts split into scenes. Each scene must be 25-30 seconds of
narration (roughly 80-100 words). Return ONLY valid JSON - no markdown fences,
no extra text."""

SCRIPT_PROMPT_TOP10 = """Write a YouTube "Top {n_scenes}" countdown video script about: {topic}

Rules:
- Exactly {n_scenes} scenes total.
- Scene 1: short energetic hook (do not reveal what #1 is).
- Scenes 2 to {last}: one list item each, from #{countdown_start} down to #1 (last scene).
- Each scene: 80-100 words of energetic, surprising narration.
- Last scene: reveal #1 + strong call-to-action (like & subscribe).

Return JSON:
{{
  "title": "Top {n_items} ...",
  "scenes": [
    {{
      "scene_number": 1,
      "narration": "Spoken text (80-100 words).",
      "visual_description": "What should appear on screen."
    }}
  ]
}}"""

SCRIPT_PROMPT_DOCUMENTAL = """Write a YouTube documentary-style video script about: {topic}

Rules:
- {n_scenes} scenes total, flowing as a narrative documentary.
- Each scene: 80-100 words, informative yet captivating.
- Scene 1: hook with a surprising fact or question.
- Scene {n_scenes}: conclusion + call-to-action.
- Vary tone: some scenes factual, some emotional, some suspenseful.

Return JSON:
{{
  "title": "Documentary title",
  "scenes": [
    {{
      "scene_number": 1,
      "narration": "Spoken text (80-100 words).",
      "visual_description": "What should appear on screen."
    }}
  ]
}}"""

IMAGE_PROMPT_SYSTEM = """You are an expert AI image prompt engineer.
Create vivid, detailed prompts for image generation models.
Return ONLY valid JSON - no markdown fences, no extra text."""

IMAGE_PROMPT_TEMPLATE = """Create an image generation prompt for this video scene:

Scene narration: {narration}
Visual description: {visual_description}
Reference character/style: {reference_character}

Return JSON:
{{
  "image_prompt": "Detailed comma-separated prompt for SDXL. Include style, lighting, composition."
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


# ── Outline + script-from-outline ────────────────────────────────────────────

OUTLINE_SYSTEM = "You are an expert YouTube video script writer."

OUTLINE_PROMPT_WITH_TRANSCRIPTS = """Generate an outline with 10 detailed talking points \
(so provide what information to talk about for each talking point) About {title}. \
Use the attached transcript as a sample on how to structure this video. \
Format the talking points into a vidrush prompt.

TRANSCRIPT REFERENCE:
{transcripts}"""

OUTLINE_PROMPT_NO_TRANSCRIPTS = """Generate an outline with 10 detailed talking points \
(so provide what information to talk about for each talking point) About {title}. \
Format the talking points into a vidrush prompt."""

SCRIPT_FROM_OUTLINE_PROMPT = """Give me a script of {min_words} to {max_words} words. \
For a {duration_min}-minute video. These must be the required words.

OUTLINE:
{outline}"""

# Word count ranges (min, max, target minutes) per duration option
DURATION_WORD_COUNTS = {
    "6-8":   (900, 1200, 8),
    "10-12": (1500, 1800, 12),
    "18-20": (2500, 3000, 20),
    "30-40": (4500, 6000, 40),
}


def generate_outline(title: str, transcripts: list = None) -> str:
    """Generate a 10-point video outline, optionally guided by reference transcripts."""
    if transcripts:
        transcript_text = "\n\n---\n\n".join(
            f"Video: {t.get('title', 'Reference')}\n{t.get('transcript', '')}"
            for t in transcripts
        )
        prompt = OUTLINE_PROMPT_WITH_TRANSCRIPTS.format(
            title=title, transcripts=transcript_text
        )
    else:
        prompt = OUTLINE_PROMPT_NO_TRANSCRIPTS.format(title=title)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=OUTLINE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def generate_script_from_outline(outline: str, duration: str = "6-8") -> str:
    """Generate a full plain-text script from an outline, sized for the given duration."""
    min_w, max_w, dur_min = DURATION_WORD_COUNTS.get(duration, (900, 1200, 8))
    prompt = SCRIPT_FROM_OUTLINE_PROMPT.format(
        min_words=min_w,
        max_words=max_w,
        duration_min=dur_min,
        outline=outline,
    )
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        system=OUTLINE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def clean_script(text: str) -> str:
    """
    Strip ALL non-narration content from a Claude-generated script.
    Returns only the spoken narration paragraphs, ready for TTS.
    """
    # ── Step 1: Strip markdown formatting first (so patterns below see plain text) ──
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'__(.+?)__', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'\*(.+?)\*', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'_(.+?)_', r'\1', text, flags=re.DOTALL)

    # ── Step 2: Strip inline [bracket content] throughout ─────────────────
    text = re.sub(r'\[.*?\]', '', text)

    # ── Step 3: Line-by-line filtering ────────────────────────────────────
    _REMOVE_LINE_RE = re.compile(
        r'^('
        # Markdown headers
        r'#{1,6}\s'
        # Separator lines: ---, ===, ———, etc.
        r'|[-=—–]{2,}\s*$'
        # Metadata labels Claude sometimes adds
        r'|YouTube Video Script'
        r'|Runtime\b'
        r'|Words?\s*:\s*\d'
        r'|Word Count\s*:'
        r'|Estimated (Runtime|Duration)\s*:'
        r'|Total Words?\s*:'
        r'|Script\s*:\s*$'
        r'|Title\s*:\s*\S'
        r'|Topic\s*:\s*\S'
        # Stage direction keyword-only lines (nothing else on the line)
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

        # Keep blank lines as paragraph separators
        if not stripped:
            cleaned_lines.append('')
            continue

        # Drop lines matching removal patterns
        if _REMOVE_LINE_RE.match(stripped):
            continue

        cleaned_lines.append(line)

    text = '\n'.join(cleaned_lines)
    # Collapse 3+ consecutive blank lines into a single blank line
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def generate_script(topic: str, video_type: str = "top10", duration: str = "6-8") -> Dict:
    """Generate a full script adapted to video_type and duration."""
    min_s, max_s = DURATION_SCENES.get(duration, (15, 20))
    n_scenes = (min_s + max_s) // 2
    n_items = n_scenes - 1  # hook scene + n_items list items

    if video_type == "documental":
        prompt = SCRIPT_PROMPT_DOCUMENTAL.format(topic=topic, n_scenes=n_scenes)
    else:
        prompt = SCRIPT_PROMPT_TOP10.format(
            topic=topic,
            n_scenes=n_scenes,
            n_items=n_items,
            last=n_scenes,
            countdown_start=n_items,
        )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        system=SCRIPT_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return _extract_json(response.content[0].text)


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
