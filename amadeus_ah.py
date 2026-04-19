#!/usr/bin/env python3
"""
Amadeus Altéa DCS FM Mobile – Air Algérie Ground Operations
=====================================================================
Automates login, flight search, passenger data extraction, and
final loadsheet retrieval using Playwright (Chromium).

Requirements:
    pip install playwright
    playwright install chromium
"""

import asyncio
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────
LOGIN_URL = (
    "https://www.accounts.mca.amadeus.com/LoginService/authorizeAngular"
    "?service=fm&client_id=1ASIHDFAH&response_mode=form_post"
    "&redirect_uri=https://afmgui.si.amadeus.net/1ASIHDFAH/fm/home/RampMobile"
    "&nonce=1345561146#/login"
)
HOME_URL     = "https://afmgui.si.amadeus.net/1ASIHDFAH/fm/home/RampMobile"
USERNAME     = "MBOUDINE"
ORGANIZATION = "AH"
PASSWORD     = "PROMOTION2026@"
CARRIER      = "AH"

_MONTHS = {
    1:"JAN",2:"FEB",3:"MAR",4:"APR",5:"MAY",6:"JUN",
    7:"JUL",8:"AUG",9:"SEP",10:"OCT",11:"NOV",12:"DEC",
}

# ──────────────────────────────────────────────────────────────────────────────
# Terminal helpers
# ──────────────────────────────────────────────────────────────────────────────
def _banner(title: str, width: int = 64) -> None:
    print("\n" + "═" * width)
    print(f"  {title}")
    print("═" * width)

def _info(msg: str) -> None: print(f"  [→] {msg}")
def _ok(msg: str)   -> None: print(f"  [✓] {msg}")
def _warn(msg: str) -> None: print(f"  [!] {msg}")
def _sep()          -> None: print("  " + "─" * 56)

def _today() -> str:
    d = datetime.now()
    return f"{d.day:02d}-{_MONTHS[d.month]}-{d.year}"


# ──────────────────────────────────────────────────────────────────────────────
# Frame helper – always returns the afmgui iframe (or main page as fallback)
# ──────────────────────────────────────────────────────────────────────────────
async def _app_frame(page: Page):
    for frame in page.frames:
        if "afmgui.si.amadeus.net" in frame.url:
            return frame
    for frame in page.frames:
        try:
            if await frame.query_selector("#applicationsLink, #tpl0_SEARCH_searchForm_flightNum_input"):
                return frame
        except Exception:
            pass
    return page


# ──────────────────────────────────────────────────────────────────────────────
# Step 1 – Login
# ──────────────────────────────────────────────────────────────────────────────
async def do_login(page: Page) -> None:
    _info("Navigating to login page …")
    await page.goto(LOGIN_URL, wait_until="load", timeout=90_000)
    await page.wait_for_selector("#userAliasInput", timeout=30_000)

    await page.fill("#userAliasInput", USERNAME)
    _info("Username filled")
    await page.fill("#organizationInput", ORGANIZATION)
    _info("Organisation filled")
    await page.fill("#passwordInput", PASSWORD)
    _info("Password filled")

    for sel in [
        'button[type="submit"]', 'input[type="submit"]',
        'button:has-text("Sign In")', 'button:has-text("Sign in")',
        'button:has-text("Log in")', 'button:has-text("Login")',
        'button:has-text("OK")', 'button:has-text("Connexion")',
    ]:
        try:
            await page.click(sel, timeout=4_000)
            _info(f"Submit clicked via {sel}")
            break
        except Exception:
            pass
    else:
        await page.keyboard.press("Enter")
        _info("Submit via Enter key")

    # ── Handle "Force Sign In" dialog if another session is active ────────────
    try:
        force_btn = await page.wait_for_selector(
            '#fosi_forceSignInButton, button:has-text("Force Sign In")',
            timeout=8_000,
        )
        _warn("Another session detected – clicking Force Sign In …")
        await force_btn.click()
        _ok("Force Sign In clicked.")
    except PWTimeout:
        pass   # No conflict dialog – normal login flow

    await page.wait_for_load_state("networkidle", timeout=60_000)
    _ok("Logged in.")


# ──────────────────────────────────────────────────────────────────────────────
# Step 2 – Contact Details → Done
# ──────────────────────────────────────────────────────────────────────────────
async def handle_contact_details(page: Page) -> None:
    """
    Click Done on the Contact Details page.
    The page can appear on the main document OR inside the app iframe,
    so we check both.  Retries up to 3 times in case of a stale element.
    """
    _info("Checking for Contact Details page …")

    DONE_SEL = (
        'button:has-text("Done"), '
        'button[id*="done" i], '
        'input[value="Done"], '
        'span.button:has-text("Done"), '
        ':has-text("Contact Details") ~ * button'
    )

    done_el = None

    # Search the main page first, then the app iframe
    for ctx in [page, await _app_frame(page)]:
        try:
            done_el = await ctx.wait_for_selector(DONE_SEL, timeout=15_000)
            if done_el:
                _info(f"Contact Details found in {'main page' if ctx is page else 'app frame'}")
                break
        except PWTimeout:
            continue

    if not done_el:
        _info("No Contact Details page detected – continuing.")
        return

    # Retry click up to 3 times (element can go stale during page transition)
    for attempt in range(3):
        try:
            await done_el.click()
            await page.wait_for_load_state("networkidle", timeout=30_000)
            await asyncio.sleep(1.5)
            _ok("Done clicked.")
            return
        except Exception as e:
            _warn(f"Done click attempt {attempt + 1} failed: {e}")
            await asyncio.sleep(1)
            # Re-query fresh element
            for ctx in [page, await _app_frame(page)]:
                try:
                    done_el = await ctx.query_selector(DONE_SEL)
                    if done_el:
                        break
                except Exception:
                    pass

    _warn("Could not click Done – continuing anyway.")


# ──────────────────────────────────────────────────────────────────────────────
# Modal / overlay dismissal – generic helper
# ──────────────────────────────────────────────────────────────────────────────
async def dismiss_any_modal(page: Page) -> None:
    """
    Dismiss any blocking Angular Bootstrap modal (ngb-modal-window) or
    generic overlay that might intercept pointer events.

    Strategy (in order):
      1. Click any visible Done / Close / OK / Cancel button inside a modal
      2. Press Escape
      3. Click the modal backdrop
      4. Force-remove the modal from the DOM via JS
    """
    _info("Checking for blocking modals …")

    MODAL_BTN_SEL = (
        'ngb-modal-window button:has-text("Done"), '
        'ngb-modal-window button:has-text("Close"), '
        'ngb-modal-window button:has-text("OK"), '
        'ngb-modal-window button:has-text("Cancel"), '
        'ngb-modal-window button[aria-label="Close"], '
        'ngb-modal-window .btn-primary, '
        'ngb-modal-window .btn-secondary, '
        '.modal button:has-text("Done"), '
        '.modal button:has-text("Close"), '
        '.modal button:has-text("OK"), '
        '.modal-footer button'
    )

    dismissed = False

    # Check both main page and iframe
    for ctx in [page, await _app_frame(page)]:
        # 1. Try clicking a button inside the modal
        try:
            btn = await ctx.query_selector(MODAL_BTN_SEL)
            if btn:
                _info("Modal button found – clicking …")
                try:
                    await btn.click(timeout=5_000)
                except Exception:
                    await btn.click(force=True, timeout=5_000)
                await asyncio.sleep(1.0)
                dismissed = True
                _ok("Modal dismissed via button click.")
                break
        except Exception:
            pass

        # 2. Check if a modal exists at all
        try:
            modal = await ctx.query_selector("ngb-modal-window, .modal.show, .modal.d-block")
            if not modal:
                continue
        except Exception:
            continue

        _warn("Modal detected but no button found – trying Escape …")
        # 3. Escape key
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.8)
        except Exception:
            pass

        # 4. Click backdrop
        try:
            backdrop = await ctx.query_selector("ngb-modal-backdrop, .modal-backdrop")
            if backdrop:
                await backdrop.click(force=True)
                await asyncio.sleep(0.8)
        except Exception:
            pass

        # 5. Force-remove via JS
        try:
            removed = await ctx.evaluate("""() => {
                let count = 0;
                for (const sel of ['ngb-modal-window', 'ngb-modal-backdrop',
                                   '.modal.show', '.modal-backdrop']) {
                    document.querySelectorAll(sel).forEach(el => {
                        el.remove(); count++;
                    });
                }
                // Also remove the 'modal-open' class from body which locks scrolling
                document.body.classList.remove('modal-open');
                document.body.style.removeProperty('overflow');
                document.body.style.removeProperty('padding-right');
                return count;
            }""")
            if removed:
                _ok(f"Force-removed {removed} modal element(s) via JS.")
                dismissed = True
        except Exception as e:
            _warn(f"JS modal removal failed: {e}")

    if not dismissed:
        _info("No blocking modal detected.")


# ──────────────────────────────────────────────────────────────────────────────
# Step 4 – Fill search form and submit
# ──────────────────────────────────────────────────────────────────────────────
async def _set_field(tf, field_id: str, value: str) -> None:
    """
    Set an Altéa xWidget input reliably.
    Strategy: click → select-all → delete → type char by char (triggers oninput).
    No Tab, no blur — moving focus is done by clicking the next field.
    """
    el = await tf.wait_for_selector(f"#{field_id}", timeout=8_000)
    await el.click()
    await asyncio.sleep(0.15)
    # Clear existing content
    await el.press("Control+a")
    await el.press("Delete")
    await asyncio.sleep(0.1)
    # Type character by character so oninput fires on each keystroke
    for ch in value:
        await el.press(ch)
        await asyncio.sleep(0.05)
    # Read back to confirm
    actual = await el.get_attribute("value") or await el.input_value()
    if actual.upper() != value.upper():
        _warn(f"#{field_id} shows '{actual}' after typing '{value}' – retrying with fill()")
        await el.click()
        await el.fill(value)


async def do_search(page: Page, flight_num: str, dep_port: str, date_str: str) -> None:
    _info("Waiting for search form …")
    tf = await _app_frame(page)
    _info(f"App frame: {getattr(tf, 'url', 'main')}")

    await tf.wait_for_selector("#tpl0_SEARCH_searchForm_flightNum_input", timeout=40_000)
    await asyncio.sleep(0.5)

    # ── 1. Flight number ───────────────────────────────────────────────────────
    await _set_field(tf, "tpl0_SEARCH_searchForm_flightNum_input", flight_num)
    _info(f"Flight number set: {flight_num}")

    # ── 2. Date – set via JS (avoids calendar popup) ──────────────────────────
    # First try known IDs, then auto-detect any date input in the search form.
    _SET_DATE_JS = """([id, val]) => {
        const el = id ? document.getElementById(id)
                      : (() => {
                            // Auto-detect: find any visible input whose id/name/
                            // placeholder contains 'date', or whose current value
                            // already looks like a date string.
                            for (const inp of document.querySelectorAll('input')) {
                                const attr = (inp.id + inp.name + inp.placeholder).toLowerCase();
                                if (attr.includes('date') || attr.includes('fecha') ||
                                    /^\\d{1,2}[\\-\\/A-Z]/.test(inp.value)) {
                                    return inp;
                                }
                            }
                            return null;
                        })();
        if (!el) return '';
        const nativeSet = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'value').set;
        nativeSet.call(el, val);
        // Fire every event Angular / xWidget might listen to
        ['input','change','keydown','keyup','blur'].forEach(t =>
            el.dispatchEvent(new Event(t, {bubbles: true}))
        );
        return el.id || 'auto';
    }"""

    date_set = False
    for date_id in [
        "tpl0_SEARCH_searchForm_flightDate_input",
        "tpl0_SEARCH_searchForm_date_input",
        "tpl0_SEARCH_searchForm_departureDate_input",
        "tpl0_SEARCH_searchForm_schedDate_input",
        "",   # empty string → auto-detect fallback
    ]:
        result = await tf.evaluate(_SET_DATE_JS, [date_id, date_str])
        if result:
            _info(f"Date set (field='{result}'): {date_str}")
            date_set = True
            break

    if not date_set:
        _warn("Date field not found by any method – proceeding without date filter")

    # ── 3. Departure port ──────────────────────────────────────────────────────
    await _set_field(tf, "tpl0_SEARCH_searchForm_departurePort_input", dep_port)
    _info(f"Departure port set: {dep_port}")

    # ── 4. Click flight-number field to trigger global re-validation ───────────
    fn_el = await tf.query_selector("#tpl0_SEARCH_searchForm_flightNum_input")
    if fn_el:
        await fn_el.click()
    await asyncio.sleep(0.8)   # let Angular validate & enable Search button

    # ── 5. Verify ALL field values; re-force any that got cleared ────────────────
    _FORCE_JS = """([id, val]) => {
        const el = document.getElementById(id);
        if (!el) return;
        const nativeSet = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'value').set;
        nativeSet.call(el, val);
        ['input','change','keydown','keyup','blur'].forEach(t =>
            el.dispatchEvent(new Event(t, {bubbles:true}))
        );
    }"""

    fn_val  = await tf.evaluate("() => document.getElementById('tpl0_SEARCH_searchForm_flightNum_input')?.value || ''")
    dep_val = await tf.evaluate("() => document.getElementById('tpl0_SEARCH_searchForm_departurePort_input')?.value || ''")
    # Try to read date field (any of the known IDs)
    date_val = await tf.evaluate("""() => {
        const ids = ['tpl0_SEARCH_searchForm_flightDate_input',
                     'tpl0_SEARCH_searchForm_date_input',
                     'tpl0_SEARCH_searchForm_departureDate_input',
                     'tpl0_SEARCH_searchForm_schedDate_input'];
        for (const id of ids) {
            const el = document.getElementById(id);
            if (el && el.value) return el.value;
        }
        return '';
    }""")
    _info(f"Fields before search — Flight:'{fn_val}'  Dep:'{dep_val}'  Date:'{date_val}'")

    if fn_val.strip() != flight_num.strip():
        _warn(f"Flight field wrong ('{fn_val}') – re-forcing …")
        await tf.evaluate(_FORCE_JS, ["tpl0_SEARCH_searchForm_flightNum_input", flight_num])
        await asyncio.sleep(0.3)

    if dep_val.upper() != dep_port.upper():
        _warn(f"Dep port wrong ('{dep_val}') – re-forcing …")
        await tf.evaluate(_FORCE_JS, ["tpl0_SEARCH_searchForm_departurePort_input", dep_port])
        await asyncio.sleep(0.3)

    if date_val and date_val.upper() != date_str.upper():
        _warn(f"Date wrong ('{date_val}') – re-forcing to '{date_str}' …")
        for date_id in ["tpl0_SEARCH_searchForm_flightDate_input",
                        "tpl0_SEARCH_searchForm_date_input",
                        "tpl0_SEARCH_searchForm_departureDate_input",
                        "tpl0_SEARCH_searchForm_schedDate_input"]:
            await tf.evaluate(_FORCE_JS, [date_id, date_str])
        await asyncio.sleep(0.3)

    _info(f"Searching AH{flight_num} / dep:{dep_port} / {date_str} …")

    # ── 6. Click Search ────────────────────────────────────────────────────────
    search_btn = await tf.wait_for_selector('span:text-is("Search")', timeout=10_000)
    try:
        await search_btn.click(timeout=10_000)
    except Exception:
        _warn("Normal click on Search failed – using force=True")
        await search_btn.click(force=True)

    await page.wait_for_load_state("networkidle", timeout=40_000)
    _ok("Search submitted.")


# ──────────────────────────────────────────────────────────────────────────────
# Step 5 – Click the matching flight row
# ──────────────────────────────────────────────────────────────────────────────
async def select_flight_row(page: Page, flight_num: str, dep_port: str) -> bool:
    _info("Waiting for search results …")
    tf = await _app_frame(page)

    # Wait for either a result row or the "no flights" message (retry once)
    found_results = False
    for attempt in range(2):
        try:
            await tf.wait_for_selector(
                '#flightsearch_result0, :text("No flights matching")',
                timeout=35_000,
            )
            found_results = True
            break
        except PWTimeout:
            if attempt == 0:
                _warn("Timeout waiting for results – retrying search click …")
                # Re-click the Search button in case it wasn't submitted properly
                try:
                    s_btn = await tf.query_selector('span:text-is("Search")')
                    if s_btn:
                        await s_btn.click(force=True)
                        await page.wait_for_load_state("networkidle", timeout=30_000)
                except Exception:
                    pass
            else:
                _warn("Timeout waiting for results (both attempts).")

    if not found_results:
        return False

    await asyncio.sleep(0.5)

    # Check if result row exists
    row = await tf.query_selector("#flightsearch_result0")
    if not row:
        body = await tf.inner_text("body")
        if re.search(r"no flights matching", body, re.IGNORECASE):
            _warn("No flights matching your search criteria.")
            _warn("Check flight number, departure port, and date.")
        else:
            _warn("No result rows found.")
        return False

    snippet = (await row.inner_text()).replace("\n", " ").strip()[:100]
    _info(f"Found: {snippet}")
    _info("Clicking flight row …")
    await row.click()
    await page.wait_for_load_state("networkidle", timeout=40_000)
    # Wait for any splash/loading screen to disappear before next step
    await _wait_splash_gone(page)
    _ok("Flight row clicked.")
    return True


# ──────────────────────────────────────────────────────────────────────────────
# Step 6 – Navigate via header tab buttons
# (HeaderDOCUMENT / HeaderPASSENGER are always-visible tabs in the top bar,
#  NOT items inside a hamburger dropdown)
# ──────────────────────────────────────────────────────────────────────────────
async def _wait_splash_gone(page: Page, timeout: int = 30_000) -> None:
    """Wait until the full-screen splash/loading overlay is gone."""
    tf = await _app_frame(page)
    try:
        await tf.wait_for_selector(
            "#splashScreenContainer", state="hidden", timeout=timeout
        )
        _info("Splash screen gone.")
    except PWTimeout:
        pass   # If it never appeared that's fine too
    await asyncio.sleep(1.0)


async def _click_apps_then_header(page: Page, btn_id: str, label: str) -> None:
    """
    Altéa navigation flow:
      1. Click #applicationsLink  (the apps / hamburger icon)
      2. Click the target header tab  (e.g. #HeaderPASSENGER, #HeaderDOCUMENT)

    The apps icon click is retried up to 3 times with force=True to handle
    transient overlay / stale-element situations.
    """
    await _wait_splash_gone(page)

    # ── Step 1: click the apps icon (with retry) ──────────────────────────────
    _info("Clicking apps icon (#applicationsLink) …")
    apps_clicked = False
    for attempt in range(3):
        tf = await _app_frame(page)   # re-query frame each attempt
        for apps_sel in ["#applicationsLink", '[class*="amadeusIcon"]', ".amadeusIcon"]:
            try:
                apps_btn = await tf.wait_for_selector(
                    apps_sel, state="visible", timeout=6_000
                )
                if not apps_btn:
                    continue
                await apps_btn.scroll_into_view_if_needed()
                await asyncio.sleep(0.2)
                try:
                    await apps_btn.click(timeout=4_000)
                except Exception:
                    await apps_btn.click(force=True, timeout=4_000)
                _info(f"Apps icon clicked (attempt {attempt + 1}, sel={apps_sel})")
                apps_clicked = True
                break
            except Exception as e:
                _warn(f"Apps click attempt {attempt + 1} via '{apps_sel}': {e}")
        if apps_clicked:
            break
        await asyncio.sleep(0.6)

    await asyncio.sleep(1.0)   # let header tabs become active / visible

    # ── Step 2: click the target header tab ───────────────────────────────────
    tf = await _app_frame(page)
    selectors = [
        f"#{btn_id}",
        f'[id*="{label.upper()}"]',
        f'[id*="{label.lower()}"]',
        f'.headerButton:has-text("{label}")',
        f'span.headerButton:text-is("{label}")',
        f'span:text-is("{label}")',
        f':text("{label}")',
    ]
    btn = None
    for sel in selectors:
        try:
            btn = await tf.wait_for_selector(sel, state="visible", timeout=6_000)
            if btn:
                _info(f"Header tab found via: {sel}")
                break
        except Exception:
            pass

    if not btn:
        _warn(f"Header tab '{label}' not found – skipping.")
        return

    await btn.click()
    await page.wait_for_load_state("networkidle", timeout=40_000)
    await asyncio.sleep(1.0)
    _ok(f"'{label}' view opened.")


async def _find_refresh_btn(tf):
    """
    Find the circular refresh button shown in the Passenger view.
    Tries ID/class selectors first, then falls back to SVG path content detection.
    Returns the element or None.
    """
    # CSS-based attempts
    css_selectors = [
        "#refreshButton", "#reload", "#refresh",
        '[id*="refresh" i]', '[id*="reload" i]',
        '[title*="Refresh" i]', '[title*="Reload" i]',
        '[aria-label*="Refresh" i]', '[aria-label*="Reload" i]',
        '.refreshButton', '.refreshIcon', '.reloadButton',
    ]
    for sel in css_selectors:
        try:
            el = await tf.query_selector(sel)
            if el:
                return el
        except Exception:
            pass

    # JS fallback: walk up from the SVG <path> whose d-attribute contains
    # the distinctive fragment "14.133,28.265" (the refresh icon path)
    el = await tf.evaluate_handle("""() => {
        for (const path of document.querySelectorAll('path')) {
            const d = path.getAttribute('d') || '';
            if (d.includes('14.133') && d.includes('28.265')) {
                // Walk up the DOM to find the nearest clickable ancestor
                let node = path.parentElement;
                while (node && node.tagName !== 'BODY') {
                    const tag = node.tagName.toLowerCase();
                    if (tag === 'button' || tag === 'a' ||
                        node.getAttribute('onclick') ||
                        node.getAttribute('atdelegate') ||
                        node.getAttribute('role') === 'button') {
                        return node;
                    }
                    node = node.parentElement;
                }
                // Fallback: return the <svg> element itself
                return path.closest('svg') || path.parentElement;
            }
        }
        return null;
    }""")

    # evaluate_handle returns a JSHandle; check it's a real element
    try:
        if el and await el.evaluate("n => n !== null"):
            # Convert JSHandle → ElementHandle via as_element()
            as_el = el.as_element()
            return as_el
    except Exception:
        pass
    return None


async def open_passenger_view(page: Page) -> None:
    await _click_apps_then_header(page, "HeaderPASSENGER", "Passenger")
    # Wait for a passenger-view specific element so we know we're actually there
    tf = await _app_frame(page)
    for pax_sel in [
        '#passengerView', '#paxView', '[id*="passenger" i]',
        ':text("PAX")', ':text("Passengers")', ':text("PASSENGERS")',
        '[class*="passenger" i]',
    ]:
        try:
            await tf.wait_for_selector(pax_sel, state="visible", timeout=5_000)
            _info(f"Passenger view confirmed via: {pax_sel}")
            break
        except Exception:
            pass
    await asyncio.sleep(1.0)


# ──────────────────────────────────────────────────────────────────────────────
# Output helpers
# ──────────────────────────────────────────────────────────────────────────────
OUTPUT_DIR = Path("C:/Users/n/Downloads/script/reports")

def _ensure_output_dir() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

def _report_path(flight_num: str, dep_port: str, date_str: str, suffix: str, ext: str) -> Path:
    safe_date = date_str.replace("-", "")
    name = f"AH{flight_num}_{dep_port}_{safe_date}_{suffix}.{ext}"
    return OUTPUT_DIR / name

async def _expand_for_print(page: Page) -> None:
    """
    Remove overflow / height clipping from every element so that
    CDP Page.printToPDF captures ALL content (scrollable areas included).
    Runs JS both in the main page and inside the app iframe.
    """
    _EXPAND_JS = """() => {
        const SCROLL_VALS = ['scroll', 'auto', 'hidden'];
        const els = document.querySelectorAll('*');
        for (const el of els) {
            try {
                const cs = window.getComputedStyle(el);
                if (SCROLL_VALS.includes(cs.overflow)  ||
                    SCROLL_VALS.includes(cs.overflowY) ||
                    SCROLL_VALS.includes(cs.overflowX)) {
                    el.style.setProperty('overflow',   'visible', 'important');
                    el.style.setProperty('overflow-y', 'visible', 'important');
                    el.style.setProperty('overflow-x', 'visible', 'important');
                    el.style.setProperty('max-height', 'none',    'important');
                    el.style.setProperty('height',     'auto',    'important');
                }
            } catch(_) {}
        }
        // Also force the document root
        document.documentElement.style.setProperty('overflow',   'visible', 'important');
        document.documentElement.style.setProperty('height',     'auto',    'important');
        document.body.style.setProperty('overflow', 'visible', 'important');
        document.body.style.setProperty('height',   'auto',    'important');
        // Scroll to bottom to trigger any lazy-loaded content, then back to top
        window.scrollTo(0, document.body.scrollHeight);
    }"""

    # 1. Expand inside the app iframe first
    tf = await _app_frame(page)
    if tf is not page:
        await tf.evaluate(_EXPAND_JS)
        await asyncio.sleep(0.4)
        # Scroll all the way to the bottom inside the frame, then top
        await tf.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(0.3)
        await tf.evaluate("() => window.scrollTo(0, 0)")
        await asyncio.sleep(0.2)

    # 2. Expand the main page and try to stretch the iframe element to full height
    await page.evaluate("""() => {
        // Remove clipping on the main page
        document.documentElement.style.setProperty('overflow', 'visible', 'important');
        document.documentElement.style.setProperty('height',   'auto',    'important');
        document.body.style.setProperty('overflow', 'visible', 'important');
        document.body.style.setProperty('height',   'auto',    'important');

        // Attempt to resize each iframe to its content height
        // (will silently fail for cross-origin; that's fine — Chromium's PDF
        //  renderer can still reach into same-process frames)
        document.querySelectorAll('iframe').forEach(iframe => {
            try {
                const h = iframe.contentDocument?.documentElement?.scrollHeight
                       || iframe.contentWindow?.document?.documentElement?.scrollHeight;
                if (h && h > 300) {
                    iframe.style.setProperty('height',     h + 200 + 'px', 'important');
                    iframe.style.setProperty('min-height', h + 200 + 'px', 'important');
                    iframe.style.setProperty('max-height', 'none',          'important');
                }
            } catch(_) {}
        });
    }""")
    await asyncio.sleep(0.5)   # let the browser reflow


async def _save_pdf(page: Page, path: Path, *, wide: bool = False) -> None:
    """
    Save the current page as a PDF using Chromium's built-in print-to-PDF.
    Works in both headless and headed mode via CDP.

    `wide=True` uses landscape A4 — handy for wide monospaced loadsheets.
    """
    try:
        # Choose paper dimensions
        if wide:
            pw, ph = 11.69, 8.27   # A4 landscape
        else:
            pw, ph = 8.27, 11.69   # A4 portrait

        cdp = await page.context.new_cdp_session(page)
        result = await cdp.send("Page.printToPDF", {
            "printBackground":    True,
            "paperWidth":         pw,
            "paperHeight":        ph,
            "marginTop":          0.4,
            "marginBottom":       0.4,
            "marginLeft":         0.4,
            "marginRight":        0.4,
            "scale":              0.85,
            "preferCSSPageSize":  False,   # use our paper size, not CSS @page
        })
        await cdp.detach()
        import base64
        pdf_bytes = base64.b64decode(result["data"])
        path.write_bytes(pdf_bytes)
        _ok(f"PDF saved → {path}")
    except Exception as e:
        _warn(f"PDF via CDP failed ({e})")

def _save_text(content: str, path: Path) -> None:
    path.write_text(content, encoding="utf-8")
    _ok(f"Text saved → {path}")


def _save_loadsheet_pdf(text: str, path: Path) -> None:
    """
    Generate a PDF from the extracted loadsheet text using reportlab.

    Output is identical to the .txt file — Courier 9 pt, black on white,
    A4 landscape so wide columns are never wrapped.  No browser involved.
    """
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.pdfgen import canvas as rl_canvas

    PAGE_W, PAGE_H = landscape(A4)   # 841.9 × 595.3 pt  (≈ 297 × 210 mm)
    MARGIN      = 28.3               # 10 mm in points
    FONT        = "Courier"
    FONT_SIZE   = 9
    LINE_H      = FONT_SIZE * 1.25   # 11.25 pt leading

    c = rl_canvas.Canvas(str(path), pagesize=landscape(A4))
    c.setFont(FONT, FONT_SIZE)

    x = MARGIN
    y = PAGE_H - MARGIN   # start at top-left

    for raw_line in text.splitlines():
        # New page when we run out of vertical space
        if y < MARGIN + LINE_H:
            c.showPage()
            c.setFont(FONT, FONT_SIZE)
            y = PAGE_H - MARGIN

        # Replace any characters that Courier cannot encode (keep ASCII safe)
        safe = raw_line.encode("latin-1", errors="replace").decode("latin-1")
        c.drawString(x, y, safe)
        y -= LINE_H

    c.save()
    _ok(f"Loadsheet PDF saved → {path}")


async def _page_structured_text(page: Page) -> str:
    """
    Extract text from the page preserving visual structure:
    table rows become pipe-separated lines, block elements become new lines.
    """
    tf = await _app_frame(page)
    text = await tf.evaluate("""() => {
        function walk(node, lines, indent) {
            if (node.nodeType === 3) {
                const t = node.textContent.trim();
                if (t) lines.push(' '.repeat(indent) + t);
                return;
            }
            if (node.nodeType !== 1) return;
            const tag = node.tagName.toLowerCase();
            const skip = ['script','style','noscript','svg','path','polygon','circle'];
            if (skip.includes(tag)) return;

            // Table row → join cells with |
            if (tag === 'tr') {
                const cells = [...node.querySelectorAll('td,th')].map(c =>
                    c.innerText.replace(/\\n/g,' ').trim()
                );
                lines.push(cells.join('  |  '));
                return;
            }
            // Block-level elements → newline before children
            const block = ['div','p','br','h1','h2','h3','h4','section',
                           'article','header','footer','table','thead','tbody'];
            if (tag === 'br') { lines.push(''); return; }
            if (block.includes(tag)) {
                for (const child of node.childNodes) walk(child, lines, indent);
                lines.push('');
                return;
            }
            // Inline – just recurse
            for (const child of node.childNodes) walk(child, lines, indent);
        }
        const lines = [];
        walk(document.body, lines, 0);
        // Collapse 3+ blank lines to 1
        return lines
            .join('\\n')
            .replace(/\\n{3,}/g, '\\n\\n')
            .trim();
    }""")
    return text


# ──────────────────────────────────────────────────────────────────────────────
# Step 7 – Extract and display passenger information + save PDF & text
# ──────────────────────────────────────────────────────────────────────────────
async def extract_passenger_data(
    page: Page, flight_num: str, dep_port: str, date_str: str
) -> str:
    _banner("PASSENGER INFORMATION")

    # Expand all scrollable containers so CDP captures the full passenger list
    await _expand_for_print(page)

    # Structured text that mirrors the visual layout of the page
    text = await _page_structured_text(page)

    # ── Strip login-page text if it leaked in as an overlay ──────────────────
    _LOGIN_MARKERS = (
        "Please enter your details to log in",
        "Login + Organization",
        "Forgot your password",
        "Amadeus Copyright 1999",
    )

    def _strip_login(raw: str) -> str:
        if not any(m in raw for m in _LOGIN_MARKERS):
            return raw
        _warn("Login page text detected in passenger output – stripping …")
        lines = raw.splitlines()
        last_login_line = -1
        for i, line in enumerate(lines):
            if any(m in line for m in _LOGIN_MARKERS):
                last_login_line = i
        return "\n".join(lines[last_login_line + 1:]).strip() if last_login_line >= 0 else raw

    text = _strip_login(text)

    # ── Retry once if text is still empty (page may not have loaded yet) ──────
    if not text.strip():
        _warn("Passenger text empty – waiting 5 s and retrying extraction …")
        await asyncio.sleep(5)
        await _expand_for_print(page)
        text = _strip_login(await _page_structured_text(page))

    # Print to terminal
    for line in text.splitlines():
        print(f"  {line}")

    # Save text file
    _ensure_output_dir()
    txt_path = _report_path(flight_num, dep_port, date_str, "passenger", "txt")
    _save_text(text, txt_path)

    # Save PDF (faithful visual copy via CDP print-to-PDF)
    pdf_path = _report_path(flight_num, dep_port, date_str, "passenger", "pdf")
    await _save_pdf(page, pdf_path)

    return text


# ──────────────────────────────────────────────────────────────────────────────
# Step 8 – Final Loadsheet
# Called only when the flight is confirmed CLOSED.
# Flow: Documents tab → click Final Loadsheet → expand → extract → PDF + TXT
# ──────────────────────────────────────────────────────────────────────────────
async def get_final_loadsheet(
    page: Page, flight_num: str, dep_port: str, date_str: str,
) -> bool:
    """
    Navigate to Documents and look for the Final Loadsheet button.

    Returns
    -------
    True  – loadsheet found, extracted, and saved as PDF + TXT.
    False – button not present → flight is NOT closed yet.
    """
    # ── 1. Click apps icon → Documents header tab ────────────────────────────
    await _click_apps_then_header(page, "HeaderDOCUMENT", "Documents")

    # ── 2. Find the Final Loadsheet button in the document list ──────────────
    tf = await _app_frame(page)
    ls_btn = None
    for ls_sel in [
        '.documentListInnerButton:has-text("Final Loadsheet")',
        'button:has-text("Final Loadsheet")',
        'span:has-text("Final Loadsheet")',
        ':text("Final Loadsheet")',
        ':text("LOADSHEET")',
    ]:
        try:
            ls_btn = await tf.wait_for_selector(ls_sel, state="visible", timeout=6_000)
            if ls_btn:
                _info(f"Loadsheet button found via: {ls_sel}")
                break
        except Exception:
            pass

    if not ls_btn:
        # No loadsheet button → flight is not closed
        return False

    # ── 3. Open the loadsheet ─────────────────────────────────────────────────
    _info("Opening Final Loadsheet …")
    await ls_btn.click()
    await page.wait_for_load_state("networkidle", timeout=40_000)
    await asyncio.sleep(2.0)   # let the document fully render

    # ── 4. Expand ALL scrollable containers before extracting / printing ───────
    # The loadsheet spans multiple screen heights; overflow:hidden clips it.
    # _expand_for_print() removes those constraints so CDP sees the full DOM.
    _info("Expanding page for full-content capture …")
    await _expand_for_print(page)

    # ── 5. Extract full text and print to terminal ────────────────────────────
    text = await _page_structured_text(page)
    for line in text.splitlines():
        print(f"  {line}")

    # ── 6. Save TXT + PDF ─────────────────────────────────────────────────────
    _ensure_output_dir()
    txt_path = _report_path(flight_num, dep_port, date_str, "loadsheet", "txt")
    pdf_path = _report_path(flight_num, dep_port, date_str, "loadsheet", "pdf")
    _save_text(text, txt_path)
    # PDF built from extracted text via reportlab — Courier font, identical
    # to the .txt file, NOT a screenshot of the webpage.
    _save_loadsheet_pdf(text, pdf_path)
    return True


# ──────────────────────────────────────────────────────────────────────────────
# LIVE PASSENGER MONITOR
# ──────────────────────────────────────────────────────────────────────────────
async def live_passenger_monitor(
    page: Page, flight_num: str, dep_port: str, date_str: str,
    interval: int = 15,
) -> None:
    """
    Stay on the Passenger view.
    Every `interval` seconds (default 15) click the circular refresh button
    to reload data from the server, then redisplay the updated passenger list.
    Press Ctrl+C to stop.
    """
    print(
        f"\n  ┌─ LIVE MONITOR  AH{flight_num} {dep_port} {date_str} ──────────────────┐"
        f"\n  │  Refreshing every {interval}s via refresh button.                   │"
        f"\n  │  Press Ctrl+C to stop.                                              │"
        f"\n  └─────────────────────────────────────────────────────────────────────┘\n"
    )

    iteration = 0
    try:
        while True:
            iteration += 1
            tf = await _app_frame(page)

            # ── Read current passenger data ───────────────────────────────────
            try:
                text = await _page_structured_text(page)
            except Exception:
                try:
                    text = await tf.inner_text("body")
                except Exception:
                    text = "(could not read passenger data)"

            # ── Display in terminal ───────────────────────────────────────────
            os.system("cls" if os.name == "nt" else "clear")
            now = datetime.now().strftime("%H:%M:%S")
            print(
                f"\n  ══ LIVE  AH{flight_num}/{dep_port}  [{now}]"
                f"  refresh #{iteration} ══\n"
            )
            for line in text.splitlines():
                line = line.strip()
                if line:
                    print(f"  {line}")
            print(f"\n  ─── next refresh in {interval}s ─── Ctrl+C to stop ───")

            # ── Wait the interval in small chunks (keeps asyncio responsive) ──
            for _ in range(interval * 2):
                await asyncio.sleep(0.5)

            # ── Click the circular refresh button to reload passenger data ────
            tf = await _app_frame(page)
            refresh_btn = await _find_refresh_btn(tf)
            if refresh_btn:
                try:
                    await refresh_btn.click()
                    await page.wait_for_load_state("networkidle", timeout=15_000)
                    await asyncio.sleep(0.8)
                    _info(f"Refresh clicked  [{datetime.now().strftime('%H:%M:%S')}]")
                except Exception as e:
                    _warn(f"Refresh button click failed: {e}")
                    # Fallback: re-open Passenger via apps icon
                    try:
                        await _click_apps_then_header(page, "HeaderPASSENGER", "Passenger")
                    except Exception:
                        pass
            else:
                # Refresh button not found – navigate back to Passenger
                _warn("Refresh button not found – re-opening Passenger view …")
                try:
                    await _click_apps_then_header(page, "HeaderPASSENGER", "Passenger")
                except Exception:
                    pass

    except KeyboardInterrupt:
        print("\n\n  [LIVE] Monitor stopped by user.")


# ──────────────────────────────────────────────────────────────────────────────
# Navigation back to search
# ──────────────────────────────────────────────────────────────────────────────
async def return_to_search(page: Page) -> None:
    """
    Go back to the flight search screen:
      #applicationsLink  →  #search  (the Search header tab)
    Same click pattern used everywhere else in Altéa.
    """
    _info("Returning to search screen …")
    await _click_apps_then_header(page, "search", "Search")


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────
async def run() -> None:
    _banner("Amadeus Altéa DCS FM Mobile  –  Air Algérie Ground Ops", 62)

    headless = (
        input("\n  Run browser silently (headless)? [y/N]: ").strip().lower()
        in ("y", "yes")
    )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=["--start-maximized"] if not headless else [],
        )
        ctx = await browser.new_context(
            no_viewport=True if not headless else None,
            viewport={"width": 1366, "height": 768} if headless else None,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await ctx.new_page()
        page.set_default_timeout(30_000)

        # Step 1 – Login
        await do_login(page)

        # Step 2 – Contact Details
        await handle_contact_details(page)

        # Flight loop
        first = True
        while True:
            if not first:
                await return_to_search(page)
            first = False

            # Step 3 – User input
            _banner("FLIGHT SEARCH", 40)
            flight_num = input("  Flight number  (e.g. 6007) : ").strip()
            dep_port   = input("  Departure IATA (e.g. AAE)  : ").strip().upper()
            today      = _today()
            raw_date   = input(f"  Date           [{today}] : ").strip()

            if not raw_date:
                # Enter pressed → use today
                date_str = today
            elif raw_date.isdigit() and 1 <= int(raw_date) <= 31:
                # Only a day number typed (e.g. "17") → keep month + year from today
                d = datetime.now()
                date_str = f"{int(raw_date):02d}-{_MONTHS[d.month]}-{d.year}"
                _info(f"Day-only input → {date_str}")
            else:
                # Full date string typed
                date_str = raw_date

            # Step 4 – Fill form and search
            await do_search(page, flight_num, dep_port, date_str)

            # Step 5 – Click flight row
            found = await select_flight_row(page, flight_num, dep_port)
            if not found:
                again = input("\n  Search another flight? [Y/n]: ").strip().lower()
                if again in ("n", "no"):
                    break
                continue

            # ── Check closed vs open: Documents → look for Final Loadsheet ──────
            # If the Final Loadsheet button exists  → flight is CLOSED.
            # If it does NOT exist                  → flight is still OPEN.
            # This is the only reliable indicator — no guessing from page text.
            _info("Checking Documents for Final Loadsheet …")
            is_closed = await get_final_loadsheet(page, flight_num, dep_port, date_str)

            if is_closed:
                # ══════════════════════════════════════════════════════════════
                # CLOSED – loadsheet already extracted above.
                # Now extract passenger data too.
                # ══════════════════════════════════════════════════════════════
                _ok("Flight is CLOSED ✓ — loadsheet saved, fetching passengers …")
                await open_passenger_view(page)
                await extract_passenger_data(page, flight_num, dep_port, date_str)

            else:
                # ══════════════════════════════════════════════════════════════
                # OPEN – no loadsheet yet.
                # Go to Passenger view and start live monitor.
                # ══════════════════════════════════════════════════════════════
                _banner("FLIGHT STATUS")
                print(
                    "\n  ✈  Final Loadsheet not found — flight is NOT CLOSED yet.\n"
                    "     Opening Passenger view with live refresh every 15 s.\n"
                )
                await open_passenger_view(page)
                await live_passenger_monitor(page, flight_num, dep_port, date_str, interval=15)

            # ── Search another flight? ────────────────────────────────────────
            again = input("\n  Search another flight? [Y/n]: ").strip().lower()
            if again in ("n", "no"):
                _ok("Exiting. Goodbye!")
                break

        await browser.close()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n\n  [✗] Interrupted by user.")
        sys.exit(0)
