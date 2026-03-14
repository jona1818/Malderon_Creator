"""Render animated title cards using Remotion (React) or FFmpeg fallback."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional


# Path to the remotion project directory
_REMOTION_DIR = Path(__file__).resolve().parent.parent.parent / "remotion"


def _safe_print(msg: str) -> None:
    try:
        sys.stdout.buffer.write((msg + "\n").encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()
    except Exception:
        pass


def render_title_card(
    title_text: str,
    output_path: Path,
    duration_seconds: float = 5.0,
    fps: int = 30,
    background_image: Optional[Path] = None,
) -> bool:
    """Render a title card video with animated text.

    Tries Remotion first, falls back to FFmpeg if unavailable.

    Args:
        title_text: The text to display (e.g. "#10 Miniatures Over CGI")
        output_path: Where to save the MP4 file
        duration_seconds: Duration of the video in seconds
        fps: Frames per second
        background_image: Optional path to a background image file

    Returns:
        True if the video was created successfully
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Try Remotion first
    success = _render_with_remotion(title_text, output_path, duration_seconds, fps, background_image)
    if success:
        return True

    # Fallback to FFmpeg
    _safe_print("[Remotion] Falling back to FFmpeg for title card...")
    return _render_with_ffmpeg(title_text, output_path, duration_seconds, fps, background_image)


def _render_with_remotion(
    title_text: str,
    output_path: Path,
    duration_seconds: float,
    fps: int,
    background_image: Optional[Path] = None,
) -> bool:
    """Render title card using Remotion CLI."""
    bg_filename = None
    try:
        # Check if remotion directory and node_modules exist
        if not (_REMOTION_DIR / "node_modules").exists():
            _safe_print("[Remotion] node_modules not found, skipping Remotion render")
            return False

        duration_frames = max(int(duration_seconds * fps), 30)  # At least 1 second

        # Copy background image to remotion/public/ if provided
        if background_image and Path(background_image).exists():
            public_dir = _REMOTION_DIR / "public"
            public_dir.mkdir(exist_ok=True)
            bg_filename = f"bg_{output_path.stem}{Path(background_image).suffix}"
            bg_dest = public_dir / bg_filename
            shutil.copy2(str(background_image), str(bg_dest))
            _safe_print(f"[Remotion] Background image: {bg_filename}")

        # Write props to a temp JSON file
        props = {
            "titleText": title_text,
            "durationInFrames": duration_frames,
            "backgroundImage": bg_filename,
        }

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, dir=str(_REMOTION_DIR)
        ) as f:
            json.dump(props, f)
            props_path = f.name

        try:
            # Build the remotion render command
            cmd = [
                "npx", "remotion", "render",
                "src/Root.tsx",
                "TitleCard",
                str(output_path.resolve()),
                f"--props={props_path}",
                "--codec=h264",
                "--log=error",
            ]

            bg_label = " + bg" if bg_filename else ""
            _safe_print(f"[Remotion] Rendering: {title_text[:50]}...{bg_label} ({duration_seconds:.1f}s, {duration_frames} frames)")

            result = subprocess.run(
                cmd,
                cwd=str(_REMOTION_DIR),
                capture_output=True,
                text=True,
                timeout=120,
                shell=True,  # Required on Windows for npx
            )

            if result.returncode != 0:
                _safe_print(f"[Remotion] Error (exit {result.returncode}): {result.stderr[:500]}")
                return False

            if output_path.exists() and output_path.stat().st_size > 1000:
                _safe_print(f"[Remotion] Success: {output_path.name} ({output_path.stat().st_size // 1024}KB)")
                return True

            _safe_print("[Remotion] Output file missing or too small")
            return False

        finally:
            # Clean up temp props file
            try:
                Path(props_path).unlink(missing_ok=True)
            except Exception:
                pass

    except subprocess.TimeoutExpired:
        _safe_print("[Remotion] Render timed out (120s)")
        return False
    except FileNotFoundError:
        _safe_print("[Remotion] npx not found — Node.js not installed?")
        return False
    except Exception as exc:
        _safe_print(f"[Remotion] Unexpected error: {exc}")
        return False
    finally:
        # Clean up background image from public/
        if bg_filename:
            try:
                (_REMOTION_DIR / "public" / bg_filename).unlink(missing_ok=True)
            except Exception:
                pass


def _render_with_ffmpeg(
    title_text: str,
    output_path: Path,
    duration_seconds: float,
    fps: int,
    background_image: Optional[Path] = None,
) -> bool:
    """Fallback: render title card using FFmpeg drawtext, optionally over an image."""
    try:
        # Escape special characters for FFmpeg drawtext
        escaped = (
            title_text.strip()
            .replace("\\", "\\\\\\\\")
            .replace(":", "\\:")
            .replace("'", "\\'")
            .replace("%", "%%")
        )

        if background_image and Path(background_image).exists():
            # Render text over background image
            cmd = [
                "ffmpeg", "-y",
                "-loop", "1",
                "-i", str(background_image),
                "-t", str(duration_seconds),
                "-vf", (
                    f"scale=1920:1080:force_original_aspect_ratio=increase,"
                    f"crop=1920:1080,"
                    f"zoompan=z='min(zoom+0.0005,1.08)':d={int(duration_seconds * fps)}:s=1920x1080,"
                    f"drawtext=text='{escaped}'"
                    f":fontcolor=white:fontsize=72:font=Arial"
                    f":x=(w-text_w)/2:y=h-text_h-100"
                    f":shadowcolor=black@0.8:shadowx=3:shadowy=3"
                ),
                "-c:v", "libx264",
                "-pix_fmt", "yuv420p",
                "-preset", "fast",
                "-crf", "23",
                "-r", str(fps),
                str(output_path),
            ]
        else:
            # Black background with centered text
            cmd = [
                "ffmpeg", "-y",
                "-f", "lavfi",
                "-i", f"color=c=0x0a0a0a:s=1920x1080:d={duration_seconds}:r={fps}",
                "-vf", (
                    f"drawtext=text='{escaped}'"
                    f":fontcolor=white:fontsize=72:font=Arial"
                    f":x=(w-text_w)/2:y=(h-text_h)/2"
                    f":shadowcolor=black:shadowx=2:shadowy=2"
                ),
                "-c:v", "libx264",
                "-pix_fmt", "yuv420p",
                "-preset", "fast",
                "-crf", "23",
                str(output_path),
            ]

        bg_label = " + bg" if background_image else ""
        _safe_print(f"[FFmpeg] Rendering title card{bg_label}: {title_text[:50]}...")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )

        if result.returncode != 0:
            _safe_print(f"[FFmpeg] Error: {result.stderr[:300]}")
            return False

        if output_path.exists() and output_path.stat().st_size > 1000:
            _safe_print(f"[FFmpeg] Success: {output_path.name}")
            return True

        return False

    except Exception as exc:
        _safe_print(f"[FFmpeg] Error: {exc}")
        return False
