"""
Playwright verification script for vtc_app_v3.html report sheet UI.
Drives the per-row Trash button through all three states and captures screenshots.
Runs headless so screenshots capture the rendered page content reliably.
"""
import sys
from playwright.sync_api import sync_playwright

HTML_PATH = "/Users/simondavis/projects/very_thoughtful_compression/vtc/vtc_app_v3.html"
SS_DIR = "/Users/simondavis/projects/very_thoughtful_compression/vtc/verification/screenshots"

INJECT_JS = r"""
window.__vtcFileMgr = 'Finder';
window.__vtcReveal = function(){ return true; };
window.__vtcTrash = function(paths){
  return Promise.resolve({
    results: paths.map(function(p){ return {path:p, ok:true, where:'folder', dest:p}; }),
    trashed: 0, moved: paths.length, failed: 0,
    folders: ['/Volumes/Beast 8TB/Movies/VTC Trashed Files']
  });
};
RUN = { rows: [
  {f:"Splash (1984) [Bluray-1080p][DTS 5.1][x264]-AMIABLE.mp4", path:"/Volumes/Beast 8TB/Movies/Splash.mp4", t:"fail", d:"couldn't read", sev:"err", detail:"couldn't read this file — it may be corrupt or truncated", problem:true, sbytes:0, obytes:0},
  {f:"Heat (1995) [Bluray-1080p][AAC 5.1][x264].mp4", path:"/Volumes/Beast 8TB/Movies/Heat.mp4", t:"fail", d:"couldn't read", sev:"err", detail:"couldn't read this file", problem:true, sbytes:0, obytes:0},
  {f:"The Fifth Element (1997) [Bluray-1080p][AAC 5.1][x265]-SSDSSE.mp4", path:"/Volumes/Beast 8TB/Movies/Fifth.mp4", t:"fail", d:"couldn't read", sev:"err", detail:"couldn't read this file", problem:true, sbytes:0, obytes:0}
], done:120, skip:340, fail:3, failedRetryable:3, tb:1.2, mins:65, stopped:false };
rTab = 'fail'; trashStatus = null; trashArmed = false;
drawReport(); openSheet('#report-sheet');
"""

def run():
    with sync_playwright() as p:
        # Headless so page.screenshot captures the rendered content directly.
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()

        url = f"file://{HTML_PATH}"
        page.goto(url, wait_until="domcontentloaded")
        # Let DOMContentLoaded handlers and any deferred init settle.
        page.wait_for_timeout(1000)

        # ── STEP 1: inject stubs + fake run, open report sheet ──────────────
        page.evaluate(INJECT_JS)
        page.wait_for_timeout(700)

        # Verify sheet is open and rows rendered
        rows = page.query_selector_all(".r-row")
        print(f"Rows rendered: {len(rows)}")
        trash_btns = page.query_selector_all(".r-tbtn")
        print(f"Trash buttons found: {len(trash_btns)}")

        # Confirm the report sheet has the 'on' class (visible)
        sheet = page.query_selector("#report-sheet")
        sheet_class = sheet.get_attribute("class") if sheet else "MISSING"
        print(f"report-sheet class: {sheet_class!r}")

        # ── STEP 2: screenshot — initial state with all 3 rows ──────────────
        page.screenshot(path=f"{SS_DIR}/01_report_initial.png", full_page=False)
        print("Screenshot 1 saved: 01_report_initial.png")

        # ── STEP 3: click first-row Trash button (arm state) ────────────────
        first_btn = page.query_selector(".r-tbtn")
        assert first_btn, "No .r-tbtn found"
        first_btn.click()
        page.wait_for_timeout(400)

        # Verify arm state
        armed_btns = page.query_selector_all(".r-tbtn.armed")
        btn_text = first_btn.inner_text()
        print(f"Armed buttons after first click: {len(armed_btns)}, text='{btn_text}'")

        page.screenshot(path=f"{SS_DIR}/02_report_armed.png", full_page=False)
        print("Screenshot 2 saved: 02_report_armed.png")

        # ── STEP 4: click first-row Trash button again (commit) ─────────────
        armed_btn = page.query_selector(".r-tbtn.armed")
        assert armed_btn, "No armed button found for second click"
        armed_btn.click()
        page.wait_for_timeout(800)  # doTrash is async; give the Promise time to resolve

        # Verify: row count reduced, banner visible
        rows_after = page.query_selector_all(".r-row")
        print(f"Rows after commit: {len(rows_after)}")

        status_el = page.query_selector("#r-trash-status")
        status_hidden = status_el.get_attribute("hidden") if status_el else "element missing"
        status_text = status_el.inner_text() if status_el else ""
        print(f"Trash status hidden attr: {status_hidden!r}")
        print(f"Trash status text: {status_text!r}")

        page.screenshot(path=f"{SS_DIR}/03_report_committed.png", full_page=False)
        print("Screenshot 3 saved: 03_report_committed.png")

        browser.close()
        print("Done.")

if __name__ == "__main__":
    run()
