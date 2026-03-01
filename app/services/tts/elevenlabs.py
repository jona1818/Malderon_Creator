"""
ElevenLabs TTS provider.

API reference: https://docs.elevenlabs.io/api-reference/text-to-speech

Supported config keys:
  voice_id    (str)           – ElevenLabs voice ID (find it in your ElevenLabs dashboard)
  model_id    (str)           – Model to use (default: "eleven_multilingual_v2")
  stability   (float, 0–1)   – Voice stability (default: 0.5)
  similarity  (float, 0–1)   – Similarity boost (default: 0.75)
"""
from __future__ import annotations

from pathlib import Path

from .base import TTSProvider


class ElevenLabsTTS(TTSProvider):
    name = "elevenlabs"

    BASE_URL = "https://api.elevenlabs.io/v1"

    def generate(self, text: str, output_path: Path) -> Path:
        """
        TODO: implement once an ElevenLabs API key is available.

        Expected request:
            POST  {BASE_URL}/text-to-speech/{voice_id}
            xi-api-key: {self.api_key}
            Content-Type: application/json
            Body: {
                "text": text,
                "model_id": self.config.get("model_id", "eleven_multilingual_v2"),
                "voice_settings": {
                    "stability":        self.config.get("stability",  0.5),
                    "similarity_boost": self.config.get("similarity", 0.75),
                }
            }

        Expected response: audio/mpeg binary stream.

        Example implementation:
            import requests
            voice_id = self.config.get("voice_id", "")
            resp = requests.post(
                f"{self.BASE_URL}/text-to-speech/{voice_id}",
                headers={
                    "xi-api-key": self.api_key,
                    "Content-Type": "application/json",
                },
                json={
                    "text": text,
                    "model_id": self.config.get("model_id", "eleven_multilingual_v2"),
                    "voice_settings": {
                        "stability":        float(self.config.get("stability",  0.5)),
                        "similarity_boost": float(self.config.get("similarity", 0.75)),
                    },
                },
                timeout=120,
            )
            resp.raise_for_status()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(resp.content)
            return output_path
        """
        raise NotImplementedError(
            "ElevenLabs TTS not yet connected. "
            "Implement the HTTP call in app/services/tts/elevenlabs.py"
        )
