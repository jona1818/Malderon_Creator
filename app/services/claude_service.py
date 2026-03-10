"""AI service - script generation, image prompts, keyword extraction.
Uses OpenRouter (openai-compatible) instead of Anthropic directly.
"""
import json
import re
from pathlib import Path
from typing import Dict, List, Optional
from openai import OpenAI
from ..config import settings

# OpenRouter is OpenAI-compatible — just swap the base_url and api_key
client = OpenAI(
    api_key=settings.openrouter_api_key,
    base_url="https://openrouter.ai/api/v1",
)

# Model aliases
_MODEL_FAST  = "google/gemini-2.0-flash-lite-001"  # cheap + fast (JSON tasks, image prompts)
_MODEL_SMART = "google/gemini-2.0-flash-001"        # quality (scripts, editing)


def _chat(system: str, user: str, model: str = _MODEL_SMART, max_tokens: int = 8192) -> str:
    """Call OpenRouter with a system + user message. Returns the text response."""
    resp = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
    )
    content = resp.choices[0].message.content
    if content is None:
        raise RuntimeError("OpenRouter returned empty content (None)")
    return content.strip()

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
        "- Return ONLY the narration script as clean, flowing text.",
        "- Do NOT include scene markers, numbering, or any segmentation.",
        "- Do NOT include the outline, talking points header, or any labels.",
        "- Do NOT include: 'NARRATOR:', 'Scene:', '[Music]', '[Pause]', timestamps.",
        "- Do NOT use bold (**), italic (*), or headers (#).",
        "- The output must be pure spoken narration, ready for text-to-speech directly.",
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

RETURN ONLY THE NARRATION SCRIPT. Clean flowing text, no markers, no scene numbers. Nothing else."""

    # ── Call AI via OpenRouter ─────────────────────────────────────────────────
    return _chat(system_prompt, user_prompt, model=_MODEL_SMART, max_tokens=8192)


# ── Image prompt generation ────────────────────────────────────────────────────

IMAGE_PROMPT_SYSTEM = """You are a visual prompt engineer for cinematic AI image generation.
Create detailed, photorealistic image prompts for documentary-style YouTube videos.

CRITICAL RULES:
- Cinematic style: dark moody lighting, rich color grading, deep shadows, warm highlights.
- Camera: professional documentary cinematography (wide shots, medium close-ups, aerials).
- Lighting: dramatic natural light, golden hour, volumetric fog, rim lighting.
- NO people, NO characters, NO faces, NO human figures.
- Focus on: landscapes, architecture, objects, environments, aerial views, macro details.
- 16:9 widescreen. No text, no watermarks, no logos.
Return ONLY valid JSON - no markdown fences, no extra text."""

IMAGE_PROMPT_TEMPLATE = """Create a detailed cinematic image prompt for this video scene:

Scene narration: {narration}
Visual description: {visual_description}
Style reference: {reference_character}

Return JSON:
{{
  "image_prompt": "Detailed cinematic prompt. Include: subject, composition, lighting (dramatic/moody), camera angle/lens, color palette, mood, textures. NO people. Comma-separated descriptive terms."
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
    return _extract_json(_chat(IMAGE_PROMPT_SYSTEM, prompt, model=_MODEL_FAST, max_tokens=512))["image_prompt"]


def generate_search_keywords(narration: str, visual_description: str) -> Dict:
    prompt = KEYWORDS_TEMPLATE.format(narration=narration, visual_description=visual_description)
    return _extract_json(_chat(KEYWORDS_SYSTEM, prompt, model=_MODEL_FAST, max_tokens=256))


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
    # Strip ALL bracketed labels like [Music], [Pause], [1], etc.
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


def edit_script_with_prompt(current_script: str, user_prompt: str) -> str:
    """Use Claude to edit/revise an existing script based on the user's instruction.
    Returns the revised script as plain narration text."""
    raw = _chat(
        system=(
            "You are an expert YouTube video scriptwriter. "
            "The user will give you an existing narration script and an instruction. "
            "Apply the instruction to revise the script. "
            "Return ONLY the revised narration text, plain prose, ready for text-to-speech. "
            "Do NOT include any headers, stage directions, metadata, word counts, or markdown. "
            "Return clean flowing narration. No scene markers, no numbering."
        ),
        user=(
            f"CURRENT SCRIPT:\n\n{current_script}\n\n"
            f"INSTRUCTION: {user_prompt}\n\n"
            "Return the revised script:"
        ),
        model=_MODEL_SMART,
        max_tokens=8192,
    )
    return clean_script(raw)


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


# ── Scene division with SRT timestamps ───────────────────────────────────────

def divide_script_into_scenes(_script_text: str, srt_content: str) -> list:
    """Divide a script into scenes using Claude + SRT timestamps.

    For long videos, the SRT is split into ~60s blocks and each block is
    processed independently. Results are merged and renumbered.

    Returns list of dicts: [{"id": 1, "texto": "...", "startMs": 0, "endMs": 6500}, ...]
    """
    import sys as _sys

    srt_breakpoints = _parse_srt_breakpoints(srt_content)
    srt_blocks = _split_srt_into_blocks(srt_content, block_duration_ms=60000)

    print(f"[SceneDivision] {len(srt_blocks)} bloques de ~60s para procesar.")

    all_scenes: list = []

    for block_idx, (block_srt, block_start_ms, block_end_ms) in enumerate(srt_blocks):
        print(f"[SceneDivision] Bloque {block_idx + 1}/{len(srt_blocks)}: "
              f"{block_start_ms / 1000:.1f}s - {block_end_ms / 1000:.1f}s")

        block_scenes = _divide_srt_block(block_srt, block_start_ms, srt_breakpoints)
        all_scenes.extend(block_scenes)

    # Renumber IDs sequentially across all blocks
    for idx, s in enumerate(all_scenes, 1):
        s["id"] = idx

    if not all_scenes:
        raise RuntimeError("No se generaron escenas.")

    print(f"[SceneDivision] Total: {len(all_scenes)} escenas.")
    return all_scenes


def _split_srt_into_blocks(srt_content: str, block_duration_ms: int = 60000) -> list:
    """Split SRT content into blocks of ~block_duration_ms each.

    Always cuts at SRT entry boundaries (never mid-entry).
    Returns list of (block_srt_text, block_start_ms, block_end_ms).
    """
    # Parse all SRT entries: (start_ms, end_ms, text)
    entry_pattern = re.compile(
        r"\d+\s*\n"
        r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
        r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*\n"
        r"((?:.+\n?)+)",
        re.MULTILINE
    )

    def ts_to_ms(h, m, s, ms):
        return int(h) * 3600000 + int(m) * 60000 + int(s) * 1000 + int(ms)

    def ms_to_srt(ms):
        h = ms // 3600000; ms %= 3600000
        m = ms // 60000;   ms %= 60000
        s = ms // 1000;    ms %= 1000
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    entries = []
    for match in entry_pattern.finditer(srt_content):
        start = ts_to_ms(match.group(1), match.group(2), match.group(3), match.group(4))
        end   = ts_to_ms(match.group(5), match.group(6), match.group(7), match.group(8))
        text  = match.group(9).strip()
        entries.append((start, end, text))

    if not entries:
        return [(srt_content, 0, 0)]

    # Group entries into blocks
    blocks = []
    current_entries = []
    block_start = entries[0][0]

    for i, entry in enumerate(entries):
        current_entries.append(entry)
        block_duration = entry[1] - block_start
        if block_duration >= block_duration_ms:
            blocks.append((current_entries[:], block_start, entry[1]))
            current_entries = []
            if i + 1 < len(entries):
                block_start = entries[i + 1][0]

    # Last block (remaining entries)
    if current_entries:
        blocks.append((current_entries, block_start, current_entries[-1][1]))

    # Convert each block back to SRT text
    result = []
    for block_entries, blk_start, blk_end in blocks:
        srt_lines = []
        for i, (start, end, text) in enumerate(block_entries, 1):
            srt_lines.append(f"{i}\n{ms_to_srt(start)} --> {ms_to_srt(end)}\n{text}\n")
        result.append(("\n".join(srt_lines), blk_start, blk_end))

    return result


def _parse_srt_entries(block_srt: str) -> list:
    """Parse SRT text into list of dicts: [{idx, start, end, text}, ...]."""
    pattern = re.compile(
        r"(\d+)\s*\n"
        r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
        r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*\n"
        r"((?:.+\n?)+)",
        re.MULTILINE
    )
    entries = []
    for m in pattern.finditer(block_srt):
        start = int(m.group(2))*3600000 + int(m.group(3))*60000 + int(m.group(4))*1000 + int(m.group(5))
        end   = int(m.group(6))*3600000 + int(m.group(7))*60000 + int(m.group(8))*1000 + int(m.group(9))
        entries.append({"idx": int(m.group(1)), "start": start, "end": end, "text": m.group(10).strip()})
    return entries


def _divide_srt_block(block_srt: str, block_start_ms: int, srt_breakpoints: list) -> list:
    """Divide one ~60s SRT block into scenes using AI for grouping.

    Key insight: we DON'T ask the AI for timestamps. Instead we ask it to
    group SRT entries by number, then derive timestamps from the real SRT data.
    This avoids timestamp format issues across different AI models.
    """
    import time as _time

    entries = _parse_srt_entries(block_srt)
    if not entries:
        return []

    FIRST_MINUTE_MS = 60000
    is_zone1 = block_start_ms < FIRST_MINUTE_MS
    target = "2-4 seconds (fast cuts)" if is_zone1 else "5-8 seconds"

    # Build numbered list with durations for the AI
    entry_descs = []
    for e in entries:
        dur = (e["end"] - e["start"]) / 1000
        entry_descs.append(f"Entry {e['idx']}: ({dur:.1f}s) \"{e['text']}\"")

    system_prompt = (
        "You group subtitle entries into video scenes for AI image generation. "
        "Each scene = one visual shot. Return ONLY a valid JSON array of [first, last] entry ranges. "
        "No markdown fences, no explanation."
    )

    user_prompt = (
        f"Group these {len(entries)} subtitle entries into scenes of {target} each.\n\n"
        + "\n".join(entry_descs) +
        "\n\nRules:\n"
        f"- Target duration per scene: {target}.\n"
        "- HARD MAXIMUM: 8 seconds per scene. Never exceed this.\n"
        "- Each scene must represent ONE clear visual idea (for AI image generation).\n"
        "- Group consecutive entries only.\n"
        "- Every entry must be in exactly one scene.\n"
        "- Prefer splitting at sentence boundaries (after periods).\n"
        "- If a single entry exceeds 8s, it must be its own scene.\n\n"
        "Return ONLY JSON like: [[1,2], [3,3], [4,5]]"
    )

    MAX_ATTEMPTS = 2
    last_scenes = None
    messages = [{"role": "user", "content": user_prompt}]

    for attempt in range(MAX_ATTEMPTS):
        raw = ""
        try:
            resp = client.chat.completions.create(
                model=_MODEL_FAST,
                max_tokens=1024,
                messages=[{"role": "system", "content": system_prompt}] + messages,
            )
            raw = resp.choices[0].message.content.strip()
            raw_clean = re.sub(r"^```(?:json)?\s*", "", raw)
            raw_clean = re.sub(r"\s*```$", "", raw_clean)
            ranges = json.loads(raw_clean)

            if not isinstance(ranges, list):
                raise ValueError(f"Expected JSON array, got {type(ranges).__name__}")

            # Build scenes from entry ranges
            scenes = []
            for r in ranges:
                if isinstance(r, list) and len(r) >= 2:
                    first, last = int(r[0]), int(r[1])
                elif isinstance(r, dict):
                    first = int(r.get("first") or r.get("start") or r.get("from", 0))
                    last = int(r.get("last") or r.get("end") or r.get("to", 0))
                else:
                    continue

                scene_entries = [e for e in entries if first <= e["idx"] <= last]
                if not scene_entries:
                    continue

                scenes.append({
                    "id": len(scenes) + 1,
                    "texto": " ".join(e["text"] for e in scene_entries),
                    "startMs": scene_entries[0]["start"],
                    "endMs": scene_entries[-1]["end"],
                })

            if not scenes:
                raise ValueError("No valid scenes produced from ranges")

            last_scenes = scenes
            _validate_scenes(scenes)
            return scenes

        except Exception as exc:
            print(f"[SceneDivision] Block {block_start_ms}ms attempt {attempt+1}/{MAX_ATTEMPTS} error: {exc}")
            if attempt < MAX_ATTEMPTS - 1:
                messages = [
                    {"role": "user", "content": user_prompt},
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": f"ERROR: {exc}\nFix and return corrected JSON ranges."},
                ]
                _time.sleep(1)

    # Fallback 1: accept partial result if scenes respect 8s limit
    MAX_FALLBACK_MS = 8500
    if last_scenes:
        valid = [s for s in last_scenes
                 if s["endMs"] > s["startMs"] and (s["endMs"] - s["startMs"]) <= MAX_FALLBACK_MS]
        if len(valid) >= len(last_scenes) * 0.7:  # at least 70% pass
            print(f"[WARNING] Block {block_start_ms}ms: using partial result ({len(valid)} scenes).")
            return valid

    # Fallback 2: group entries greedily up to 8s — always has real text
    MAX_SCENE_FALLBACK = 8000
    print(f"[WARNING] Block {block_start_ms}ms: using programmatic grouping (max {MAX_SCENE_FALLBACK}ms).")
    scenes = []
    i = 0
    while i < len(entries):
        grp = [entries[i]]
        j = i + 1
        while j < len(entries):
            combined_dur = entries[j]["end"] - grp[0]["start"]
            if combined_dur > MAX_SCENE_FALLBACK:
                break
            grp.append(entries[j])
            j += 1
        scenes.append({
            "id": len(scenes) + 1,
            "texto": " ".join(e["text"] for e in grp),
            "startMs": grp[0]["start"],
            "endMs": grp[-1]["end"],
        })
        i = j
    return scenes


def _ts_str_to_ms(ts: str) -> int:
    """Convert timestamp string '00:01:23,456' or '00:01:23.456' to milliseconds."""
    if not ts:
        return 0
    ts = ts.strip().replace(",", ".")
    parts = ts.split(":")
    try:
        if len(parts) == 3:
            h, m, s = parts
            return int(h) * 3600000 + int(m) * 60000 + round(float(s) * 1000)
        if len(parts) == 2:
            m, s = parts
            return int(m) * 60000 + round(float(s) * 1000)
    except (ValueError, TypeError):
        pass
    return 0


def _normalize_scene(scene: dict, idx: int) -> dict:
    """Normalize any AI response format to {id, texto, startMs, endMs}."""
    # id
    scene_id = scene.get("id") or idx + 1
    # text — accept varios nombres de campo
    texto = (scene.get("texto") or scene.get("text") or scene.get("narration")
             or scene.get("content") or scene.get("script") or "")
    # startMs — acepta entero o string timestamp
    start_ms = scene.get("startMs") or scene.get("start_ms")
    if start_ms is None:
        start_ms = _ts_str_to_ms(scene.get("start") or scene.get("startTime") or "")
    # endMs
    end_ms = scene.get("endMs") or scene.get("end_ms")
    if end_ms is None:
        end_ms = _ts_str_to_ms(scene.get("end") or scene.get("endTime") or "")
    return {"id": int(scene_id), "texto": str(texto), "startMs": int(start_ms), "endMs": int(end_ms)}


def _parse_scenes_json(raw: str) -> list:
    """Parse AI JSON response, normalize to expected format regardless of field names."""
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    scenes = json.loads(raw)
    if not isinstance(scenes, list):
        raise ValueError(f"Expected JSON array, got {type(scenes).__name__}")
    return [_normalize_scene(s, i) for i, s in enumerate(scenes)]


def _parse_srt_breakpoints(srt_content: str) -> list:
    """Extract all unique timestamp breakpoints (in ms) from an SRT file."""
    pattern = re.compile(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})")
    breakpoints = set()
    for m in pattern.finditer(srt_content):
        h, mi, s, ms = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        breakpoints.add(h * 3600000 + mi * 60000 + s * 1000 + ms)
    return sorted(breakpoints)


def _force_split_oversized(scenes: list, srt_breakpoints: list, max_ms: int = 10000) -> list:
    """Programmatically split any scene exceeding max_ms at the nearest SRT breakpoint.

    This is a safety net: if Claude returns scenes > max_ms, we force-split them
    using real SRT timestamps so the result respects the limit.
    Text is split proportionally by time position.
    """
    result = []
    for scene in scenes:
        duration = scene["endMs"] - scene["startMs"]
        if duration <= max_ms:
            result.append(scene)
            continue

        # Find SRT breakpoints within this scene
        start, end = scene["startMs"], scene["endMs"]
        candidates = [bp for bp in srt_breakpoints if start < bp < end]

        if not candidates:
            # No SRT breakpoint inside — force split at midpoint
            mid = start + duration // 2
            candidates = [mid]

        # Greedily split: take the largest chunk ≤ max_ms from the left
        words = scene["texto"].split()
        total_words = len(words)
        boundaries = [start] + candidates + [end]
        sub_scenes = []
        seg_start = start
        seg_word_start = 0

        i = 1
        while i < len(boundaries):
            seg_end = boundaries[i]
            seg_dur = seg_end - seg_start

            # If this segment is within limit, try extending to next boundary
            if seg_dur <= max_ms and i + 1 < len(boundaries):
                next_dur = boundaries[i + 1] - seg_start
                if next_dur <= max_ms:
                    i += 1
                    continue

            # Commit this segment
            # Proportional word allocation
            time_fraction = (seg_end - seg_start) / max(duration, 1)
            word_count = max(1, round(time_fraction * total_words))
            seg_word_end = min(seg_word_start + word_count, total_words)

            # Ensure last segment gets remaining words
            if i == len(boundaries) - 1 or seg_word_end >= total_words:
                seg_word_end = total_words

            seg_text = " ".join(words[seg_word_start:seg_word_end])
            if seg_text.strip():
                sub_scenes.append({
                    "id": 0,  # renumbered below
                    "texto": seg_text,
                    "startMs": seg_start,
                    "endMs": seg_end,
                })

            seg_word_start = seg_word_end
            seg_start = seg_end
            i += 1

        # If no sub-scenes were created (edge case), keep original
        if sub_scenes:
            result.extend(sub_scenes)
        else:
            result.append(scene)

    # Renumber all scene IDs sequentially
    for idx, s in enumerate(result, 1):
        s["id"] = idx

    return result


def _validate_scenes(scenes: list) -> None:
    """Validate the scene division JSON.

    Enforces:
    - Hard max 8000ms (8s) for all scenes + 500ms tolerance for SRT rounding
    - No negative timestamps, no empty text, consecutive timestamps
    Scenes between 8000-8500ms are accepted with a console warning.
    """
    import sys as _sys

    MAX_SCENE_MS = 8500   # 8s + 500ms tolerance for SRT rounding
    WARN_SCENE_MS = 8000
    MIN_HARD_MS = 500     # absolute minimum — reject below this

    if not scenes:
        raise ValueError("Empty scenes list")

    required_keys = {"id", "texto", "startMs", "endMs"}
    oversized = []
    warnings = []

    for i, scene in enumerate(scenes):
        missing = required_keys - set(scene.keys())
        if missing:
            raise ValueError(f"Scene {i+1} missing keys: {missing}")

        if scene["startMs"] < 0 or scene["endMs"] < 0:
            raise ValueError(f"Scene {scene['id']} has negative timestamps")

        if scene["endMs"] <= scene["startMs"]:
            raise ValueError(f"Scene {scene['id']} has zero or negative duration")

        duration_ms = scene["endMs"] - scene["startMs"]

        # Absolute hard max — reject
        if duration_ms > MAX_SCENE_MS:
            oversized.append(
                f"Scene {scene['id']}: {duration_ms}ms ({duration_ms/1000:.1f}s) "
                f"at {scene['startMs']}-{scene['endMs']}ms"
            )
        # Warning zone (8-10s) — accept but log warning
        elif duration_ms > WARN_SCENE_MS:
            warnings.append(
                f"Scene {scene['id']}: {duration_ms}ms ({duration_ms/1000:.1f}s) [long]"
            )

        # Min duration — only reject extremely short (<500ms), warn otherwise
        if duration_ms < MIN_HARD_MS:
            raise ValueError(f"Scene {scene['id']} is too short ({duration_ms}ms)")
        elif duration_ms < 2000:
            warnings.append(
                f"Scene {scene['id']}: {duration_ms}ms ({duration_ms/1000:.1f}s) [short]"
            )

        if not scene["texto"].strip():
            raise ValueError(f"Scene {scene['id']} has empty text")

    # Log warnings (accepted, not rejected)
    if warnings:
        try:
            _sys.stdout.buffer.write(
                f"[WARNING] {len(warnings)} scene(s) fuera de rango ideal (aceptadas): "
                f"{', '.join(warnings)}\n".encode("utf-8", errors="replace")
            )
            _sys.stdout.buffer.flush()
        except Exception:
            pass

    # Only reject scenes exceeding absolute 10s limit
    if oversized:
        raise ValueError(
            f"{len(oversized)} scene(s) exceed {MAX_SCENE_MS}ms absolute limit:\n"
            + "\n".join(oversized)
        )

    # Check sequential continuity — auto-fix small gaps (<100ms)
    for i in range(1, len(scenes)):
        gap = abs(scenes[i]["startMs"] - scenes[i-1]["endMs"])
        if gap > 0 and gap < 100:
            scenes[i]["startMs"] = scenes[i-1]["endMs"]
        elif gap >= 100:
            raise ValueError(
                f"Gap of {gap}ms between scene {scenes[i-1]['id']} "
                f"(ends {scenes[i-1]['endMs']}ms) and scene {scenes[i]['id']} "
                f"(starts {scenes[i]['startMs']}ms)"
            )
