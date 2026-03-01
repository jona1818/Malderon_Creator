"""YouTube transcript extraction service."""
import re
import requests
from youtube_transcript_api import YouTubeTranscriptApi


def extract_video_id(url: str) -> str:
    """Extract YouTube video ID from various URL formats."""
    patterns = [
        r"(?:v=|/v/|youtu\.be/|/embed/|/shorts/)([a-zA-Z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    raise ValueError(f"Could not extract video ID from URL: {url}")


def get_video_title(url: str) -> str:
    """Fetch video title via YouTube oEmbed (no API key required)."""
    try:
        resp = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": url, "format": "json"},
            timeout=10,
        )
        if resp.ok:
            return resp.json().get("title", "Video de referencia")
    except Exception:
        pass
    return "Video de referencia"


def get_transcript(url: str) -> dict:
    """
    Extract transcript from a YouTube URL.
    Returns: {video_id, title, transcript, url}
    Raises ValueError if transcript cannot be obtained.
    """
    video_id = extract_video_id(url)
    title = get_video_title(url)

    api = YouTubeTranscriptApi()
    try:
        # Try Spanish first, then English
        snippets = api.fetch(video_id, languages=["es", "en"])
    except Exception:
        try:
            # Fall back to any available transcript
            transcript_list = api.list(video_id)
            transcript_obj = transcript_list.find_transcript(["es", "en"])
            snippets = transcript_obj.fetch()
        except Exception as e:
            raise ValueError(f"No transcript available for this video: {e}")

    transcript_text = " ".join(s.text for s in snippets)
    return {
        "video_id": video_id,
        "title": title,
        "transcript": transcript_text,
        "url": url,
    }
