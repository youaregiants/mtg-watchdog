"""FastAPI app for mtg-watchdog."""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, refresh, scryfall, seed

log = logging.getLogger("mtg_watchdog")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

PKG_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(PKG_DIR / "templates"))
TEMPLATES.env.cache = None  # workaround: jinja LRU cache + py3.14 raises in threadpool


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    inserted = seed.seed()
    if inserted:
        log.info("Seeded %d demo cards", inserted)
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(refresh.refresh_all, "interval", hours=12,
                      id="refresh-all", max_instances=1, coalesce=True)
    scheduler.start()
    app.state.scheduler = scheduler
    log.info("Scheduler started (refresh every 12h)")
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(title="mtg-watchdog", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(PKG_DIR / "static")), name="static")


def _ctx(**extra) -> dict:
    return {"unseen_alerts": db.unseen_alert_count(), **extra}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    watches = [dict(w) for w in db.list_watches()]
    for w in watches:
        w["atl_pct_usd"] = _delta_pct(w.get("usd"), w.get("atl_usd"))
        w["at_atl_usd"] = w.get("usd") is not None and w.get("atl_usd") is not None \
            and abs(w["usd"] - w["atl_usd"]) < 0.005
    return TEMPLATES.TemplateResponse(request, "index.html", _ctx(watches=watches))


@app.get("/card/{scryfall_id}", response_class=HTMLResponse)
def card(request: Request, scryfall_id: str):
    w = db.get_watch(scryfall_id)
    if not w:
        raise HTTPException(404, "Not watching that card")
    history = [dict(h) for h in db.history_for(scryfall_id)]
    history_chrono = list(reversed(history))
    chart_data = {
        "labels": [h["observed_at"][:10] for h in history_chrono],
        "usd":        [h["usd"] for h in history_chrono],
        "usd_foil":   [h["usd_foil"] for h in history_chrono],
        "usd_etched": [h["usd_etched"] for h in history_chrono],
    }
    return TEMPLATES.TemplateResponse(
        request,
        "card.html",
        _ctx(w=dict(w), history=history, chart_data_json=json.dumps(chart_data),
             atl_usd=db.atl(scryfall_id, "usd"),
             atl_foil=db.atl(scryfall_id, "usd_foil"),
             atl_etched=db.atl(scryfall_id, "usd_etched")),
    )


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: Optional[str] = None):
    results = []
    error = None
    if q:
        try:
            results = scryfall.search(q, limit=20)
        except Exception as e:
            log.exception("scryfall search failed")
            error = f"Scryfall error: {e}"
    return TEMPLATES.TemplateResponse(
        request, "search.html", _ctx(q=q or "", results=results, error=error),
    )


@app.post("/watch")
def watch(scryfall_id: str = Form(...), target_price: Optional[str] = Form(None)):
    try:
        card = scryfall.get_card(scryfall_id)
    except Exception as e:
        raise HTTPException(502, f"Scryfall fetch failed: {e}")
    tp = None
    if target_price:
        try:
            tp = float(target_price)
        except ValueError:
            tp = None
    db.add_watch(card, target_price=tp)
    # also record the current price snapshot so charts start populated
    db.record_price(card["id"], card.get("prices") or {})
    return RedirectResponse(url=f"/card/{card['id']}", status_code=303)


@app.post("/unwatch/{scryfall_id}")
def unwatch(scryfall_id: str):
    db.remove_watch(scryfall_id)
    return RedirectResponse(url="/", status_code=303)


@app.post("/set-target/{scryfall_id}")
def set_target(scryfall_id: str, target_price: Optional[str] = Form(None)):
    if not db.get_watch(scryfall_id):
        raise HTTPException(404, "Not watching that card")
    tp = None
    if target_price and target_price.strip():
        try:
            tp = float(target_price)
        except ValueError:
            pass
    db.update_target(scryfall_id, tp)
    return RedirectResponse(url=f"/card/{scryfall_id}", status_code=303)


@app.post("/refresh")
def manual_refresh():
    tally = refresh.refresh_all()
    log.info("manual refresh: %s", tally)
    return RedirectResponse(url="/alerts", status_code=303)


@app.get("/alerts", response_class=HTMLResponse)
def alerts(request: Request):
    rows = [dict(a) for a in db.list_alerts()]
    db.mark_alerts_seen()
    return TEMPLATES.TemplateResponse(request, "alerts.html", _ctx(alerts=rows))


# ---- helpers --------------------------------------------------------------

def _delta_pct(now, baseline):
    if now is None or baseline is None or baseline == 0:
        return None
    return (now - baseline) / baseline * 100
