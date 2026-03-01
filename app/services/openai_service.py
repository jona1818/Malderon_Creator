"""OpenAI TTS + Whisper transcription service."""
import os
import re
from pathlib import Path
from openai import OpenAI
from ..config import settings

client = OpenAI(api_key=settings.openai_api_key)


# ── Text-to-Speech ────────────────────────────────────────────────────────────

def generate_tts(text: str, output_path: Path, voice: str = "alloy") -> Path:
    """Generate MP3 voiceover from text using OpenAI TTS.

    Supported voices: alloy, echo, fable, onyx, nova, shimmer
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    response = client.audio.speech.create(
        model="tts-1",
        voice=voice,
        input=text,
        response_format="mp3",
    )
    response.stream_to_file(str(output_path))
    return output_path


# ── Whisper Transcription → SRT ───────────────────────────────────────────────

def transcribe_to_srt(audio_path: Path, output_srt_path: Path) -> Path:
    """Transcribe audio with Whisper and write an SRT subtitle file."""
    audio_path = Path(audio_path)
    output_srt_path = Path(output_srt_path)
    output_srt_path.parent.mkdir(parents=True, exist_ok=True)

    with open(audio_path, "rb") as f:
        transcript = client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            response_format="verbose_json",
            timestamp_granularities=["word"],
        )

    srt_content = _build_srt(transcript)
    output_srt_path.write_text(srt_content, encoding="utf-8")
    return output_srt_path


def _build_srt(transcript) -> str:
    """Convert Whisper verbose_json transcript to SRT format."""
    lines = []
    segments = getattr(transcript, "segments", None)

    if not segments:
        # Fallback: single segment covering entire duration
        duration = getattr(transcript, "duration", 10.0)
        text = getattr(transcript, "text", "").strip()
        lines.append("1")
        lines.append(f"00:00:00,000 --> {_fmt_time(duration)}")
        lines.append(text)
        lines.append("")
        return "\n".join(lines)

    for i, seg in enumerate(segments, 1):
        start = _fmt_time(seg.get("start", 0) if isinstance(seg, dict) else seg.start)
        end = _fmt_time(seg.get("end", 0) if isinstance(seg, dict) else seg.end)
        text = (seg.get("text", "") if isinstance(seg, dict) else seg.text).strip()
        lines.append(str(i))
        lines.append(f"{start} --> {end}")
        lines.append(text)
        lines.append("")

    return "\n".join(lines)


def _fmt_time(seconds: float) -> str:
    """Convert seconds to SRT timestamp HH:MM:SS,mmm."""
    total_ms = int(seconds * 1000)
    ms = total_ms % 1000
    s = (total_ms // 1000) % 60
    m = (total_ms // 60000) % 60
    h = total_ms // 3600000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
