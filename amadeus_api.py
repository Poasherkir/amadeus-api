#!/usr/bin/env python3
"""
Amadeus Altéa DCS FM Mobile – REST API Server
==============================================
Wraps amadeus_ah.py in a FastAPI server so any device on the same
network (phone, tablet, another PC) can trigger searches via HTTP.

Start:
    python amadeus_api.py

Then call from anywhere:
    POST http://<your-ip>:8000/search
    Body: { "flight_num": "6007", "dep_port": "AAE", "date": "18-APR-2026" }

Endpoints
---------
GET  /             – health check / status
POST /search       – search a flight and return all data
GET  /reports      – list saved report files
GET  /reports/{fn} – download a saved report file
POST /logout       – close browser session
"""

import asyncio
import os
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# On Railway the filesystem is ephemeral — use /tmp so PDFs survive the request
# On Windows (local) keep the original reports folder
_IS_RAILWAY = os.environ.get("RAILWAY_ENVIRONMENT") is not None
if _IS_RAILWAY:
    import amadeus_ah as _ah_module
    _ah_module.OUTPUT_DIR = Path("/tmp/reports")

# ── Import everything from the main automation script ──────────────────────
from amadeus_ah import (
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

# ──────────────────────────────────────────────────────────────────────────────
# Shared browser state (one session for the lifetime of the server)
# ──────────────────────────────────────────────────────────────────────────────
_state: dict = {
    "playwright": None,
    "browser":    None,
    "page":       None,
    "logged_in":  False,
    "lock":       None,   # asyncio.Lock – set in lifespan
}


async def _ensure_session() -> None:
    """Launch browser and login if not already done."""
    if _state["logged_in"] and _state["page"]:
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

    await do_login(page)
    await handle_contact_details(page)

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


# ──────────────────────────────────────────────────────────────────────────────
# App lifespan – start browser on boot, close on shutdown
# ──────────────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    _state["lock"] = asyncio.Lock()
    await _ensure_session()
    yield
    await _close_session()


app = FastAPI(
    title="Amadeus Altéa API",
    description="Air Algérie Ground Operations – flight search & loadsheet extraction",
    version="1.0.0",
    lifespan=lifespan,
)


# ──────────────────────────────────────────────────────────────────────────────
# Request / Response models
# ──────────────────────────────────────────────────────────────────────────────
class FlightRequest(BaseModel):
    flight_num: str
    dep_port:   str
    date:       Optional[str] = ""   # leave empty for today


class FlightResponse(BaseModel):
    flight:       str
    dep_port:     str
    date:         str
    closed:       bool
    passenger_txt: Optional[str] = None
    loadsheet_txt: Optional[str] = None
    files:        list[str] = []


# ──────────────────────────────────────────────────────────────────────────────
# Helper – resolve date string
# ──────────────────────────────────────────────────────────────────────────────
def _resolve_date(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return _today()
    if raw.isdigit() and 1 <= int(raw) <= 31:
        from datetime import datetime
        d = datetime.now()
        return f"{int(raw):02d}-{_MONTHS[d.month]}-{d.year}"
    return raw


# ──────────────────────────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/", summary="Server status")
async def root():
    local_ip = socket.gethostbyname(socket.gethostname())
    return {
        "status":    "running",
        "logged_in": _state["logged_in"],
        "server":    f"http://{local_ip}:8000",
        "today":     _today(),
    }


@app.post("/search", response_model=FlightResponse, summary="Search a flight")
async def search_flight(req: FlightRequest):
    """
    Search for a flight and extract all available data.

    - If the Final Loadsheet is found → flight is CLOSED → returns loadsheet + passengers.
    - If not found → flight is OPEN → returns current passenger data only.

    Saved files are also written to the reports folder on the server.
    """
    async with _state["lock"]:   # one search at a time
        page = _state["page"]
        if not page:
            raise HTTPException(503, "Browser session not ready")

        flight_num = req.flight_num.strip()
        dep_port   = req.dep_port.strip().upper()
        date_str   = _resolve_date(req.date)

        # ── navigate back to search ────────────────────────────────────────
        try:
            await _click_apps_then_header(page, "search", "Search")
        except Exception:
            pass

        # ── fill form & search ─────────────────────────────────────────────
        await do_search(page, flight_num, dep_port, date_str)

        # ── click result row ───────────────────────────────────────────────
        found = await select_flight_row(page, flight_num, dep_port)
        if not found:
            raise HTTPException(404, f"No flight AH{flight_num}/{dep_port}/{date_str} found")

        # ── check if closed (Final Loadsheet present?) ─────────────────────
        is_closed = await get_final_loadsheet(page, flight_num, dep_port, date_str)

        loadsheet_txt = None
        if is_closed:
            ls_path = _report_path(flight_num, dep_port, date_str, "loadsheet", "txt")
            if ls_path.exists():
                loadsheet_txt = ls_path.read_text(encoding="utf-8")

        # ── passenger data ─────────────────────────────────────────────────
        await open_passenger_view(page)
        pax_text = await extract_passenger_data(page, flight_num, dep_port, date_str)

        # ── collect saved file names ───────────────────────────────────────
        _ensure_output_dir()
        files = [
            f.name for f in OUTPUT_DIR.iterdir()
            if f.stem.startswith(f"AH{flight_num}_{dep_port}_{date_str.replace('-','')}")
        ]

        return FlightResponse(
            flight        = f"AH{flight_num}",
            dep_port      = dep_port,
            date          = date_str,
            closed        = is_closed,
            passenger_txt = pax_text,
            loadsheet_txt = loadsheet_txt,
            files         = sorted(files),
        )


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
    # Guard against path traversal
    if not str(path.resolve()).startswith(str(OUTPUT_DIR.resolve())):
        raise HTTPException(403, "Access denied")
    return FileResponse(str(path), filename=filename)


@app.post("/logout", summary="Close browser session")
async def logout():
    async with _state["lock"]:
        await _close_session()
    return {"status": "logged out"}


@app.post("/login", summary="Re-open browser session and login")
async def login():
    async with _state["lock"]:
        if _state["logged_in"]:
            return {"status": "already logged in"}
        await _ensure_session()
    return {"status": "logged in"}


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Railway injects $PORT; fall back to 8000 locally
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
