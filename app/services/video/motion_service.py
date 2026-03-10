"""
Motion Prompt service – generates animation descriptions using OpenRouter.
"""
import time
import httpx


_DEFAULT_MOTION = "Slow cinematic zoom in, subtle ambient movement"


def generate_motion_prompt(narration: str, image_prompt: str) -> str:
    """
    Generate a short motion prompt (max 15 words) based on narration and image prompt.
    Uses OpenRouter API with a free/cheap model. Retries on 429.
    """
    from ...config import settings

    api_key = settings.openrouter_api_key
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY no configurado.")

    narration = (narration or "").strip()
    image_prompt = (image_prompt or "").strip()
    if not narration and not image_prompt:
        return _DEFAULT_MOTION

    system_prompt = (
        "Eres un director de fotografía experto en IA. Basado en la siguiente Narración "
        "y el Prompt de la Imagen, genera un SHORT MOTION PROMPT (máximo 15 palabras) "
        "para animar esta imagen.\n"
        'Usa términos como: "Slow cinematic zoom in", "Subtle camera pan right", '
        '"Gentle movement in the background", "Dynamic tracking shot".\n'
        "Enfócate SOLO en el movimiento de cámara o del sujeto. "
        "Responde SOLO con el motion prompt, sin explicaciones."
    )
    user_prompt = f"Narración: {narration}\nImagen Prompt: {image_prompt}"

    for attempt in range(3):
        resp = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "stepfun/step-3.5-flash:free",
                "max_tokens": 50,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            },
            timeout=30.0,
        )
        if resp.status_code == 429:
            wait = 2 ** attempt * 2  # 2s, 4s, 8s
            time.sleep(wait)
            continue
        resp.raise_for_status()
        data = resp.json()
        try:
            content = data["choices"][0]["message"].get("content")
        except (KeyError, IndexError, TypeError):
            return _DEFAULT_MOTION
        return (content or "").strip() or _DEFAULT_MOTION

    # All retries exhausted (429)
    return _DEFAULT_MOTION
