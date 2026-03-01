"""Abstract base class for all TTS providers."""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class TTSProvider(ABC):
    """
    Base class for every TTS provider.

    Subclasses must implement `generate(text, output_path)`.
    The `api_key` and `config` dict are set at instantiation time.

    Config keys are provider-specific (voice_id, speed, model, etc.).
    See each subclass for the list of supported keys.
    """

    #: Human-readable identifier — override in each subclass
    name: str = "base"

    def __init__(self, api_key: str, config: dict):
        self.api_key = api_key
        self.config = config

    @abstractmethod
    def generate(self, text: str, output_path: Path) -> Path:
        """
        Generate TTS audio for `text` and save it to `output_path`.
        Returns the output path on success.
        Raises an exception on failure.
        """
        raise NotImplementedError

    def test(self, text: str, output_path: Path) -> Path:
        """
        Generate a short test clip (default: first 200 chars).
        Delegates to `generate` — override if the provider has a cheaper test endpoint.
        """
        preview = text[:200].strip()
        return self.generate(preview, output_path)
