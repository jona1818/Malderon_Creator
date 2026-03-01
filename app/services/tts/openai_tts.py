"""
OpenAI TTS provider.

API reference: https://platform.openai.com/docs/api-reference/audio/createSpeech

Supported config keys:
  voice   (str)           – One of: alloy, echo, fable, onyx, nova, shimmer (default: "alloy")
  model   (str)           – One of: tts-1, tts-1-hd (default: "tts-1")
  speed   (float, 0.25–4) – Speed multiplier (default: 1.0)
"""
from __future__ import annotations

from pathlib import Path

from .base import TTSProvider


class OpenAITTS(TTSProvider):
    name = "openai"

    API_URL = "https://api.openai.com/v1/audio/speech"

    def generate(self, text: str, output_path: Path) -> Path:
        """
        TODO: implement once an OpenAI API key is available.

        Expected request:
            POST  {API_URL}
            Authorization: Bearer {self.api_key}
            Content-Type: application/json
            Body: {
                "model": self.config.get("model", "tts-1"),
                "input": text,
                "voice": self.config.get("voice", "alloy"),
                "speed": self.config.get("speed", 1.0),
            }

        Expected response: audio/mpeg binary stream.

        Example implementation:
            import requests
            resp = requests.post(
                self.API_URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.config.get("model", "tts-1"),
                    "input": text,
                    "voice": self.config.get("voice", "alloy"),
                    "speed": float(self.config.get("speed", 1.0)),
                },
                timeout=120,
            )
            resp.raise_for_status()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(resp.content)
            return output_path
        """
        raise NotImplementedError(
            "OpenAI TTS not yet connected. "
            "Implement the HTTP call in app/services/tts/openai_tts.py"
        )
