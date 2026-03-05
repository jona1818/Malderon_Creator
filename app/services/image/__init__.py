"""
Image generation dispatcher.

Routes to WaveSpeed or Pollinations based on the provider parameter.
"""
from __future__ import annotations

from pathlib import Path

from . import pollinations_service
from . import wavespeed_image_service


def generate_image(
    prompt: str,
    output_path: str | Path,
    provider: str = "wavespeed",
    api_key: str = "",
    wavespeed_api_key: str = "",
    reference_character_path: str | Path | None = None,
    reference_style_path: str | Path | None = None,
    width: int = 1920,
    height: int = 1080,
) -> Path:
    """Generate an image using the specified provider.

    Parameters
    ----------
    provider : "wavespeed" or "pollinations"
    api_key : Pollinations API key (used when provider="pollinations")
    wavespeed_api_key : WaveSpeed API key (used when provider="wavespeed")
    """
    if provider == "wavespeed":
        return wavespeed_image_service.generate_image(
            prompt, output_path,
            api_key=wavespeed_api_key,
            reference_character_path=reference_character_path,
            reference_style_path=reference_style_path,
            width=width, height=height,
        )
    else:
        return pollinations_service.generate_image(
            prompt, output_path,
            api_key=api_key,
            reference_character_path=reference_character_path,
            reference_style_path=reference_style_path,
            width=width, height=height,
        )
