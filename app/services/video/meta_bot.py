"""Meta AI Playwright bot – image animation via web automation.

Cookie/session is persisted in `meta_session/` at the project root.
Meta AI actively detects headless browsers, so we always run in headful mode
(visible window). Debug screenshots are saved next to the output file when
selectors fail so errors are easy to diagnose.
"""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

# ── Paths ──────────────────────────────────────────────────────────────────────
# Absolute path calculated from this file so it never depends on CWD.
# Layout:  meta_bot.py → video/ → services/ → app/ → project_root/
META_SESSION_DIR = Path(__file__).resolve().parent.parent.parent.parent / "meta_session"

# ── Timeouts (milliseconds) ───────────────────────────────────────────────────
NAV_TIMEOUT = 120_000       # 2 min  — page.goto / networkidle
ELEMENT_TIMEOUT = 120_000   # 2 min  — wait_for(state="visible")
GENERATION_TIMEOUT = 360_000  # 6 min — waiting for Meta AI to finish generating

# ── Browser launch arguments ──────────────────────────────────────────────────
_BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-sandbox",
]


# ── Login helper ──────────────────────────────────────────────────────────────

async def setup_meta_login():
    """
    Open a visible (headful) browser so the user can log into Meta AI manually.
    The session cookies are saved in META_SESSION_DIR for future headless-ish runs.
    """
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
    """Return the first visible chat-input element, trying several selectors."""
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
    """
    Attach an image file to the Meta AI chat input.

    Strategy 1 — Direct hidden <input type="file">
    Strategy 2 — Click the attach/image button and intercept the file chooser
    Strategy 3 — page.set_input_files fallback
    """
    # Strategy 1: direct file input (sometimes exposed in the DOM)
    try:
        fi = page.locator("input[type='file']").first
        await fi.wait_for(state="attached", timeout=10_000)
        await fi.set_input_files(image_path)
        print("[META] Image attached via file input.")
        return True
    except Exception:
        pass

    # Strategy 2: click attach/image button and intercept file chooser
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
            print(f"[META] Image attached via button ({sel}).")
            return True
        except Exception:
            continue

    # Strategy 3: page-level fallback
    try:
        await page.set_input_files("input[type='file']", image_path)
        print("[META] Image attached via page.set_input_files fallback.")
        return True
    except Exception:
        pass

    return False


async def _save_debug_screenshot(page, output_path: str, label: str) -> str:
    """Save a full-page screenshot next to output_path for debugging. Returns path."""
    try:
        debug = str(Path(output_path).parent / f"meta_debug_{label}.png")
        await page.screenshot(path=debug, full_page=True)
        return debug
    except Exception:
        return "(screenshot failed)"


# ── Main animation entry point ────────────────────────────────────────────────

async def animate_scene(image_path: str, motion_prompt: str, output_path: str):
    """
    Automate Meta AI to generate a short video clip from a still image.

    - image_path   : path to the source image (JPEG/PNG)
    - motion_prompt: short animation instruction (e.g. "slow cinematic zoom in")
    - output_path  : where to save the downloaded MP4

    Raises RuntimeError on any failure. Debug screenshots are written next to
    output_path so you can inspect what Playwright saw.
    """
    if not META_SESSION_DIR.exists():
        raise RuntimeError(
            f"Meta AI session not found at {META_SESSION_DIR}. "
            "Please click 'Iniciar sesión con Meta AI' first."
        )

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(META_SESSION_DIR),
            headless=False,          # Meta AI blocks automated headless browsers
            args=_BROWSER_ARGS,
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.set_default_timeout(ELEMENT_TIMEOUT)

        try:
            print("[META] Navigating to meta.ai …")
            await page.goto(
                "https://www.meta.ai/",
                wait_until="networkidle",
                timeout=NAV_TIMEOUT,
            )
            await asyncio.sleep(3)  # let React hydrate

            # ── Step 1: Attach image ───────────────────────────────────────────
            attached = await _attach_image(page, image_path)
            if not attached:
                scr = await _save_debug_screenshot(page, output_path, "no_attach")
                raise RuntimeError(
                    f"Could not attach image to Meta AI. "
                    f"Debug screenshot saved: {scr} | Page title: {await page.title()}"
                )
            await asyncio.sleep(2)  # let upload propagate

            # ── Step 2: Fill the prompt ────────────────────────────────────────
            textarea = await _find_textarea(page)
            if not textarea:
                scr = await _save_debug_screenshot(page, output_path, "no_textarea")
                raise RuntimeError(
                    f"Could not find Meta AI chat input. "
                    f"Debug screenshot saved: {scr} | URL: {page.url}"
                )

            full_prompt = (
                f"{motion_prompt}. "
                "Animate this image exactly as described. "
                "Do not change the art style, colors, or characters."
            )
            await textarea.click()
            await textarea.fill(full_prompt)
            await asyncio.sleep(1)
            await textarea.press("Enter")
            print(f"[META] Prompt sent: {full_prompt[:100]}…")

            # ── Step 3: Wait for the Download button ──────────────────────────
            dl_selectors = [
                'div[role="button"][aria-label*="Download" i]',
                'button[aria-label*="Download" i]',
                'button:has-text("Download")',
                '[data-testid*="download" i]',
                'a[download]',
            ]
            print(
                f"[META] Waiting up to {GENERATION_TIMEOUT // 1000}s "
                "for video generation…"
            )
            dl_btn = None
            loop = asyncio.get_event_loop()
            deadline = loop.time() + (GENERATION_TIMEOUT / 1000)
            while loop.time() < deadline:
                for sel in dl_selectors:
                    try:
                        candidate = page.locator(sel).last
                        await candidate.wait_for(state="visible", timeout=8_000)
                        dl_btn = candidate
                        break
                    except Exception:
                        continue
                if dl_btn:
                    break
                await asyncio.sleep(5)

            if not dl_btn:
                scr = await _save_debug_screenshot(page, output_path, "no_download")
                raise RuntimeError(
                    f"Download button not found after {GENERATION_TIMEOUT // 1000}s. "
                    f"Debug screenshot: {scr}"
                )

            # ── Step 4: Download the video ─────────────────────────────────────
            async with page.expect_download(timeout=ELEMENT_TIMEOUT) as dl_info:
                await dl_btn.click()
            download = await dl_info.value
            await download.save_as(output_path)
            print(f"[META] ✓ Animation saved: {output_path}")

        finally:
            await ctx.close()
