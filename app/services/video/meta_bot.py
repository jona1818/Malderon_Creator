"""Meta AI Playwright bot – image animation via web automation.

Cookie/session is persisted in `meta_session/` at the project root.
For parallel execution, sessions are cloned to `meta_session_N/` directories.
Meta AI actively detects headless browsers, so we always run in headful mode.
"""
import asyncio
import shutil
from pathlib import Path
from playwright.async_api import async_playwright

# ── Paths ──────────────────────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
META_SESSION_DIR = _PROJECT_ROOT / "meta_session"

# ── Timeouts (milliseconds) ───────────────────────────────────────────────────
NAV_TIMEOUT = 120_000       # 2 min
ELEMENT_TIMEOUT = 120_000   # 2 min
GENERATION_TIMEOUT = 360_000  # 6 min

# ── Browser launch arguments ──────────────────────────────────────────────────
_BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-sandbox",
]

# ── Parallel session management ──────────────────────────────────────────────

def _worker_session_dir(worker_id: int) -> Path:
    """Return session directory for a specific worker."""
    if worker_id == 0:
        return META_SESSION_DIR
    return _PROJECT_ROOT / f"meta_session_{worker_id}"


def prepare_parallel_sessions(num_workers: int = 5):
    """Clone the main meta_session/ to N worker directories."""
    if not META_SESSION_DIR.exists():
        raise RuntimeError(
            f"Meta AI session not found at {META_SESSION_DIR}. "
            "Run `python run_meta_login.py` first."
        )
    for i in range(1, num_workers):
        dst = _worker_session_dir(i)
        if dst.exists():
            continue  # already created
        print(f"[META] Cloning session to {dst.name}...")
        shutil.copytree(str(META_SESSION_DIR), str(dst), dirs_exist_ok=True)
    print(f"[META] {num_workers} parallel sessions ready.")


# ── Login helper ──────────────────────────────────────────────────────────────

async def setup_meta_login():
    META_SESSION_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[META] Session directory: {META_SESSION_DIR}")

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(META_SESSION_DIR),
            headless=False,
            args=_BROWSER_ARGS,
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://www.meta.ai/")

        print("[META] Login window open — please log in and close the browser when done.")
        try:
            await page.wait_for_event("close", timeout=0)
        except Exception:
            pass
        finally:
            await ctx.close()

    print(f"[META] Session saved: {META_SESSION_DIR}")


# ── Selector helpers ──────────────────────────────────────────────────────────

async def _find_textarea(page):
    selectors = [
        '[aria-label*="message" i][contenteditable="true"]',
        'div[contenteditable="true"][role="textbox"]',
        'div[contenteditable="true"]',
        'textarea[aria-label*="message" i]',
        'textarea[placeholder]',
        'textarea',
    ]
    for sel in selectors:
        try:
            el = page.locator(sel).first
            await el.wait_for(state="visible", timeout=5_000)
            return el
        except Exception:
            continue
    return None


async def _attach_image(page, image_path: str) -> bool:
    # Strategy 1: direct file input
    try:
        fi = page.locator("input[type='file']").first
        await fi.wait_for(state="attached", timeout=10_000)
        await fi.set_input_files(image_path)
        return True
    except Exception:
        pass

    # Strategy 2: click attach button
    attach_selectors = [
        'button[aria-label*="ttach" i]',
        'button[aria-label*="image" i]',
        'button[aria-label*="photo" i]',
        'div[role="button"][aria-label*="ttach" i]',
        '[data-testid*="attach" i]',
        '[data-testid*="image-upload" i]',
        'label[for*="file"]',
        'label[for*="upload"]',
    ]
    for sel in attach_selectors:
        try:
            btn = page.locator(sel).first
            await btn.wait_for(state="visible", timeout=5_000)
            async with page.expect_file_chooser(timeout=10_000) as fc_info:
                await btn.click()
            fc = await fc_info.value
            await fc.set_files(image_path)
            return True
        except Exception:
            continue

    # Strategy 3: fallback
    try:
        await page.set_input_files("input[type='file']", image_path)
        return True
    except Exception:
        pass
    return False


async def _save_debug_screenshot(page, output_path: str, label: str) -> str:
    try:
        debug = str(Path(output_path).parent / f"meta_debug_{label}.png")
        await page.screenshot(path=debug, full_page=True)
        return debug
    except Exception:
        return "(screenshot failed)"


async def _download_video(page, video_el, output_path: str):
    """Try multiple strategies to download the generated video."""

    # Strategy A: Download button
    dl_selectors = [
        'div[role="button"][aria-label*="ownload" i]',
        'button[aria-label*="ownload" i]',
        'button:has-text("Download")',
        'button:has-text("Descargar")',
        '[data-testid*="download" i]',
        'a[download]',
        'a[href*=".mp4"]',
    ]
    for sel in dl_selectors:
        try:
            candidate = page.locator(sel).last
            await candidate.wait_for(state="visible", timeout=5_000)
            async with page.expect_download(timeout=ELEMENT_TIMEOUT) as dl_info:
                await candidate.click()
            download = await dl_info.value
            await download.save_as(output_path)
            return
        except Exception:
            continue

    # Strategy B: Extract video src URL
    import httpx as _httpx

    video_src = await video_el.get_attribute("src")
    if not video_src:
        try:
            source_el = video_el.locator("source").first
            video_src = await source_el.get_attribute("src")
        except Exception:
            pass

    if not video_src:
        video_src = await page.evaluate("""
            () => {
                const v = document.querySelector('video');
                return v ? v.src || v.currentSrc : null;
            }
        """)

    if video_src and video_src.startswith("http"):
        async with _httpx.AsyncClient(timeout=120.0) as http:
            r = await http.get(video_src)
            r.raise_for_status()
            Path(output_path).write_bytes(r.content)
        return

    scr = await _save_debug_screenshot(page, output_path, "no_src")
    raise RuntimeError(f"Could not extract video URL. Debug screenshot: {scr}")


# ── Single scene in an existing page ─────────────────────────────────────────

async def _animate_in_page(page, image_path: str, motion_prompt: str,
                           output_path: str, tag: str):
    """
    Animate one scene using an already-open browser page.
    Navigates to meta.ai (new chat), attaches image, sends prompt, downloads video.
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    print(f"{tag} Navigating to meta.ai…")
    await page.goto(
        "https://www.meta.ai/",
        wait_until="networkidle",
        timeout=NAV_TIMEOUT,
    )
    await asyncio.sleep(3)

    # Step 1: Attach image
    attached = await _attach_image(page, image_path)
    if not attached:
        scr = await _save_debug_screenshot(page, output_path, "no_attach")
        raise RuntimeError(f"Could not attach image. Debug: {scr}")
    await asyncio.sleep(2)

    # Step 2: Fill prompt
    textarea = await _find_textarea(page)
    if not textarea:
        scr = await _save_debug_screenshot(page, output_path, "no_textarea")
        raise RuntimeError(f"Could not find chat input. Debug: {scr}")

    full_prompt = (
        f"{motion_prompt}. "
        "Animate this image exactly as described. "
        "Do not change the art style, colors, or characters."
    )
    await textarea.click()
    await textarea.fill(full_prompt)
    await asyncio.sleep(1)
    await textarea.press("Enter")
    print(f"{tag} Prompt sent: {full_prompt[:80]}…")

    # Step 3: Wait for video
    video_el = None
    loop = asyncio.get_event_loop()
    deadline = loop.time() + (GENERATION_TIMEOUT / 1000)
    while loop.time() < deadline:
        try:
            vid = page.locator("video").last
            await vid.wait_for(state="visible", timeout=10_000)
            video_el = vid
            print(f"{tag} Video element detected!")
            break
        except Exception:
            pass
        await asyncio.sleep(5)

    if not video_el:
        scr = await _save_debug_screenshot(page, output_path, "no_video")
        raise RuntimeError(
            f"No video after {GENERATION_TIMEOUT // 1000}s. Debug: {scr}"
        )

    await asyncio.sleep(3)

    # Step 4: Download
    await _download_video(page, video_el, output_path)
    print(f"{tag} Animation saved: {output_path}")


# ── Standalone single-scene entry point (opens/closes browser) ───────────────

async def animate_scene(image_path: str, motion_prompt: str, output_path: str,
                        worker_id: int = 0):
    """
    Automate Meta AI to generate a short video clip from a still image.
    Opens and closes its own browser — use animate_batch() for multiple scenes.
    """
    session_dir = _worker_session_dir(worker_id)
    if not session_dir.exists():
        raise RuntimeError(f"Session dir not found: {session_dir}")

    tag = f"[META-W{worker_id}]"

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(session_dir),
            headless=False,
            args=_BROWSER_ARGS,
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.set_default_timeout(ELEMENT_TIMEOUT)
        try:
            await _animate_in_page(page, image_path, motion_prompt,
                                   output_path, tag)
        finally:
            await ctx.close()


# ── Batch parallel animation ─────────────────────────────────────────────────

async def _worker_loop(worker_id: int, queue: asyncio.Queue,
                       results: list, total: int,
                       on_scene_done=None):
    """
    Single worker: opens ONE browser, processes ALL queued scenes, then closes.
    The browser stays open between scenes — only navigates to a new meta.ai chat.
    """
    tag = f"[META-W{worker_id}]"
    session_dir = _worker_session_dir(worker_id)
    if not session_dir.exists():
        print(f"{tag} Session dir not found: {session_dir}, skipping worker.")
        return

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(session_dir),
            headless=False,
            args=_BROWSER_ARGS,
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.set_default_timeout(ELEMENT_TIMEOUT)

        try:
            while True:
                try:
                    item = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                chunk_number, image_path, motion_prompt, output_path = item
                try:
                    print(f"{tag} Starting scene #{chunk_number}…")
                    await _animate_in_page(page, image_path, motion_prompt,
                                           output_path, tag)
                    results.append((chunk_number, None))
                    if on_scene_done:
                        on_scene_done(chunk_number, None)
                    done = sum(1 for _, e in results if e is None)
                    print(f"{tag} Scene #{chunk_number} done ({done}/{total})")
                except Exception as exc:
                    results.append((chunk_number, str(exc)))
                    if on_scene_done:
                        on_scene_done(chunk_number, str(exc))
                    print(f"{tag} Scene #{chunk_number} FAILED: {exc}")
        finally:
            print(f"{tag} All tasks done, closing browser.")
            await ctx.close()


async def animate_batch(tasks: list[tuple], num_workers: int = 5,
                        on_scene_done=None):
    """
    Animate multiple scenes in parallel using N browser workers.
    Each worker opens ONE browser and reuses it for all its scenes.

    tasks: list of (chunk_number, image_path, motion_prompt, output_path)
    on_scene_done: optional callback(chunk_number, error_or_None) called after each scene
    Returns: list of (chunk_number, error_or_None)
    """
    prepare_parallel_sessions(num_workers)

    queue = asyncio.Queue()
    for t in tasks:
        queue.put_nowait(t)

    results: list[tuple[int, str | None]] = []
    total = len(tasks)

    workers = [
        _worker_loop(i, queue, results, total, on_scene_done=on_scene_done)
        for i in range(num_workers)
    ]
    await asyncio.gather(*workers)
    return results
