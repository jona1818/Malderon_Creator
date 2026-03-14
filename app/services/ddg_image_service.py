"""DuckDuckGo image search — free, no API key needed."""
from __future__ import annotations

import sys
import time
from typing import Optional

try:
    from ddgs import DDGS
    _USE_NEW_API = True
except ImportError:
    from duckduckgo_search import DDGS
    _USE_NEW_API = False


# Domains that serve watermarked / low-quality preview images
_BLOCKED_DOMAINS = (
    "alamy.com", "shutterstock.com", "gettyimages.com", "istockphoto.com",
    "dreamstime.com", "depositphotos.com", "123rf.com", "adobe.stock.com",
    "stock.adobe.com", "bigstockphoto.com", "pond5.com", "agefotostock.com",
    "superstock.com", "masterfile.com", "featurepics.com",
)

# Rate-limit protection
_MIN_DELAY = 3.0
_last_call_time = 0.0

# Circuit breaker: if DDG is rate-limited, stop trying for a while
_circuit_open = False
_circuit_open_until = 0.0
_CIRCUIT_COOLDOWN = 120.0  # 2 minutes


def _is_blocked(url: str) -> bool:
    """Return True if the URL belongs to a watermarked stock-photo site."""
    url_lower = url.lower()
    return any(domain in url_lower for domain in _BLOCKED_DOMAINS)


def _safe_print(msg: str) -> None:
    try:
        sys.stdout.buffer.write((msg + "\n").encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()
    except Exception:
        pass


def _rate_limit_wait() -> None:
    """Wait if needed to avoid DDG rate limits."""
    global _last_call_time
    now = time.time()
    elapsed = now - _last_call_time
    if elapsed < _MIN_DELAY:
        wait = _MIN_DELAY - elapsed
        _safe_print(f"[DDG] Rate limit: waiting {wait:.1f}s")
        time.sleep(wait)
    _last_call_time = time.time()


def _open_circuit() -> None:
    """Open the circuit breaker — skip DDG for a while."""
    global _circuit_open, _circuit_open_until
    _circuit_open = True
    _circuit_open_until = time.time() + _CIRCUIT_COOLDOWN
    _safe_print(f"[DDG] Circuit breaker OPEN — skipping DDG for {_CIRCUIT_COOLDOWN:.0f}s")


def _is_circuit_open() -> bool:
    """Check if circuit breaker is open."""
    global _circuit_open
    if not _circuit_open:
        return False
    if time.time() >= _circuit_open_until:
        _circuit_open = False
        _safe_print("[DDG] Circuit breaker CLOSED — DDG available again")
        return False
    return True


def _ddg_images(query: str, **kwargs):
    """Call DDGS().images() with correct API for installed version."""
    if _USE_NEW_API:
        return DDGS().images(query, **kwargs)
    else:
        return DDGS().images(keywords=query, **kwargs)


def search_image(
    query: str,
    *,
    size: str = "Large",
    layout: str = "Wide",
    max_results: int = 15,
    retries: int = 2,
) -> Optional[str]:
    """Search DuckDuckGo for an image and return the direct URL.

    Returns:
        Direct image URL string, or None if nothing found.
    """
    # Circuit breaker: skip entirely if DDG is rate-limited
    if _is_circuit_open():
        _safe_print(f"[DDG] Skipped (circuit breaker open): '{query}'")
        return None

    for attempt in range(retries):
        try:
            _rate_limit_wait()
            _safe_print(f"[DDG] Searching images: '{query}' size={size} layout={layout}"
                        + (f" (retry {attempt})" if attempt else ""))
            results = _ddg_images(
                query,
                region="us-en",
                safesearch="off",
                size=size,
                type_image="photo",
                layout=layout,
                max_results=max_results,
            )

            for r in results:
                url = r.get("image", "")
                if not url or not url.startswith("http"):
                    continue
                if _is_blocked(url):
                    _safe_print(f"[DDG] Skipped (watermark): {url[:80]}")
                    continue
                w = r.get("width", 0)
                h = r.get("height", 0)
                _safe_print(f"[DDG] Found: {w}x{h} — {url[:80]}")
                return url

            _safe_print(f"[DDG] No results for '{query}'")
            return None

        except Exception as exc:
            exc_str = str(exc).lower()
            is_rate_limit = "ratelimit" in exc_str or "429" in exc_str or "403" in exc_str
            if is_rate_limit:
                if attempt < retries - 1:
                    backoff = _MIN_DELAY * (2 ** (attempt + 1))
                    _safe_print(f"[DDG] Rate limited, waiting {backoff:.0f}s before retry {attempt+1}/{retries}")
                    time.sleep(backoff)
                    continue
                else:
                    # All retries exhausted — open circuit breaker
                    _open_circuit()
                    return None
            # "No results found" from new ddgs package
            if "no results" in exc_str:
                _safe_print(f"[DDG] No results for '{query}'")
                return None
            _safe_print(f"[DDG] Error: {exc}")
            return None

    _safe_print(f"[DDG] All {retries} retries exhausted for '{query}'")
    _open_circuit()
    return None
