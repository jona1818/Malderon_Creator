"""
GenAIPro.vn TTS provider.

BASE URL : https://genaipro.vn/api/v1
AUTH     : Authorization: Bearer {api_key}

Flow:
  1. POST /labs/task          → { "task_id": "uuid" }
  2. GET  /labs/task/{id}     → poll every 5 s until status == "completed"
     completed response:
       { "status": "completed",
         "result":   "https://files.genaipro.vn/….mp3",
         "subtitle": "https://files.genaipro.vn/….srt" }
  3. Download MP3  → output_path
  4. Download SRT  → output_path.with_suffix(".srt")   (free, no Whisper needed)

Supported config keys:
  voice_id     (str)           – Voice ID from GenAIPro dashboard  (required)
  model_id     (str)           – default: eleven_multilingual_v2
  style        (float 0–1)    – default: 0.0
  speed        (float 0.7–1.2)– default: 1.0
  similarity   (float 0–1)    – default: 0.75
  stability    (float 0–1)    – default: 0.5
  use_speaker_boost (bool)    – default: false
"""
from __future__ import annotations

import time
from pathlib import Path

import requests

from .base import TTSProvider

BASE_URL = "https://genaipro.vn/api/v1"


class GenAIProTTS(TTSProvider):
    name = "genaipro"

    # ── Public helpers ────────────────────────────────────────────────────────

    @staticmethod
    def list_voices(api_key: str, search: str = "", gender: str = "", language: str = "") -> list:
        """GET /labs/voices — returns list of available voice dicts."""
        params: dict = {}
        if search:   params["search"]   = search
        if gender:   params["gender"]   = gender
        if language: params["language"] = language

        resp = requests.get(
            f"{BASE_URL}/labs/voices",
            headers={"Authorization": f"Bearer {api_key}"},
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        # API may return a list directly or wrap it
        if isinstance(data, list):
            return data
        return data.get("voices") or data.get("data") or data.get("items") or []

    # ── TTSProvider interface ─────────────────────────────────────────────────

    def generate(self, text: str, output_path: Path) -> Path:
        voice_id         = self.config.get("voice_id", "").strip()
        model_id         = self.config.get("model_id", "eleven_multilingual_v2")
        style            = float(self.config.get("style",      0.0))
        speed            = float(self.config.get("speed",      1.0))
        similarity       = float(self.config.get("similarity", 0.75))
        stability        = float(self.config.get("stability",  0.5))
        use_speaker_boost = bool(self.config.get("use_speaker_boost", False))

        if not voice_id:
            raise ValueError(
                "voice_id es requerido para GenAIPro TTS. "
                "Selecciona una voz en el panel de configuración."
            )

        # 1. Create task
        task_id = self._create_task(
            text, voice_id, model_id,
            style, speed, similarity, stability, use_speaker_boost,
        )

        # 2. Poll until completed
        result_url, subtitle_url = self._poll_task(task_id)

        # 3. Download MP3
        output_path.parent.mkdir(parents=True, exist_ok=True)
        mp3_resp = requests.get(result_url, timeout=120)
        mp3_resp.raise_for_status()
        output_path.write_bytes(mp3_resp.content)

        # 4. Download SRT (no Whisper needed — GenAIPro provides it)
        if subtitle_url:
            srt_path = output_path.with_suffix(".srt")
            try:
                srt_resp = requests.get(subtitle_url, timeout=60)
                srt_resp.raise_for_status()
                srt_path.write_bytes(srt_resp.content)
            except Exception:
                pass  # SRT is optional; audio is what matters

        return output_path

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type":  "application/json",
        }

    def _create_task(
        self,
        text: str,
        voice_id: str,
        model_id: str,
        style: float,
        speed: float,
        similarity: float,
        stability: float,
        use_speaker_boost: bool,
    ) -> str:
        payload = {
            "input":             text,
            "voice_id":          voice_id,
            "model_id":          model_id,
            "style":             style,
            "speed":             speed,
            "similarity":        similarity,
            "stability":         stability,
            "use_speaker_boost": use_speaker_boost,
        }
        resp = requests.post(
            f"{BASE_URL}/labs/task",
            headers=self._headers(),
            json=payload,
            timeout=60,
        )
        if not resp.ok:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text[:500]
            raise RuntimeError(f"GenAIPro error {resp.status_code}: {detail}")

        data = resp.json()
        task_id = data.get("task_id") or data.get("id")
        if not task_id:
            raise RuntimeError(f"No task_id en la respuesta: {data}")
        return str(task_id)

    def _poll_task(self, task_id: str, max_wait: int = 600) -> tuple[str, str | None]:
        """Poll GET /labs/task/{task_id} every 5 s until status == 'completed'."""
        poll_url = f"{BASE_URL}/labs/task/{task_id}"
        headers  = {"Authorization": f"Bearer {self.api_key}"}
        deadline = time.time() + max_wait

        while time.time() < deadline:
            r = requests.get(poll_url, headers=headers, timeout=30)
            r.raise_for_status()
            data   = r.json()
            status = data.get("status", "").lower()

            if status == "completed":
                result_url   = data.get("result", "")
                subtitle_url = data.get("subtitle") or None
                if not result_url:
                    raise RuntimeError(f"Task completada pero sin URL de audio: {data}")
                return result_url, subtitle_url

            if status in ("failed", "error", "cancelled"):
                raise RuntimeError(f"GenAIPro task {task_id} falló: {data}")

            time.sleep(5)

        raise TimeoutError(f"GenAIPro task {task_id} no completó en {max_wait}s")
