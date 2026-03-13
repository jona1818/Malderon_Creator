"""Stock Search Orchestrator — finds the best video/image for each scene.

Searches Pexels, Pixabay, Internet Archive, NARA, and NASA.
Downloads assets locally to the project folder.
"""

import sys
import requests
from pathlib import Path
from typing import Optional, Dict, Tuple
from ..config import settings
from . import pexels_service, pixabay_service


def _safe_print(msg: str) -> None:
    try:
        sys.stdout.buffer.write((msg + "\n").encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()
    except Exception:
        pass


# ── NASA API ────────────────────────────────────────────────────────────────

def search_nasa_media(query: str) -> Optional[Dict]:
    """Search NASA Image and Video Library. Returns dict with url + media_type or None."""
    try:
        resp = requests.get(
            "https://images-api.nasa.gov/search",
            params={"q": query, "media_type": "video,image"},
            timeout=15,
        )
        resp.raise_for_status()
        items = resp.json().get("collection", {}).get("items", [])
        if not items:
            return None

        for item in items[:5]:
            data = item.get("data", [{}])[0]
            media_type = data.get("media_type", "")
            href = item.get("href", "")
            if not href:
                continue

            if media_type == "video":
                # Get the actual video file URL from the asset manifest
                try:
                    assets_resp = requests.get(href, timeout=10)
                    assets_resp.raise_for_status()
                    asset_urls = assets_resp.json()
                    # Prefer mp4 files, medium quality
                    for url in asset_urls:
                        if url.endswith(".mp4") and ("medium" in url or "orig" in url):
                            return {"url": url, "media_type": "video"}
                    for url in asset_urls:
                        if url.endswith(".mp4"):
                            return {"url": url, "media_type": "video"}
                except Exception:
                    pass

            elif media_type == "image":
                links = item.get("links", [])
                for link in links:
                    if link.get("rel") == "preview" and link.get("href"):
                        return {"url": link["href"], "media_type": "image"}

        return None
    except Exception as exc:
        _safe_print(f"[NASA] Search error: {exc}")
        return None


# ── Internet Archive API ───────────────────────────────────────────────────

def search_internet_archive(query: str) -> Optional[Dict]:
    """Search Internet Archive for video/image. Returns dict with url + media_type or None."""
    try:
        _safe_print(f"[InternetArchive] Searching: '{query}'")
        resp = requests.get(
            "https://archive.org/advancedsearch.php",
            params={
                "q": query,
                "fl[]": ["identifier", "title", "mediatype"],
                "rows": 5,
                "output": "json",
            },
            timeout=15,
        )
        resp.raise_for_status()
        docs = resp.json().get("response", {}).get("docs", [])
        if not docs:
            _safe_print(f"[InternetArchive] No results for '{query}'")
            return None

        for doc in docs:
            mediatype = doc.get("mediatype", "")
            identifier = doc.get("identifier", "")
            if not identifier:
                continue

            if mediatype == "movies":
                # Get file list to find an mp4
                try:
                    files_resp = requests.get(
                        f"https://archive.org/metadata/{identifier}/files",
                        timeout=10,
                    )
                    files_resp.raise_for_status()
                    files = files_resp.json().get("result", [])
                    for f in files:
                        name = f.get("name", "")
                        if name.endswith(".mp4"):
                            url = f"https://archive.org/download/{identifier}/{name}"
                            _safe_print(f"[InternetArchive] Found video: {identifier}/{name}")
                            return {"url": url, "media_type": "video"}
                except Exception:
                    pass

            elif mediatype == "image":
                try:
                    files_resp = requests.get(
                        f"https://archive.org/metadata/{identifier}/files",
                        timeout=10,
                    )
                    files_resp.raise_for_status()
                    files = files_resp.json().get("result", [])
                    for f in files:
                        name = f.get("name", "")
                        if name.lower().endswith((".jpg", ".jpeg", ".png")):
                            url = f"https://archive.org/download/{identifier}/{name}"
                            _safe_print(f"[InternetArchive] Found image: {identifier}/{name}")
                            return {"url": url, "media_type": "image"}
                except Exception:
                    pass

        _safe_print(f"[InternetArchive] No usable media for '{query}'")
        return None
    except Exception as exc:
        _safe_print(f"[InternetArchive] Search error: {exc}")
        return None


# ── NARA (National Archives) API ───────────────────────────────────────────

def search_nara(query: str) -> Optional[Dict]:
    """Search National Archives catalog via OPA API. Returns dict with url + media_type or None."""
    try:
        _safe_print(f"[NARA] Searching: '{query}'")
        resp = requests.get(
            "https://catalog.archives.gov/api/v1/",
            params={"q": query, "resultTypes": "item", "rows": 5},
            headers={"Accept": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "json" not in content_type:
            _safe_print(f"[NARA] API returned non-JSON ({content_type}), skipping")
            return None

        data = resp.json()
        results = (data.get("opaResponse", {})
                       .get("results", {})
                       .get("result", []))
        if not results:
            _safe_print(f"[NARA] No results for '{query}'")
            return None

        for item in results:
            objects = item.get("objects", {}).get("object", [])
            if isinstance(objects, dict):
                objects = [objects]
            for obj in objects:
                file_url = obj.get("file", {}).get("@url", "")
                mime = obj.get("file", {}).get("@mime", "")
                if not file_url:
                    continue
                if "video" in mime or file_url.endswith(".mp4"):
                    _safe_print(f"[NARA] Found video: {file_url[:80]}")
                    return {"url": file_url, "media_type": "video"}
                if "image" in mime or file_url.lower().endswith((".jpg", ".jpeg", ".png", ".gif")):
                    _safe_print(f"[NARA] Found image: {file_url[:80]}")
                    return {"url": file_url, "media_type": "image"}

        _safe_print(f"[NARA] No usable media for '{query}'")
        return None
    except Exception as exc:
        _safe_print(f"[NARA] Search error: {exc}")
        return None


# ── Clip bank ───────────────────────────────────────────────────────────────

def search_clip_bank(query: str, collection: str = "general") -> Optional[Dict]:
    """Search internal clip bank API.

    If collection == 'general', no collection filter is applied (returns clips
    from all collections). Otherwise, filters by the specific collection name.

    Returns dict with local_path + media_type, or None if not found.
    """
    clip_bank_url = settings.clip_bank_url if hasattr(settings, "clip_bank_url") else ""
    if not clip_bank_url:
        return None  # clip bank not configured

    try:
        params: dict = {"q": query, "limit": 5}
        if collection and collection != "general":
            params["collection"] = collection

        _safe_print(f"[ClipBank] Searching: '{query}'" +
                    (f" (collection={collection})" if collection != "general" else " (all collections)"))

        resp = requests.get(clip_bank_url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("results", [])
        if not items:
            _safe_print(f"[ClipBank] No results for '{query}'")
            return None

        item = items[0]
        file_path = item.get("file_path") or item.get("path") or item.get("url")
        media_type = item.get("media_type", "video")
        if file_path:
            _safe_print(f"[ClipBank] Found {media_type}: {file_path}")
            return {"local_path": file_path, "media_type": media_type}

        return None
    except Exception as exc:
        _safe_print(f"[ClipBank] Search error: {exc}")
        return None


# ── Download helper ─────────────────────────────────────────────────────────

def download_asset(url: str, dest: Path) -> bool:
    """Download a URL to local path. Returns True on success."""
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        resp = requests.get(url, timeout=120, stream=True)
        resp.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        # Verify file is not empty
        if dest.stat().st_size < 1000:
            _safe_print(f"[Download] File too small ({dest.stat().st_size}B): {dest}")
            dest.unlink(missing_ok=True)
            return False
        return True
    except Exception as exc:
        _safe_print(f"[Download] Failed: {exc}")
        dest.unlink(missing_ok=True)
        return False


# ── Main orchestrator ───────────────────────────────────────────────────────

def find_asset_for_scene(
    scene_id: int,
    analysis: Dict,
    project_dir: Path,
    collection: str = "general",
) -> Dict:
    """Find and download the best asset for a scene.

    If clip_bank_url is configured, delegates the entire search to the clip bank
    server (which handles the full search chain: local clips, YouTube, Pexels, etc.).
    Falls back to local search functions if the bank is unavailable.

    Returns:
        dict with: asset_type_found, asset_source, local_path, overlay_text
    """
    asset_type = analysis.get("asset_type", "stock_video")
    query = analysis.get("search_query", "")
    query_alt = analysis.get("search_query_alt", "")
    overlay_text = analysis.get("overlay_text") if analysis.get("has_overlay_text") else None

    assets_dir = project_dir / "assets"
    video_dest = assets_dir / f"scene_{scene_id}.mp4"
    image_dest = assets_dir / f"scene_{scene_id}.jpg"

    col_info = f" [col={collection}]" if collection != "general" else ""
    _safe_print(f"[StockSearch] Scene {scene_id}: type={asset_type}, query='{query}'{col_info}")

    result = {"asset_type_found": None, "asset_source": None, "local_path": None, "overlay_text": overlay_text}

    # AI image — handled by caller (pipeline_service), no search needed
    if asset_type == "ai_image":
        result["asset_type_found"] = "ai_image"
        result["asset_source"] = "pollinations"
        _safe_print(f"[StockSearch] Scene {scene_id}: marked for AI image generation")
        return result

    # ── Try clip bank (delegates entire search chain) ────────────────────────
    clip_bank_url = settings.clip_bank_url if hasattr(settings, "clip_bank_url") else ""
    if clip_bank_url:
        bank_result = _search_via_clip_bank(
            clip_bank_url, scene_id, query, query_alt, asset_type,
            collection, video_dest, image_dest,
        )
        if bank_result:
            result.update(**bank_result)
            _safe_print(
                f"[StockSearch] Scene {scene_id}: FOUND {result['asset_type_found']} "
                f"from {result['asset_source']} -> {result['local_path']}"
            )
            return result

    # ── Fallback: local search (clip bank unavailable) ───────────────────────
    _safe_print(f"[StockSearch] Scene {scene_id}: using local fallback search")
    if asset_type == "stock_video":
        result = _search_stock_video(scene_id, query, query_alt, video_dest, image_dest, result)
    elif asset_type == "archive_footage":
        result = _search_archive(scene_id, query, query_alt, video_dest, image_dest, result)
    elif asset_type == "space_media":
        result = _search_space(scene_id, query, query_alt, video_dest, image_dest, result)

    if result["asset_type_found"] and result["asset_type_found"] != "ai_image":
        _safe_print(
            f"[StockSearch] Scene {scene_id}: FOUND {result['asset_type_found']} "
            f"from {result['asset_source']} -> {result['local_path']}"
        )
    elif not result["asset_type_found"]:
        _safe_print(f"[StockSearch] Scene {scene_id}: NO ASSET FOUND, will use AI fallback")
        result["asset_type_found"] = "ai_image"
        result["asset_source"] = "pollinations"

    return result


def _search_via_clip_bank(
    bank_url: str, scene_id: int, query: str, query_alt: str,
    asset_type: str, collection: str, video_dest: Path, image_dest: Path,
) -> Optional[Dict]:
    """Call the clip bank's /api/clips/find endpoint. Returns partial result dict or None."""
    try:
        payload = {
            "query": query,
            "query_alt": query_alt,
            "collection": collection,
            "asset_type": asset_type,
            "min_duration": 3,
            "max_duration": 15,
        }
        _safe_print(f"[ClipBank] POST /api/clips/find  query='{query}' col={collection}")
        resp = requests.post(f"{bank_url}/api/clips/find", json=payload, timeout=60)

        if resp.status_code != 200:
            _safe_print(f"[ClipBank] HTTP {resp.status_code}")
            return None

        data = resp.json()
        if not data.get("found"):
            _safe_print(f"[ClipBank] No result for '{query}'")
            return None

        # Download the clip from the bank
        download_url = data.get("download_url", "")
        source = data.get("source", "clip_bank")
        media_type = data.get("media_type", "video")
        dest = video_dest if media_type == "video" else image_dest
        full_url = f"{bank_url}{download_url}" if download_url.startswith("/") else download_url

        _safe_print(f"[ClipBank] Found {media_type} from {source}, downloading...")
        if download_asset(full_url, dest):
            return {"asset_type_found": media_type, "asset_source": source, "local_path": str(dest)}

        _safe_print(f"[ClipBank] Download failed")
        return None

    except Exception as exc:
        _safe_print(f"[ClipBank] Error: {exc}")
        return None


def _search_stock_video(scene_id, query, query_alt, video_dest, image_dest, result):
    """Search Pexels → Pixabay for video, then images as fallback."""
    # 1. Pexels video — primary query
    url = _try_pexels_video(query)
    if url and download_asset(url, video_dest):
        result.update(asset_type_found="video", asset_source="pexels", local_path=str(video_dest))
        return result

    # 2. Pexels video — alt query
    if query_alt:
        url = _try_pexels_video(query_alt)
        if url and download_asset(url, video_dest):
            result.update(asset_type_found="video", asset_source="pexels", local_path=str(video_dest))
            return result

    # 3. Pixabay video — primary query
    url = _try_pixabay_video(query)
    if url and download_asset(url, video_dest):
        result.update(asset_type_found="video", asset_source="pixabay", local_path=str(video_dest))
        return result

    # 4. Pixabay video — alt query
    if query_alt:
        url = _try_pixabay_video(query_alt)
        if url and download_asset(url, video_dest):
            result.update(asset_type_found="video", asset_source="pixabay", local_path=str(video_dest))
            return result

    # 5. Pexels image — primary query
    url = _try_pexels_image(query)
    if url and download_asset(url, image_dest):
        result.update(asset_type_found="image", asset_source="pexels", local_path=str(image_dest))
        return result

    # 6. Pixabay image — primary query
    url = _try_pixabay_image(query)
    if url and download_asset(url, image_dest):
        result.update(asset_type_found="image", asset_source="pixabay", local_path=str(image_dest))
        return result

    return result


def _search_archive(scene_id, query, query_alt, video_dest, image_dest, result):
    """Archive footage: Internet Archive → NARA → IA alt → Pexels → Pixabay."""
    # 1. Internet Archive — primary query
    ia_result = search_internet_archive(query)
    if ia_result:
        dest = video_dest if ia_result["media_type"] == "video" else image_dest
        if download_asset(ia_result["url"], dest):
            result.update(asset_type_found=ia_result["media_type"], asset_source="internet_archive", local_path=str(dest))
            return result

    # 2. NARA — primary query
    nara_result = search_nara(query)
    if nara_result:
        dest = video_dest if nara_result["media_type"] == "video" else image_dest
        if download_asset(nara_result["url"], dest):
            result.update(asset_type_found=nara_result["media_type"], asset_source="nara", local_path=str(dest))
            return result

    # 3. Internet Archive — alt query
    if query_alt:
        ia_result = search_internet_archive(query_alt)
        if ia_result:
            dest = video_dest if ia_result["media_type"] == "video" else image_dest
            if download_asset(ia_result["url"], dest):
                result.update(asset_type_found=ia_result["media_type"], asset_source="internet_archive", local_path=str(dest))
                return result

    # 4. Fallback to Pexels/Pixabay stock
    _safe_print(f"[StockSearch] Scene {scene_id}: archive sources empty, trying stock")
    return _search_stock_video(scene_id, query, query_alt, video_dest, image_dest, result)


def _search_space(scene_id, query, query_alt, video_dest, image_dest, result):
    """Space media: try NASA first, then stock."""
    # 1. NASA API
    nasa_result = search_nasa_media(query)
    if nasa_result:
        url = nasa_result["url"]
        dest = video_dest if nasa_result["media_type"] == "video" else image_dest
        _safe_print(f"[StockSearch] Scene {scene_id}: NASA found {nasa_result['media_type']}")
        if download_asset(url, dest):
            result.update(
                asset_type_found=nasa_result["media_type"],
                asset_source="nasa",
                local_path=str(dest),
            )
            return result

    # 2. Fallback to stock
    _safe_print(f"[StockSearch] Scene {scene_id}: NASA empty, trying stock")
    return _search_stock_video(scene_id, query, query_alt, video_dest, image_dest, result)


# ── API wrappers with error handling ────────────────────────────────────────

def _try_pexels_video(query: str) -> Optional[str]:
    try:
        _safe_print(f"[Pexels] Searching video: '{query}'")
        url = pexels_service.search_video(query)
        if url:
            _safe_print(f"[Pexels] Found video for '{query}'")
        else:
            _safe_print(f"[Pexels] No video for '{query}'")
        return url
    except Exception as exc:
        _safe_print(f"[Pexels] Video search error: {exc}")
        return None


def _try_pexels_image(query: str) -> Optional[str]:
    try:
        _safe_print(f"[Pexels] Searching image: '{query}'")
        url = pexels_service.search_photo(query)
        if url:
            _safe_print(f"[Pexels] Found image for '{query}'")
        else:
            _safe_print(f"[Pexels] No image for '{query}'")
        return url
    except Exception as exc:
        _safe_print(f"[Pexels] Image search error: {exc}")
        return None


def _try_pixabay_video(query: str) -> Optional[str]:
    try:
        _safe_print(f"[Pixabay] Searching video: '{query}'")
        url = pixabay_service.search_video(query)
        if url:
            _safe_print(f"[Pixabay] Found video for '{query}'")
        else:
            _safe_print(f"[Pixabay] No video for '{query}'")
        return url
    except Exception as exc:
        _safe_print(f"[Pixabay] Video search error: {exc}")
        return None


def _try_pixabay_image(query: str) -> Optional[str]:
    try:
        _safe_print(f"[Pixabay] Searching image: '{query}'")
        url = pixabay_service.search_photo(query)
        if url:
            _safe_print(f"[Pixabay] Found image for '{query}'")
        else:
            _safe_print(f"[Pixabay] No image for '{query}'")
        return url
    except Exception as exc:
        _safe_print(f"[Pixabay] Image search error: {exc}")
        return None
