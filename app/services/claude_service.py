"""AI service - script generation, image prompts, keyword extraction.
Uses OpenRouter (openai-compatible) instead of Anthropic directly.
"""
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional
from openai import OpenAI
from ..config import settings


def _safe_print(msg: str) -> None:
    """Print that won't crash on Windows when stdout is invalid/piped."""
    try:
        sys.stdout.buffer.write((msg + "\n").encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()
    except Exception:
        pass

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

_MODEL_HAIKU = "anthropic/claude-haiku-4.5"
_SCENE_CHUNK_WORDS = 3000  # split script into chunks of this many words for long videos


def _split_srt_into_blocks(srt_content: str, block_duration_ms: int = 60000) -> list:
    """Split SRT content into blocks of ~block_duration_ms each.

    Always cuts at SRT entry boundaries. Returns list of (block_srt_text, start_ms, end_ms).
    """
    def ts_to_ms(h, m, s, ms):
        return int(h)*3600000 + int(m)*60000 + int(s)*1000 + int(ms)

    def ms_to_srt(ms):
        h = ms // 3600000; ms %= 3600000
        m = ms // 60000;   ms %= 60000
        s = ms // 1000;    ms %= 1000
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    pattern = re.compile(
        r"\d+\s*\n"
        r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
        r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*\n"
        r"((?:.+\n?)+)",
        re.MULTILINE
    )
    entries = []
    for m in pattern.finditer(srt_content):
        start = ts_to_ms(m.group(1), m.group(2), m.group(3), m.group(4))
        end   = ts_to_ms(m.group(5), m.group(6), m.group(7), m.group(8))
        entries.append((start, end, m.group(9).strip()))

    if not entries:
        return [(srt_content, 0, 0)]

    blocks = []
    current = []
    block_start = entries[0][0]
    for i, entry in enumerate(entries):
        current.append(entry)
        if entry[1] - block_start >= block_duration_ms:
            blocks.append((current[:], block_start, entry[1]))
            current = []
            if i + 1 < len(entries):
                block_start = entries[i + 1][0]
    if current:
        blocks.append((current, block_start, current[-1][1]))

    result = []
    for block_entries, blk_start, blk_end in blocks:
        srt_lines = [
            f"{i}\n{ms_to_srt(s)} --> {ms_to_srt(e)}\n{t}\n"
            for i, (s, e, t) in enumerate(block_entries, 1)
        ]
        result.append(("\n".join(srt_lines), blk_start, blk_end))
    return result


def divide_script_into_scenes(_script_text: str, srt_content: str, mode: str = "animated") -> list:
    """Divide a script into scenes using Claude Haiku + word-level SRT timestamps.

    Approach:
      1. Parse SRT → build word-level timestamps (interpolate within each entry)
      2. Extract full continuous text from SRT
      3. For long videos (>_SCENE_CHUNK_WORDS), split into ~60s SRT blocks and
         process each block independently; otherwise process all at once
      4. Send text to Haiku → get list of scene text strings
      5. Post-process: merge short scenes (<3s) + split long scenes (>7s)
      6. Map scene texts back to word-level timestamps

    Returns list of dicts: [{"id": 1, "texto": "...", "startMs": 0, "endMs": 6500}, ...]
    """
    entries = _parse_srt_entries_full(srt_content)
    if not entries:
        raise RuntimeError("No SRT entries found.")

    word_ts = _build_word_timestamps(entries)
    full_text = " ".join(e["text"] for e in entries)
    total_duration_ms = entries[-1]["end"]
    total_duration_s = total_duration_ms / 1000
    total_words = len(full_text.split())
    wps = total_words / total_duration_s if total_duration_s > 0 else 2.5

    _safe_print(f"[SceneDivision] mode={mode}, {total_words} palabras, {total_duration_s:.1f}s, {wps:.1f} wps")

    if total_words <= _SCENE_CHUNK_WORDS:
        # Short video — process everything in one call
        scene_texts = _divide_text_with_haiku(full_text, total_duration_s, wps, mode)
        _safe_print(f"[SceneDivision] Haiku devolvió {len(scene_texts)} escenas (un solo bloque)")
        scene_texts = _postprocess_scenes(scene_texts, wps, mode)
        all_scenes = _map_scenes_to_timestamps(scene_texts, word_ts)
    else:
        # Long video — split SRT into ~60s blocks, process each, merge
        srt_blocks = _split_srt_into_blocks(srt_content, block_duration_ms=60000)
        _safe_print(f"[SceneDivision] {len(srt_blocks)} bloques de ~60s")
        all_scenes = []

        for block_idx, (block_srt, block_start_ms, block_end_ms) in enumerate(srt_blocks):
            _safe_print(f"[SceneDivision] Bloque {block_idx + 1}/{len(srt_blocks)}: "
                        f"{block_start_ms / 1000:.1f}s - {block_end_ms / 1000:.1f}s")

            block_entries = _parse_srt_entries_full(block_srt)
            if not block_entries:
                continue

            block_word_ts = _build_word_timestamps(block_entries)
            block_text = " ".join(e["text"] for e in block_entries)
            block_dur_s = (block_end_ms - block_start_ms) / 1000
            block_words = len(block_text.split())
            block_wps = block_words / block_dur_s if block_dur_s > 0 else wps

            block_scene_texts = _divide_text_with_haiku(block_text, block_dur_s, block_wps, mode)
            block_scene_texts = _postprocess_scenes(block_scene_texts, block_wps, mode)
            block_scenes = _map_scenes_to_timestamps(block_scene_texts, block_word_ts)
            all_scenes.extend(block_scenes)

    # Renumber IDs sequentially
    for idx, s in enumerate(all_scenes, 1):
        s["id"] = idx

    if not all_scenes:
        raise RuntimeError("No se generaron escenas.")

    _safe_print(f"[SceneDivision] Total: {len(all_scenes)} escenas.")
    return all_scenes


def _parse_srt_entries_full(srt_text: str) -> list:
    """Parse SRT text into list of dicts: [{idx, start, end, text}, ...]."""
    pattern = re.compile(
        r"(\d+)\s*\n"
        r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
        r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*\n"
        r"((?:.+\n?)+)",
        re.MULTILINE,
    )
    entries = []
    for m in pattern.finditer(srt_text):
        start = int(m.group(2))*3600000 + int(m.group(3))*60000 + int(m.group(4))*1000 + int(m.group(5))
        end   = int(m.group(6))*3600000 + int(m.group(7))*60000 + int(m.group(8))*1000 + int(m.group(9))
        entries.append({"idx": int(m.group(1)), "start": start, "end": end, "text": m.group(10).strip()})
    return entries


def _build_word_timestamps(entries: list) -> list:
    """Build word-level timestamps by distributing time evenly across words in each SRT entry.

    Returns: [{"word": "Hola", "start_ms": 0, "end_ms": 250}, ...]
    """
    words = []
    for e in entries:
        entry_words = e["text"].split()
        if not entry_words:
            continue
        duration = e["end"] - e["start"]
        per_word = duration / len(entry_words)
        for i, w in enumerate(entry_words):
            words.append({
                "word": w,
                "start_ms": e["start"] + int(i * per_word),
                "end_ms": e["start"] + int((i + 1) * per_word),
            })
    return words


def _divide_text_with_haiku(full_text: str, total_duration_s: float, wps: float,
                            mode: str = "animated") -> list:
    """Send continuous narration text to Claude Haiku. Returns list of scene text strings."""
    total_words = len(full_text.split())
    print(f"[_divide_text_with_haiku] mode={mode}, words={total_words}, dur={total_duration_s:.1f}s")
    print(f"[_divide_text_with_haiku] STOCK PROMPT: {'YES' if mode == 'stock' else 'NO (animated)'}")

    system_prompt = (
        "Eres un editor de video profesional. Dividís texto narrado en escenas visuales. "
        "Devolvé SOLO un JSON array de strings con el texto de cada escena. "
        "Sin markdown, sin explicación, sin texto extra."
    )

    if mode == "stock":
        user_prompt = (
            f"Dividí este texto en escenas visuales para un video.\n\n"
            f"TEXTO COMPLETO:\n{full_text}\n\n"
            f"DURACIÓN TOTAL: {total_duration_s:.1f}s\n"
            f"TOTAL PALABRAS: {total_words}\n"
            f"VELOCIDAD: {wps:.1f} palabras por segundo\n\n"
            "Cada escena debe ser UNA idea visual que pueda representarse con una sola imagen o video.\n\n"
            "=== REGLAS ===\n"
            "1. Cada escena debe tener sentido completo por sí sola como imagen visual.\n"
            "2. Cortá en comas, puntos, punto y coma, o cualquier pausa natural donde cambie la imagen mental.\n"
            "3. NUNCA cortes a mitad de una idea. Si decís 'we're talking about' la siguiente parte "
            "DEBE estar en la misma escena o la escena no tiene sentido visual.\n"
            "4. NUNCA termines una escena en preposición (about, of, to, for, in, on, with) o "
            "conjunción (and, or, that, which) sin completar la idea.\n"
            "5. Cada escena debe poder responderse: ¿qué imagen pondrías aquí? "
            "Si no podés imaginar una imagen clara para la escena, está mal cortada.\n"
            "6. Duración ideal: 2-6 segundos por escena. Máximo absoluto: 10 segundos.\n"
            f"   (A {wps:.1f} palabras/segundo, 2s = ~{int(2 * wps)} palabras, 6s = ~{int(6 * wps)} palabras)\n"
            "7. No hay mínimo estricto de palabras. Una escena de 3 palabras es válida si tiene "
            "sentido visual completo.\n\n"
            "=== EJEMPLO ===\n"
            "Texto: \"Phoenix, Arizona, a desert city, is experiencing an economic transformation "
            "unlike anything seen in the US in decades. We're not just talking about a few new "
            "buildings; we're talking about a $200 billion boom driven by semiconductor factories, "
            "data centers, and urban revitalization.\"\n\n"
            "BIEN dividido:\n"
            "[\n"
            '  "Phoenix, Arizona, a desert city,",\n'
            '  "is experiencing an economic transformation unlike anything seen in the US in decades.",\n'
            '  "We\'re not just talking about a few new buildings;",\n'
            '  "we\'re talking about a $200 billion boom",\n'
            '  "driven by semiconductor factories, data centers, and urban revitalization."\n'
            "]\n\n"
            "MAL dividido:\n"
            '- "is experiencing an economic" ← cortado a mitad de idea, no hay imagen posible\n'
            '- "a $200 billion boom driven by semiconductor factories, data centers, and urban '
            'revitalization. This could make Phoenix the most important" ← dos ideas distintas mezcladas\n'
            '- "or a cautionary tale of" ← preposición colgando sin completar la idea\n\n'
            "=== OUTPUT ===\n"
            "Devolvé SOLO el JSON array de strings. "
            "Todo el texto original debe estar presente, sin omitir ni repetir nada.\n"
            '[\"texto escena 1\", \"texto escena 2\", ...]'
        )
    else:
        # Animated mode — original prompt
        target_min, target_max, abs_max = 3, 5, 6
        user_prompt = (
            f"Dividí este texto narrado en escenas visuales para un video.\n\n"
            f"TEXTO COMPLETO:\n{full_text}\n\n"
            f"DURACIÓN TOTAL: {total_duration_s:.1f}s\n"
            f"TOTAL PALABRAS: {total_words}\n"
            f"VELOCIDAD: {wps:.1f} palabras por segundo\n\n"
            "=== REGLAS ===\n"
            f"1. Cada escena debe durar entre {target_min} y {target_max} segundos.\n"
            f"   (A {wps:.1f} palabras/segundo, eso es ~{int(target_min * wps)}-{int(target_max * wps)} palabras por escena)\n"
            f"2. MÁXIMO ABSOLUTO: {abs_max} segundos. NUNCA superar {abs_max} segundos por escena.\n"
            "3. Calculá la duración estimada de cada escena contando sus palabras y dividiéndolas por la velocidad.\n"
            "4. NUNCA cortes a mitad de frase o palabra.\n"
            "5. Cortá preferentemente en puntos (.), signos de exclamación (!) o signos de interrogación (?).\n"
            f"6. Si una oración dura más de {target_max} segundos, DEBÉS dividirla en un punto medio natural como una coma "
            "entre cláusulas. En ese caso la coma SÍ es un punto de corte válido. "
            f"La regla de no cortar en comas aplica solo para oraciones que caben en {target_max} segundos.\n"
            "7. NUNCA dejes una escena con menos de 4 palabras.\n"
            f"8. Si dos frases cortas consecutivas duran menos de {target_min} segundos cada una, agrupalas en una sola escena.\n"
            "9. Cada escena debe representar UNA idea visual completa.\n\n"
            "=== EJEMPLO ===\n"
            "Texto: \"Objetos que estuvieron en contacto directo con el cuerpo de Cristo "
            "aún existen hoy, custodiados en catedrales, bóvedas seguras y museos "
            "de todo el mundo. No son solo leyendas o cuentos medievales, sino "
            "reliquias físicas con siglos de historia documentada, analizadas por "
            "científicos modernos y veneradas por millones de peregrinos.\"\n\n"
            "BIEN dividido:\n"
            "[\n"
            "  \"Objetos que estuvieron en contacto directo con el cuerpo de Cristo aún existen hoy,\",\n"
            "  \"custodiados en catedrales, bóvedas seguras y museos de todo el mundo.\",\n"
            "  \"No son solo leyendas o cuentos medievales,\",\n"
            "  \"sino reliquias físicas con siglos de historia documentada,\",\n"
            "  \"analizadas por científicos modernos y veneradas por millones de peregrinos.\"\n"
            "]\n\n"
            "MAL dividido:\n"
            "- Una oración de 8 palabras en una sola escena cuando podría dividirse en coma\n"
            "- Repetir texto entre escenas\n"
            "- Cortar a mitad de frase sin puntuación\n\n"
            "=== OUTPUT ===\n"
            "Devolvé SOLO el JSON array de strings. "
            "Todo el texto original debe estar presente, sin omitir ni repetir nada.\n"
            "[\"texto escena 1\", \"texto escena 2\", ...]"
        )

    # Debug: confirm which prompt is being sent
    _has_visual_rules = "NUNCA termines una escena en preposición" in user_prompt
    print(f"[_divide_text_with_haiku] Prompt has visual-coherence rules: {_has_visual_rules}")
    print(f"[_divide_text_with_haiku] Prompt first 300 chars: {user_prompt[:300]}")

    resp = client.chat.completions.create(
        model=_MODEL_HAIKU,
        max_tokens=16000,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    raw = resp.choices[0].message.content.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    scenes = json.loads(raw)

    if not isinstance(scenes, list) or not all(isinstance(s, str) for s in scenes):
        raise ValueError(f"Expected JSON array of strings, got: {type(scenes)}")

    return scenes


def _postprocess_scenes(scene_texts: list, wps: float, mode: str = "animated") -> list:
    """Post-process scene texts: merge short, split long, validate.

    Runs merge→split→merge cycles until all scenes are 4+ words and within max duration.
    """
    MAX_DUR = 10.0 if mode == "stock" else 6.0
    min_words = 4

    scenes = list(scene_texts)

    # Run up to 10 full cycles of merge+split until everything is clean
    for _cycle in range(10):
        # ── MERGE: absorb any scene with <4 words into its neighbor ──────
        scenes = _merge_short_scenes(scenes, min_words)

        # ── SPLIT: break any scene >6s ───────────────────────────────────
        scenes = _split_long_scenes(scenes, wps, MAX_DUR, min_words)

        # ── CHECK: are we done? ──────────────────────────────────────────
        has_short = any(len(t.split()) < min_words for t in scenes)
        has_long = any(len(t.split()) / wps > MAX_DUR for t in scenes)

        if not has_short and not has_long:
            break

        # If only long scenes remain that can't be split, stop
        if not has_short and has_long:
            # One more attempt with brute-force splits
            scenes = _force_split_long_scenes(scenes, wps, MAX_DUR, min_words)
            scenes = _merge_short_scenes(scenes, min_words)
            break

    # Final safety log
    for i, t in enumerate(scenes):
        wc = len(t.split())
        dur = wc / wps
        if wc < min_words:
            _safe_print(f"[WARNING] Scene {i+1}: {wc} words (< {min_words}): {t[:60]}")
        if dur > MAX_DUR:
            _safe_print(f"[WARNING] Scene {i+1}: {dur:.1f}s (> {MAX_DUR}s): {t[:60]}")

    return scenes


def _merge_short_scenes(scenes: list, min_words: int) -> list:
    """Merge any scene with fewer than min_words into its neighbor.
    Repeats until no short scenes remain."""
    for _pass in range(10):
        result = []
        changed = False
        i = 0
        while i < len(scenes):
            text = scenes[i]
            wc = len(text.split())
            if wc < min_words and len(scenes) > 1:
                changed = True
                if i + 1 < len(scenes):
                    # Merge into next
                    scenes[i + 1] = text.rstrip() + " " + scenes[i + 1].lstrip()
                    i += 1
                    continue
                elif result:
                    # Last scene — merge into previous
                    result[-1] = result[-1].rstrip() + " " + text.lstrip()
                    i += 1
                    continue
            result.append(text)
            i += 1
        scenes = result
        if not changed:
            break
    return scenes


def _split_long_scenes(scenes: list, wps: float, max_dur: float, min_words: int) -> list:
    """Split scenes exceeding max_dur at natural boundaries. Up to 5 passes."""
    for _ in range(5):
        result = []
        changed = False
        for text in scenes:
            words = text.split()
            if len(words) / wps <= max_dur:
                result.append(text)
                continue
            parts = _try_split_scene(text, words, min_words)
            if parts:
                result.extend(parts)
                changed = True
            else:
                result.append(text)
        scenes = result
        if not changed:
            break
    return scenes


def _force_split_long_scenes(scenes: list, wps: float, max_dur: float, min_words: int) -> list:
    """Brute-force split any remaining >max_dur scenes at word midpoint."""
    result = []
    for text in scenes:
        words = text.split()
        if len(words) / wps <= max_dur:
            result.append(text)
            continue
        # Try smart split first
        parts = _try_split_scene(text, words, min_words)
        if parts:
            result.extend(parts)
            continue
        # Brute force: split at word midpoint
        mid_w = len(words) // 2
        if mid_w >= min_words and (len(words) - mid_w) >= min_words:
            result.append(" ".join(words[:mid_w]))
            result.append(" ".join(words[mid_w:]))
        else:
            result.append(text)
    return result


def _try_split_scene(text: str, words: list, min_words: int = 4) -> list | None:
    """Try to split a scene text at a natural boundary. Returns [left, right] or None."""
    mid = len(text) // 2

    # 1. Period nearest to midpoint
    for radius in range(len(text) // 2):
        for pos in [mid - radius, mid + radius]:
            if 0 <= pos < len(text) and text[pos] == '.':
                left, right = text[:pos + 1].strip(), text[pos + 1:].strip()
                if len(left.split()) >= min_words and len(right.split()) >= min_words:
                    return [left, right]

    # 2. Comma between 30-70%
    lo, hi = int(len(text) * 0.30), int(len(text) * 0.70)
    best_comma, best_dist = None, float("inf")
    for pos in range(lo, hi):
        if text[pos] == ',':
            left, right = text[:pos + 1].strip(), text[pos + 1:].strip()
            if len(left.split()) >= min_words and len(right.split()) >= min_words:
                dist = abs(pos - mid)
                if dist < best_dist:
                    best_dist, best_comma = dist, pos
    if best_comma is not None:
        return [text[:best_comma + 1].strip(), text[best_comma + 1:].strip()]

    # 3. Clause connector nearest to midpoint (30-70%)
    CONNECTORS = {"y", "que", "pero", "porque", "donde", "cuando", "sino", "ni", "o"}
    mid_w = len(words) // 2
    lo_w, hi_w = int(len(words) * 0.30), int(len(words) * 0.70)
    best_pos, best_dist = None, float("inf")
    for wi in range(lo_w, hi_w):
        if words[wi].lower().strip(".,;:!?") in CONNECTORS:
            dist = abs(wi - mid_w)
            if dist < best_dist and len(words[:wi]) >= min_words and len(words[wi:]) >= min_words:
                best_dist, best_pos = dist, wi
    if best_pos is not None:
        return [" ".join(words[:best_pos]), " ".join(words[best_pos:])]

    return None


def _map_scenes_to_timestamps(scene_texts: list, word_timestamps: list) -> list:
    """Map scene texts to timestamps by sequential word counting.

    Walks through scene texts and word_timestamps in parallel, assigning
    startMs/endMs from interpolated word positions.
    """
    scenes = []
    word_idx = 0
    total_words = len(word_timestamps)

    for scene_num, scene_text in enumerate(scene_texts, 1):
        scene_words = scene_text.split()
        if not scene_words:
            continue

        start_idx = word_idx
        end_idx = min(start_idx + len(scene_words), total_words)

        if start_idx >= total_words:
            last_ts = word_timestamps[-1]["end_ms"] if word_timestamps else 0
            scenes.append({"id": scene_num, "texto": scene_text,
                           "startMs": last_ts, "endMs": last_ts})
            continue

        start_ms = word_timestamps[start_idx]["start_ms"]
        end_ms = word_timestamps[min(end_idx - 1, total_words - 1)]["end_ms"]

        scenes.append({"id": scene_num, "texto": scene_text,
                       "startMs": start_ms, "endMs": end_ms})
        word_idx = end_idx

    return scenes
