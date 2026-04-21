#!/usr/bin/env python3
"""
Amadeus Altéa DCS FM Mobile – REST API Server

Wraps amadeus_ah.py in a FastAPI server so any device on the same
network can trigger searches via HTTP.

Start:
    python amadeus_api.py

Endpoints:
    GET  /              – health check
    POST /search        – start a flight search, returns job_id immediately
    GET  /result/{id}   – poll for search result
    GET  /reports       – list saved report files
    GET  /reports/{fn}  – download a saved report file
    POST /login         – open browser session and login
    POST /logout        – close browser session
"""

import asyncio
import os
import socket
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

_IS_RAILWAY = os.environ.get("RAILWAY_ENVIRONMENT") is not None
if _IS_RAILWAY:
    import amadeus_ah as _ah_module
    _ah_module.OUTPUT_DIR = Path("/tmp/reports")

from amadeus_ah import (
    HOME_URL,
    OUTPUT_DIR,
    _app_frame,
    _click_apps_then_header,
    _ensure_output_dir,
    _page_structured_text,
    _report_path,
    _save_loadsheet_pdf,
    _save_pdf,
    _save_text,
    _today,
    _MONTHS,
    _wait_splash_gone,
    dismiss_any_modal,
    do_login,
    do_search,
    extract_passenger_data,
    get_final_loadsheet,
    handle_contact_details,
    live_passenger_monitor,
    open_passenger_view,
    select_flight_row,
)
from playwright.async_api import async_playwright

_state: dict = {
    "playwright":   None,
    "browser":      None,
    "page":         None,
    "logged_in":    False,
    "lock":         None,
    "username":     None,
    "organization": None,
    "password":     None,
}

# job_id -> {"status": "pending"|"done"|"error", "result": ..., "detail": ...}
_jobs: dict = {}

SEARCH_INPUT = "#tpl0_SEARCH_searchForm_flightNum_input"


async def _session_expired(page) -> bool:
    try:
        url = page.url
        if "LoginService" in url or "login" in url.lower():
            return True
        el = await page.query_selector("#userAliasInput, #passwordInput")
        return el is not None
    except Exception:
        return False


async def _go_to_search(page) -> bool:
    """Navigate to the flight-search form. Auto-relogs in if the session expired."""
    async def _form_visible(ms=5_000):
        try:
            tf = await _app_frame(page)
            await tf.wait_for_selector(SEARCH_INPUT, state="visible", timeout=ms)
            return True
        except Exception:
            return False

    await _wait_splash_gone(page)
    await dismiss_any_modal(page)

    try:
        await _click_apps_then_header(page, "search", "Search")
        if await _form_visible(5_000):
            print("  [✓] _go_to_search: form ready via apps+tab.")
            return True
    except Exception as e:
        print(f"  [!] _go_to_search method A: {e}")

    try:
        print("  [→] _go_to_search: HOME_URL fallback …")
        await page.goto(HOME_URL, wait_until="load", timeout=30_000)
        await _wait_splash_gone(page)
        await dismiss_any_modal(page)
        await _click_apps_then_header(page, "search", "Search")
        if await _form_visible(6_000):
            print("  [✓] _go_to_search: form ready after HOME_URL + tab.")
            return True
    except Exception as e:
        print(f"  [!] _go_to_search method B: {e}")

    print("  [→] _go_to_search: session may have expired – re-logging in …")
    try:
        _state["logged_in"] = False
        await _ensure_session()
        _state["logged_in"] = True
        if await _form_visible(8_000):
            print("  [✓] _go_to_search: form ready after re-login.")
            return True
    except Exception as e:
        print(f"  [!] _go_to_search re-login failed: {e}")

    print("  [!] _go_to_search: all methods failed.")
    return False


async def _ensure_session(username: str = None, organization: str = None, password: str = None) -> None:
    """Launch browser (if needed) and login with the provided or stored credentials."""
    u = username     or _state["username"]
    o = organization or _state["organization"]
    p = password     or _state["password"]

    if _state["page"]:
        page = _state["page"]
        await do_login(page, u, o, p)
        await handle_contact_details(page)
        await dismiss_any_modal(page)
        _state["logged_in"] = True
        return

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    ctx = await browser.new_context(
        viewport={"width": 1366, "height": 768},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    )
    page = await ctx.new_page()
    page.set_default_timeout(30_000)

    await do_login(page, u, o, p)
    await handle_contact_details(page)
    await dismiss_any_modal(page)

    _state["playwright"] = pw
    _state["browser"]    = browser
    _state["page"]       = page
    _state["logged_in"]  = True


async def _close_session() -> None:
    if _state["browser"]:
        await _state["browser"].close()
    if _state["playwright"]:
        await _state["playwright"].stop()
    _state["browser"]    = None
    _state["playwright"] = None
    _state["page"]       = None
    _state["logged_in"]  = False


async def _warmup() -> None:
    """Launch the browser in the background so it's ready when /login is called."""
    await asyncio.sleep(3)
    async with _state["lock"]:
        try:
            if _state["browser"]:
                return
            pw = await async_playwright().start()
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            ctx = await browser.new_context(
                viewport={"width": 1366, "height": 768},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )
            page = await ctx.new_page()
            page.set_default_timeout(30_000)
            _state["playwright"] = pw
            _state["browser"]    = browser
            _state["page"]       = page
            print("  [✓] Browser ready – waiting for POST /login.")
        except Exception:
            import traceback
            print(f"WARMUP ERROR:\n{traceback.format_exc()}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _state["lock"] = asyncio.Lock()
    asyncio.create_task(_warmup())
    yield
    await _close_session()


app = FastAPI(
    title="Amadeus Altéa API",
    description="Air Algérie Ground Operations – flight search & loadsheet extraction",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)


class LoginRequest(BaseModel):
    username:     str
    organization: Optional[str] = "AH"
    password:     str


class FlightRequest(BaseModel):
    flight_num: str
    dep_port:   str
    date:       Optional[str] = ""


class FlightResponse(BaseModel):
    flight:        str
    dep_port:      str
    date:          str
    closed:        bool
    passenger_txt: Optional[str] = None
    loadsheet_txt: Optional[str] = None
    files:         list[str] = []


def _resolve_date(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return _today()
    if raw.isdigit() and 1 <= int(raw) <= 31:
        from datetime import datetime
        d = datetime.now()
        return f"{int(raw):02d}-{_MONTHS[d.month]}-{d.year}"
    return raw


async def _run_search(job_id: str, flight_num: str, dep_port: str, date_str: str) -> None:
    """Run the full search in the background and store result in _jobs."""
    async with _state["lock"]:
        page = _state["page"]
        if not page:
            _jobs[job_id] = {"status": "error", "detail": "Browser session not ready"}
            return

        try:
            if not await _go_to_search(page):
                _jobs[job_id] = {"status": "error", "detail": "Search form unreachable. Try POST /logout then POST /login."}
                return

            await dismiss_any_modal(page)
            await do_search(page, flight_num, dep_port, date_str)

            found = await select_flight_row(page, flight_num, dep_port)
            if not found:
                _jobs[job_id] = {"status": "error", "detail": f"No flight AH{flight_num}/{dep_port}/{date_str} found", "code": 404}
                return

            await open_passenger_view(page)
            pax_text = await extract_passenger_data(page, flight_num, dep_port, date_str)

            is_closed = await get_final_loadsheet(page, flight_num, dep_port, date_str)

            loadsheet_txt = None
            if is_closed:
                ls_path = _report_path(flight_num, dep_port, date_str, "loadsheet", "txt")
                if ls_path.exists():
                    loadsheet_txt = ls_path.read_text(encoding="utf-8")

            _ensure_output_dir()
            files = sorted(
                f.name for f in OUTPUT_DIR.iterdir()
                if f.stem.startswith(f"AH{flight_num}_{dep_port}_{date_str.replace('-','')}")
            )

            _jobs[job_id] = {
                "status": "done",
                "result": FlightResponse(
                    flight        = f"AH{flight_num}",
                    dep_port      = dep_port,
                    date          = date_str,
                    closed        = is_closed,
                    passenger_txt = pax_text,
                    loadsheet_txt = loadsheet_txt,
                    files         = files,
                ).model_dump(),
            }

            try:
                await _go_to_search(page)
            except Exception:
                pass

        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            print(f"SEARCH ERROR:\n{tb}")
            _jobs[job_id] = {"status": "error", "detail": f"{type(exc).__name__}: {exc}"}


@app.get("/", summary="Server status")
async def root():
    local_ip = socket.gethostbyname(socket.gethostname())
    return {
        "status":    "running",
        "logged_in": _state["logged_in"],
        "server":    f"http://{local_ip}:8000",
        "today":     _today(),
    }


@app.post("/search", summary="Start a flight search (returns immediately)")
async def search_flight(req: FlightRequest):
    """
    Starts the search in the background and returns a job_id straight away.
    Poll GET /result/{job_id} every few seconds until status is 'done' or 'error'.
    """
    if not (_state["logged_in"] and _state["page"]):
        raise HTTPException(503, "Not logged in – call POST /login first.")

    flight_num = req.flight_num.strip()
    dep_port   = req.dep_port.strip().upper()
    date_str   = _resolve_date(req.date)

    job_id = str(uuid.uuid4())
    _jobs[job_id] = {"status": "pending"}

    asyncio.create_task(_run_search(job_id, flight_num, dep_port, date_str))

    return {"job_id": job_id, "status": "pending"}


@app.get("/result/{job_id}", summary="Poll for search result")
async def get_result(job_id: str):
    """
    Returns the search result once ready.
    - status 'pending' → still running, check again in a few seconds
    - status 'done'    → result is in the 'result' field
    - status 'error'   → something went wrong, check 'detail'
    """
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, f"Job '{job_id}' not found")
    return job


@app.get("/reports", summary="List saved report files")
async def list_reports():
    _ensure_output_dir()
    files = sorted(f.name for f in OUTPUT_DIR.iterdir() if f.is_file())
    return {"reports": files, "count": len(files)}


@app.get("/reports/{filename}", summary="Download a report file")
async def get_report(filename: str):
    path = OUTPUT_DIR / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(404, f"File '{filename}' not found")
    if not str(path.resolve()).startswith(str(OUTPUT_DIR.resolve())):
        raise HTTPException(403, "Access denied")
    return FileResponse(str(path), filename=filename)


@app.post("/logout", summary="Close browser session")
async def logout():
    async with _state["lock"]:
        await _close_session()
    return {"status": "logged out"}


@app.post("/login", summary="Login with your Amadeus credentials")
async def login(req: LoginRequest):
    async with _state["lock"]:
        _state["username"]     = req.username.strip()
        _state["organization"] = req.organization.strip().upper()
        _state["password"]     = req.password
        await _ensure_session(req.username.strip(), req.organization.strip().upper(), req.password)
    return {"status": "logged in", "user": req.username.upper()}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    try:
        local_ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        local_ip = "localhost"

    print(f"""
  ╔══════════════════════════════════════════════════════╗
  ║   Amadeus Altéa API Server                          ║
  ║                                                      ║
  ║   Local :  http://127.0.0.1:{port}                  ║
  ║   Network: http://{local_ip}:{port}                 ║
  ║                                                      ║
  ║   Docs  :  http://127.0.0.1:{port}/docs             ║
  ║   Ctrl+C to stop                                     ║
  ╚══════════════════════════════════════════════════════╝
""")
    uvicorn.run("amadeus_api:app", host="0.0.0.0", port=port, reload=False)
