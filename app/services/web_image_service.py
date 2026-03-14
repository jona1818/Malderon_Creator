"""Web image search — multiple free sources, no API key needed.

Sources tried in order:
  1. Google Images (best relevance for specific queries)
  2. Bing Images (direct scrape)
  3. Brave Search Images
  4. Wikimedia Commons API

Returns multiple candidates so the caller can try each until one downloads.
"""
from __future__ import annotations

import random
import re
import sys
import time
import json
from typing import Optional, List
from urllib.parse import quote_plus

# Track how many times a query has been searched — to vary pagination on re-search
_query_search_count: dict[str, int] = {}

import requests

# Reuse watermark blocklist from ddg service
from .ddg_image_service import _is_blocked


_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
})

# Min delay between requests to same service
_MIN_DELAY = 1.5
_last_call: dict[str, float] = {}


def _safe_print(msg: str) -> None:
    try:
        sys.stdout.buffer.write((msg + "\n").encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()
    except Exception:
        pass


def _wait_for(service: str) -> None:
    now = time.time()
    last = _last_call.get(service, 0)
    elapsed = now - last
    if elapsed < _MIN_DELAY:
        time.sleep(_MIN_DELAY - elapsed)
    _last_call[service] = time.time()


def _valid_urls(urls: list[str], max_count: int = 10) -> List[str]:
    """Return valid URLs that aren't blocked, up to max_count."""
    result = []
    seen = set()
    for url in urls:
        if not url or not url.startswith("http"):
            continue
        # Normalize URL for dedup
        url_clean = url.split("?")[0].lower()
        if url_clean in seen:
            continue
        seen.add(url_clean)
        if _is_blocked(url):
            continue
        # Skip very small images (thumbnails)
        if any(x in url.lower() for x in ["thumb", "icon", "favicon", "1x1", "pixel", "logo", "badge"]):
            continue
        result.append(url)
        if len(result) >= max_count:
            break
    return result


# ── Bing Images ──────────────────────────────────────────────────────────────

def _search_bing(query: str, count: int = 20) -> List[str]:
    """Search Bing Images. Returns list of valid direct URLs. Filters: wide aspect + large size."""
    try:
        _wait_for("bing")
        # Use different page offset on re-searches so results vary
        search_num = _query_search_count.get(query, 0)
        first = 1 + (search_num * 35)  # Page through results: 1, 36, 71, ...
        # qft filters: wide aspect ratio (horizontal) + large images
        url = (f"https://www.bing.com/images/search?q={quote_plus(query)}"
               f"&form=HDRSC2&first={first}&count=35"
               f"&qft=+filterui:aspect-wide+filterui:imagesize-large")
        _safe_print(f"[Bing] Searching images (wide+large, page={search_num+1}): '{query}'")
        # Clear cookies to avoid cached results
        _SESSION.cookies.clear()
        resp = _SESSION.get(url, timeout=15)
        if resp.status_code != 200:
            _safe_print(f"[Bing] HTTP {resp.status_code}")
            return []

        html = resp.text
        # Bing encodes image URLs as murl&quot;:&quot;URL&quot;
        img_urls = re.findall(r'murl&quot;:&quot;(https?://[^&]+)&', html)
        # Also try JSON format (some pages use it)
        img_urls += re.findall(r'"murl"\s*:\s*"(https?://[^"]+)"', html)

        results = _valid_urls(img_urls, count)
        _safe_print(f"[Bing] Found {len(results)} valid images for '{query}'")
        return results

    except Exception as exc:
        _safe_print(f"[Bing] Error: {exc}")
        return []


# ── Brave Search Images ──────────────────────────────────────────────────────

def _search_brave(query: str, count: int = 15) -> List[str]:
    """Search Brave for images. Returns list of valid direct URLs."""
    try:
        _wait_for("brave")
        # Use page offset on re-searches
        search_num = _query_search_count.get(query, 0)
        offset_param = f"&offset={search_num * 20}" if search_num > 0 else ""
        # img_layout=Wide for horizontal images
        url = f"https://search.brave.com/images?q={quote_plus(query)}&source=web&img_layout=Wide{offset_param}"
        _safe_print(f"[Brave] Searching images (wide, page={search_num+1}): '{query}'")
        _SESSION.cookies.clear()
        resp = _SESSION.get(url, timeout=15)
        if resp.status_code != 200:
            _safe_print(f"[Brave] HTTP {resp.status_code}")
            return []

        html = resp.text
        # Brave embeds image URLs in JSON data within script tags
        img_urls = re.findall(
            r'"(?:url|src|thumbnail)"\s*:\s*"(https?://[^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"',
            html, re.IGNORECASE
        )
        # Also try noscript img tags
        img_urls += re.findall(
            r'<img[^>]+src="(https?://[^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"',
            html, re.IGNORECASE
        )

        results = _valid_urls(img_urls, count)
        _safe_print(f"[Brave] Found {len(results)} valid images for '{query}'")
        return results

    except Exception as exc:
        _safe_print(f"[Brave] Error: {exc}")
        return []


# ── Wikimedia Commons ────────────────────────────────────────────────────────

def _search_wikimedia(query: str, count: int = 10) -> List[str]:
    """Search Wikimedia Commons for free images. Returns list of valid URLs."""
    try:
        _wait_for("wikimedia")
        _safe_print(f"[Wikimedia] Searching: '{query}'")
        resp = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            headers={"User-Agent": "MalderonCreator/1.0 (video creation tool; contact@example.com)"},
            params={
                "action": "query",
                "generator": "search",
                "gsrsearch": f"filetype:bitmap {query}",
                "gsrlimit": count,
                "gsrnamespace": 6,  # File namespace
                "prop": "imageinfo",
                "iiprop": "url|size|mime",
                "iiurlwidth": 1280,
                "format": "json",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        pages = data.get("query", {}).get("pages", {})
        if not pages:
            _safe_print(f"[Wikimedia] No results for '{query}'")
            return []

        # Sort by image size (prefer larger)
        candidates = []
        for page in pages.values():
            info_list = page.get("imageinfo", [])
            if not info_list:
                continue
            info = info_list[0]
            mime = info.get("mime", "")
            if "image" not in mime:
                continue
            url = info.get("thumburl") or info.get("url", "")
            width = info.get("thumbwidth") or info.get("width", 0)
            height = info.get("thumbheight") or info.get("height", 0)
            # Only accept landscape (horizontal) images with decent resolution
            if url and width >= 640 and width > height:
                candidates.append((width, url))

        candidates.sort(reverse=True)
        results = [url for _, url in candidates if not _is_blocked(url)]
        _safe_print(f"[Wikimedia] Found {len(results)} valid images for '{query}'")
        return results

    except Exception as exc:
        _safe_print(f"[Wikimedia] Error: {exc}")
        return []


# ── Main search functions ────────────────────────────────────────────────────

def search_image_candidates(query: str, max_per_source: int = 8, shuffle: bool = True) -> List[str]:
    """Search all sources and return ALL valid candidate URLs.

    Returns a combined list from Bing → Brave → Wikimedia.
    If shuffle=True, randomizes order so re-searches get different images.
    The caller should try downloading each until one succeeds.
    """
    all_urls: List[str] = []
    seen = set()

    def _add(urls: List[str]):
        for u in urls:
            key = u.split("?")[0].lower()
            if key not in seen:
                seen.add(key)
                all_urls.append(u)

    # 1. Bing Images (best direct URLs)
    _add(_search_bing(query, max_per_source))

    # 2. Brave Search
    _add(_search_brave(query, max_per_source))

    # 3. Wikimedia Commons
    _add(_search_wikimedia(query, max_per_source))

    # Increment search count so next search for same query gets different page
    _query_search_count[query] = _query_search_count.get(query, 0) + 1

    if shuffle and len(all_urls) > 1:
        random.shuffle(all_urls)

    _safe_print(f"[WebImg] Total candidates for '{query}': {len(all_urls)} (search #{_query_search_count[query]}, shuffled={shuffle})")
    return all_urls


def search_image(query: str) -> Optional[str]:
    """Search multiple free sources for an image. Returns first valid direct URL or None.

    Tries: Bing → Brave → Wikimedia Commons
    For backward compatibility — returns only the first candidate.
    """
    candidates = search_image_candidates(query, max_per_source=5)
    if candidates:
        return candidates[0]
    _safe_print(f"[WebImg] All sources exhausted for '{query}'")
    return None
