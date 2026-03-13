"""Visual Analyzer — uses Claude Haiku to decide what type of visual each scene needs."""

import json
import re
import sys
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


def analyze_scenes(full_script: str, scenes: List[Dict]) -> List[Dict]:
    """Analyze each scene and decide asset_type + search_query.

    Args:
        full_script: complete narration text (for context)
        scenes: list of dicts with at least 'id' and 'texto'

    Returns:
        list of dicts per scene: scene_id, asset_type, search_query, search_query_alt,
        has_overlay_text, overlay_text
    """
    # Process in blocks of 15 scenes
    all_results = []
    for i in range(0, len(scenes), 15):
        block = scenes[i:i + 15]
        block_results = _analyze_block(full_script, block)
        all_results.extend(block_results)
    return all_results


def _analyze_block(full_script: str, scenes: List[Dict]) -> List[Dict]:
    """Analyze a block of up to 15 scenes."""
    scenes_text = "\n".join(
        f"Escena {s['id']}: \"{s['texto']}\""
        for s in scenes
    )

    system_prompt = (
        "Eres un editor de video profesional. Analizas cada escena de un video "
        "para decidir que tipo de visual necesita. "
        "Devuelve SOLO un JSON array. Sin markdown, sin explicacion."
    )

    user_prompt = f"""Analiza cada escena de este video para decidir que tipo de visual necesita.

GUION COMPLETO (para contexto):
{full_script}

ESCENAS:
{scenes_text}

Para CADA escena, decide:

1. asset_type — Que tipo de visual necesita:
   - "stock_video": escenas genericas de ciudades, tecnologia, naturaleza, personas, negocios. Es el tipo mas comun. Usar para el 70-80% de las escenas.
   - "archive_footage": escenas que hablan de eventos historicos reales: guerras (WW1, WW2, Vietnam, Guerra Fria), siglo XX, revoluciones, presidentes historicos, batallas, invasiones, tratados, documentos antiguos, propaganda de epoca. Si el guion completo trata sobre un evento historico, la MAYORIA de escenas deberian ser archive_footage.
   - "space_media": escenas que hablan de espacio, planetas, NASA, astronomia, estrellas, cohetes, ISS. Usar cuando la escena menciona contenido espacial/cientifico.
   - "ai_image": usar SOLO como ultimo recurso para escenas muy abstractas o conceptuales que no se pueden representar con video real.

2. search_query — Termino de busqueda en INGLES, maximo 5 palabras, visual y especifico. Pensa: que escribirias en Pexels para encontrar un video que represente esta escena? NO uses palabras abstractas. Usa cosas que se puedan filmar: objetos, lugares, acciones, personas.

3. search_query_alt — Termino alternativo mas generico por si el primero no da resultados.

4. has_overlay_text — true si la escena es un titulo de seccion numerado (ej: '10. TSMC's Chip Factories: The Engine of Growth'). En videos Top 10 o countdown, estos titulos deben mostrarse como texto grande sobre el visual.

5. overlay_text — Si has_overlay_text es true, el texto del titulo limpio (ej: '#10 TSMC Chip Factories'). Si es false, null.

Devuelve SOLO un JSON array:
[
  {{
    "scene_id": 1,
    "asset_type": "stock_video",
    "search_query": "phoenix arizona desert skyline",
    "search_query_alt": "desert city aerial view",
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

    for r in results:
        _safe_print(
            f"[VisualAnalyzer] Scene {r.get('scene_id')}: "
            f"type={r.get('asset_type')}, query='{r.get('search_query')}'"
            f"{' [OVERLAY: ' + r.get('overlay_text', '') + ']' if r.get('has_overlay_text') else ''}"
        )

    return results
