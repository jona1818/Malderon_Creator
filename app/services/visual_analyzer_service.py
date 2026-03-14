"""Visual Analyzer — uses Claude Haiku to decide what type of visual each scene needs.

Also provides image validation: checks if a downloaded image actually matches the scene.
"""

import base64
import json
import re
import sys
from pathlib import Path
from typing import List, Dict
from openai import OpenAI
from ..config import settings


def _safe_print(msg: str) -> None:
    try:
        sys.stdout.buffer.write((msg + "\n").encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()
    except Exception:
        pass


_client = OpenAI(api_key=settings.openrouter_api_key, base_url="https://openrouter.ai/api/v1")
_MODEL = "anthropic/claude-haiku-4.5"


# ── Image validation with Vision ────────────────────────────────────────────

_VISION_MODEL = "google/gemini-2.0-flash-001"  # Cheap & fast for image validation


def validate_image(
    image_path: str | Path,
    scene_text: str,
    search_query: str,
    project_title: str = "",
) -> bool:
    """Check if a downloaded image is relevant to the scene using Gemini Flash Vision.

    Returns True if the image is relevant, False if not.
    Fails open (returns True) on any error to avoid blocking the pipeline.
    """
    try:
        image_path = Path(image_path)
        if not image_path.exists() or image_path.stat().st_size < 1000:
            return True  # Can't validate, accept it

        # Read and base64 encode the image
        img_bytes = image_path.read_bytes()
        # Limit to ~500KB to keep token costs low
        if len(img_bytes) > 500_000:
            _safe_print(f"[Validate] Image too large ({len(img_bytes)}B), skipping validation")
            return True

        img_b64 = base64.b64encode(img_bytes).decode("utf-8")

        # Detect mime type from extension
        ext = image_path.suffix.lower()
        mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
        mime_type = mime_map.get(ext, "image/jpeg")

        context = f'This is for a video titled: "{project_title}". ' if project_title else ""
        prompt = (
            f'{context}\n'
            f'The scene narration says: "{scene_text[:300]}"\n'
            f'We searched for: "{search_query}"\n\n'
            f'Look at this image carefully. Does it SPECIFICALLY match what the scene is talking about?\n'
            f'- If the video is about a specific movie/person/event, the image MUST show something from that movie/person/event.\n'
            f'- Generic images, illustrations, cartoons, or unrelated content = NO.\n'
            f'- Real photos/screenshots that match the specific topic = YES.\n\n'
            f'Answer ONLY "YES" or "NO".'
        )

        resp = _client.chat.completions.create(
            model=_VISION_MODEL,
            max_tokens=10,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{img_b64}"}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        answer = resp.choices[0].message.content.strip().upper()
        is_relevant = "YES" in answer
        _safe_print(f"[Validate] Image {'APPROVED' if is_relevant else 'REJECTED'} for query='{search_query}' (answer={answer})")
        return is_relevant

    except Exception as exc:
        _safe_print(f"[Validate] Error (accepting image): {exc}")
        return True  # Fail open — don't block pipeline on validation errors


def analyze_scenes(
    full_script: str, scenes: List[Dict], collection: str = "general",
    allowed_types: list | None = None,
    project_title: str = "",
) -> List[Dict]:
    """Analyze each scene and decide asset_type + search_query.

    Args:
        full_script: complete narration text (for context)
        scenes: list of dicts with at least 'id' and 'texto'
        collection: project collection name (e.g. 'cine', 'tech') for context
        allowed_types: if set, only these types can be used
        project_title: title of the video project (for search context)

    Returns:
        list of dicts per scene: scene_id, asset_type, search_query, search_query_alt,
        has_overlay_text, overlay_text
    """
    # Process in blocks of 15 scenes
    all_results = []
    for i in range(0, len(scenes), 15):
        block = scenes[i:i + 15]
        block_results = _analyze_block(full_script, block, collection, allowed_types, project_title)
        all_results.extend(block_results)
    return all_results


def _analyze_block(
    full_script: str, scenes: List[Dict], collection: str = "general",
    allowed_types: list | None = None, project_title: str = "",
) -> List[Dict]:
    """Analyze a block of up to 15 scenes."""
    scenes_text = "\n".join(
        f"Escena {s['id']}: \"{s['texto']}\""
        for s in scenes
    )

    # Build collection context hint
    collection_hint = ""
    col_lower = (collection or "").lower()
    if col_lower in ("cine", "peliculas", "movies", "film"):
        collection_hint = (
            "\nCONTEXTO: Este video es de la coleccion CINE. Usa VARIEDAD de tipos: "
            "~40-50% 'clip_bank' para footage especifico de peliculas (trailers, behind-the-scenes, VFX), "
            "~25-35% 'stock_video' para tomas genéricas relacionadas (explosiones, ciudades, tecnologia, naturaleza), "
            "~10-15% 'title_card' para titulos numerados o introducciones de seccion, "
            "~5% 'ai_image' solo para conceptos muy abstractos. "
            "NO pongas todo como clip_bank — mezcla tipos para un video mas interesante."
        )
    elif col_lower in ("tech", "tecnologia", "technology"):
        collection_hint = (
            "\nCONTEXTO: Video de tecnologia. Priorizar 'clip_bank' para footage tech "
            "y 'stock_video' para tomas genericas."
        )
    elif col_lower in ("historia", "history"):
        collection_hint = (
            "\nCONTEXTO: Video historico. Priorizar 'archive_footage' para eventos reales "
            "y 'clip_bank' para footage documental."
        )

    system_prompt = (
        "Eres un editor de video profesional. Analizas cada escena de un video "
        "para decidir que tipo de visual necesita. "
        "Devuelve SOLO un JSON array. Sin markdown, sin explicacion."
    )

    # Build asset type descriptions — only include allowed types
    type_descriptions = {
        "clip_bank": '"clip_bank": footage ESPECIFICO de nuestro banco de clips local. Usar para: escenas que mencionan peliculas especificas, behind-the-scenes, VFX, escenas tematicas de la coleccion.',
        "stock_video": '"stock_video": footage de VIDEO GENERICO buscado en internet (Pexels/Pixabay). Usar para: tomas de ciudades, naturaleza, tecnologia, personas, acciones genericas.',
        "title_card": '"title_card": para titulos de seccion numerados (ej: \'#10 Miniatures Over CGI\'). Se buscara una IMAGEN DE FONDO en internet y se pondra el TEXTO ENCIMA.',
        "web_image": '"web_image": IMAGEN buscada en internet (Pexels/Pixabay/Google). Usar para: fotos reales de objetos, lugares, personas, eventos. NO es video, es una imagen fija de alta calidad.',
        "ai_image": '"ai_image": imagen generada por IA. Usar para conceptos abstractos o cuando no hay otra opcion.',
        "archive_footage": '"archive_footage": eventos historicos reales: guerras, revoluciones, presidentes, documentos antiguos.',
        "space_media": '"space_media": espacio, planetas, NASA, astronomia, cohetes.',
    }

    if allowed_types:
        types_block = "\n".join(f"   - {type_descriptions[t]}" for t in allowed_types if t in type_descriptions)
        types_constraint = f"\nIMPORTANTE: SOLO puedes usar estos tipos: {', '.join(allowed_types)}. NO uses ningun otro tipo."
    else:
        types_block = "\n".join(f"   - {v}" for v in type_descriptions.values())
        types_constraint = ""

    title_context = ""
    if project_title:
        title_context = f"\nTITULO DEL VIDEO: {project_title}\nIMPORTANTE: Las search queries DEBEN ser especificas al tema del video. Si el video es sobre una pelicula, incluye el nombre de la pelicula. Si es sobre una persona, incluye su nombre. Las queries deben ser tan especificas que al buscar en Google encuentres la imagen exacta que necesitas para esa escena.\n"

    user_prompt = f"""Analiza cada escena de este video para decidir que tipo de visual necesita.
{title_context}
GUION COMPLETO (para contexto):
{full_script}
{collection_hint}{types_constraint}

ESCENAS:
{scenes_text}

Para CADA escena, decide:

1. asset_type — Que tipo de visual necesita (USA VARIEDAD, no pongas todo igual):
{types_block}

2. search_query — Termino de busqueda en INGLES, maximo 5-7 palabras, visual y ESPECIFICO AL TEMA DEL VIDEO. Si el video habla de "Independence Day (1996)", la query debe incluir "Independence Day 1996" no solo "explosion". Pensa: que escribirias en Google Images para encontrar exactamente la imagen que necesita esta escena?

3. search_query_alt — Termino alternativo mas generico por si el primero no da resultados.

4. has_overlay_text — true si la escena es un titulo de seccion numerado o una introduccion que necesita texto sobre el visual.

5. overlay_text — Si has_overlay_text es true, un titulo CORTO de 2-5 palabras maximo (ej: '#10 Miniatures Over CGI', 'Independence Day', '20 Hidden Facts', 'The Hidden Truth'). NUNCA uses la frase completa de la escena — genera un titulo BREVE y cinematografico. Si es false, null.

Devuelve SOLO un JSON array:
[
  {{
    "scene_id": 1,
    "asset_type": "clip_bank",
    "search_query": "movie explosion practical effects",
    "search_query_alt": "explosion fire vfx",
    "has_overlay_text": false,
    "overlay_text": null
  }}
]"""

    _safe_print(f"[VisualAnalyzer] Analyzing {len(scenes)} scenes...")

    resp = _client.chat.completions.create(
        model=_MODEL,
        max_tokens=8000,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    raw = resp.choices[0].message.content.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    results = json.loads(raw)

    if not isinstance(results, list):
        raise ValueError(f"Expected JSON array, got: {type(results)}")

    # Enforce allowed_types — fix any violations
    if allowed_types:
        allowed_set = set(allowed_types)
        fallback = allowed_types[0]
        for r in results:
            if r.get("asset_type") not in allowed_set:
                _safe_print(f"[VisualAnalyzer] Scene {r.get('scene_id')}: type '{r.get('asset_type')}' not allowed, using '{fallback}'")
                r["asset_type"] = fallback

    for r in results:
        _safe_print(
            f"[VisualAnalyzer] Scene {r.get('scene_id')}: "
            f"type={r.get('asset_type')}, query='{r.get('search_query')}'"
            f"{' [OVERLAY: ' + r.get('overlay_text', '') + ']' if r.get('has_overlay_text') else ''}"
        )

    return results
