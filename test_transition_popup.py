"""
Test transition popup functionality using Playwright (sync API).
Navigates to the app, opens a project, finds transition markers, clicks one,
and captures screenshots at each step.
"""
import sys, time, os
from pathlib import Path

from playwright.sync_api import sync_playwright

SCREENSHOTS_DIR = Path(__file__).parent / "test_screenshots"
SCREENSHOTS_DIR.mkdir(exist_ok=True)

BASE_URL = "http://localhost:8000"


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1400, "height": 900},
            color_scheme="dark",
        )
        page = context.new_page()

        # ── Step 1: Navigate to homepage ─────────────────────────────────
        print("[1] Navigating to", BASE_URL)
        page.goto(BASE_URL, wait_until="networkidle")
        time.sleep(2)

        # ── Step 2: Screenshot of homepage ───────────────────────────────
        ss1 = str(SCREENSHOTS_DIR / "01_homepage.png")
        page.screenshot(path=ss1, full_page=True)
        print(f"[2] Screenshot saved: {ss1}")

        # ── Step 3: Check for projects ───────────────────────────────────
        project_cards = page.query_selector_all("#projectList .project-card")
        print(f"[3] Found {len(project_cards)} project card(s) on dashboard")

        if len(project_cards) == 0:
            print("    No project cards found. Trying to look for any clickable project elements...")
            # Try alternative selectors
            alt_items = page.query_selector_all("[data-project-id]")
            print(f"    Found {len(alt_items)} elements with data-project-id")
            if len(alt_items) == 0:
                # Dump the project list HTML for debugging
                pl_html = page.inner_html("#projectList")
                print(f"    projectList innerHTML (first 1000 chars): {pl_html[:1000]}")

        # ── Step 4: Find and click project 4 ─────────────────────────────
        target_project = None
        for card in project_cards:
            text = card.inner_text()
            pid = card.get_attribute("data-project-id") or ""
            onclick = card.get_attribute("onclick") or ""
            print(f"    Card: pid={pid}, onclick={onclick[:80]}, text={text[:60]}")
            if "4" in pid or "openProject(4" in onclick:
                target_project = card
                break

        if target_project is None and len(project_cards) >= 4:
            target_project = project_cards[3]  # 0-indexed, 4th project
            print("    Using 4th project card by index")

        if target_project is None and len(project_cards) > 0:
            # Try any project that has chunks / timeline
            target_project = project_cards[0]
            print("    No project 4 found, using first project instead")

        if target_project is None:
            # Last resort: try clicking via JS
            print("    Attempting to call openProject(4) via JS...")
            try:
                page.evaluate("openProject(4)")
                time.sleep(3)
            except Exception as ex:
                print(f"    JS openProject(4) failed: {ex}")
                # Try looking for project links / buttons
                links = page.query_selector_all("a, button, [onclick]")
                for lnk in links[:20]:
                    oc = lnk.get_attribute("onclick") or ""
                    txt = lnk.inner_text()[:50]
                    print(f"      element: onclick={oc[:60]} text={txt}")
                browser.close()
                return
        else:
            print(f"[4] Clicking on target project card")
            target_project.click()
            time.sleep(3)

        # ── Step 5: Screenshot of editing view ───────────────────────────
        ss2 = str(SCREENSHOTS_DIR / "02_editing_view.png")
        page.screenshot(path=ss2, full_page=True)
        print(f"[5] Screenshot saved: {ss2}")

        # ── Step 6: Look for the timeline area ───────────────────────────
        timeline = page.query_selector("#editingTimeline")
        if timeline:
            box = timeline.bounding_box()
            print(f"[6] Timeline found. BoundingBox: {box}")
            if box:
                ss3 = str(SCREENSHOTS_DIR / "03_timeline_area.png")
                # Capture a wider area to include context
                page.screenshot(
                    path=ss3,
                    clip={
                        "x": max(0, box["x"] - 20),
                        "y": max(0, box["y"] - 60),
                        "width": min(box["width"] + 40, 1400),
                        "height": min(box["height"] + 120, 400),
                    },
                )
                print(f"    Timeline screenshot saved: {ss3}")
        else:
            print("[6] Timeline (#editingTimeline) NOT found on page")
            # Try scrolling down or looking for alternative
            page.evaluate("window.scrollBy(0, 500)")
            time.sleep(1)

        # ── Step 7: Find transition markers ──────────────────────────────
        markers = page.query_selector_all(".transition-marker")
        print(f"[7] Found {len(markers)} transition marker(s)")

        if len(markers) == 0:
            print("    No transition markers found. Taking debug screenshot...")
            ss_debug = str(SCREENSHOTS_DIR / "04_no_markers_debug.png")
            page.screenshot(path=ss_debug, full_page=True)
            print(f"    Debug screenshot saved: {ss_debug}")
            # Print page HTML structure for debugging
            body_html = page.inner_html("body")
            print(f"    Body HTML length: {len(body_html)}")
            # Search for any elements related to timeline
            tl_clips = page.query_selector_all(".timeline-clip")
            print(f"    Timeline clips found: {len(tl_clips)}")
            browser.close()
            return

        # Print marker details
        for i, m in enumerate(markers):
            mbox = m.bounding_box()
            has_trans = "has-transition" in (m.get_attribute("class") or "")
            chunk_num = m.get_attribute("data-chunk-number") or "?"
            title = m.get_attribute("title") or ""
            print(f"    Marker {i}: chunk={chunk_num}, has_transition={has_trans}, title={title}, box={mbox}")

        # ── Step 8: Click first transition marker ────────────────────────
        marker_to_click = markers[0]
        print(f"[8] Clicking on transition marker 0...")

        # Scroll marker into view first
        marker_to_click.scroll_into_view_if_needed()
        time.sleep(0.5)

        marker_to_click.click()
        time.sleep(1)

        # ── Step 9: Screenshot after clicking marker ─────────────────────
        ss4 = str(SCREENSHOTS_DIR / "05_after_marker_click.png")
        page.screenshot(path=ss4, full_page=True)
        print(f"[9] Screenshot saved: {ss4}")

        # ── Step 10: Check if popup appeared ─────────────────────────────
        popup = page.query_selector(".transition-popup")
        if popup:
            pbox = popup.bounding_box()
            print(f"[10] TRANSITION POPUP FOUND! BoundingBox: {pbox}")
            popup_html = popup.inner_html()
            print(f"     Popup innerHTML (first 500 chars): {popup_html[:500]}")

            # Screenshot focused on popup
            if pbox:
                ss5 = str(SCREENSHOTS_DIR / "06_popup_closeup.png")
                page.screenshot(
                    path=ss5,
                    clip={
                        "x": max(0, pbox["x"] - 10),
                        "y": max(0, pbox["y"] - 10),
                        "width": min(pbox["width"] + 20, 400),
                        "height": min(pbox["height"] + 20, 500),
                    },
                )
                print(f"     Popup closeup screenshot saved: {ss5}")

            # Check for transition items in the grid
            items = popup.query_selector_all(".transition-item")
            print(f"     Transition items in grid: {len(items)}")
            for i, item in enumerate(items):
                label = item.query_selector(".transition-item-label")
                icon = item.query_selector(".transition-item-icon")
                is_active = "active" in (item.get_attribute("class") or "")
                lbl_text = label.inner_text() if label else "?"
                ico_text = icon.inner_text() if icon else "?"
                print(f"       Item {i}: icon={ico_text}, label={lbl_text}, active={is_active}")

            # Check duration slider
            slider = popup.query_selector("#trDurSlider")
            if slider:
                val = slider.get_attribute("value")
                print(f"     Duration slider value: {val}ms")

            # Check header
            header = popup.query_selector(".transition-popup-header")
            if header:
                print(f"     Popup header: {header.inner_text()}")
        else:
            print("[10] NO transition popup found after clicking marker!")
            # Check for bulk-transition-popup as alternative
            bulk = page.query_selector(".bulk-transition-popup")
            if bulk:
                print("     Found .bulk-transition-popup instead")

        # ── Step 11: Try clicking a transition item if popup is open ─────
        if popup:
            items = popup.query_selector_all(".transition-item")
            if len(items) > 0:
                print(f"[11] Clicking on first transition item to apply it...")
                items[0].click()
                time.sleep(2)
                ss6 = str(SCREENSHOTS_DIR / "07_after_transition_applied.png")
                page.screenshot(path=ss6, full_page=True)
                print(f"     Screenshot after applying transition: {ss6}")

                # Check if the marker now shows has-transition
                markers_after = page.query_selector_all(".transition-marker")
                if len(markers_after) > 0:
                    cls = markers_after[0].get_attribute("class") or ""
                    print(f"     First marker class after applying: {cls}")

        browser.close()
        print("\n[DONE] Test completed successfully!")


if __name__ == "__main__":
    run()
