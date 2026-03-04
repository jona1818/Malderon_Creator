"""
Motion Prompt service to generate animation descriptions using Claude.
"""

from typing import Optional

def generate_motion_prompt(narration: str, image_prompt: str) -> str:
    """
    Generate a short motion prompt (max 15 words) based on narration and image prompt.
    """
    system_prompt = (
        "Eres un director de fotografía experto en IA. Basado en la siguiente Narración "
        "y el Prompt de la Imagen, genera un SHORT MOTION PROMPT (máximo 15 palabras) "
        "para animar esta imagen.\n"
        "Usa términos como: \"Slow cinematic zoom in\", \"Subtle camera pan right\", "
        "\"Gentle movement in the background\", \"Dynamic tracking shot\".\n"
        "Enfócate SOLO en el movimiento de cámara o del sujeto."
    )
    user_prompt = f"Narración: {narration}\nImagen Prompt: {image_prompt}"
    
    # We use our existing claude abstraction
    # If it expects (prompt, system_prompt, max_tokens, etc.) we adjust here
    # Assuming _call_claude helper or similar exists in claude_service
    # Actually, we can use the anthropic client directly if claude_service._call_claude is internal
    
    # Let's inspect claude_service briefly or try to use its client.
    # We will just write the direct call for safety if _call_claude isn't exposed properly,
    # but let's assume get_client exists or we simply import anthropic and settings.
    
    from ..config import settings
    from anthropic import Anthropic
    
    api_key = settings.anthropic_api_token
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY no configurado.")
    
    client = Anthropic(api_key=api_key)
    
    response = client.messages.create(
        model="claude-3-haiku-20240307", # Use Haiku for speed and low cost on short text
        max_tokens=50,
        system=system_prompt,
        messages=[
            {"role": "user", "content": user_prompt}
        ]
    )
    return response.content[0].text.strip()
