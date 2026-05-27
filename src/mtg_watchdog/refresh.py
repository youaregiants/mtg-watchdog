"""Refresh worker: re-fetches prices for every watched card and raises alerts."""
from __future__ import annotations

import logging
import time

from . import db, scryfall

log = logging.getLogger(__name__)

FINISHES = ("usd", "usd_foil", "usd_etched")
DROP_THRESHOLD = 0.10  # 10% drop day-over-day


def refresh_all() -> dict:
    """Refresh prices for every watch. Returns a tally."""
    watches = db.list_watches()
    tally = {"refreshed": 0, "alerts": 0, "errors": 0}
    for w in watches:
        try:
            _refresh_one(w)
            tally["refreshed"] += 1
            time.sleep(0.1)  # be polite to Scryfall (10rps soft cap)
        except Exception:
            log.exception("refresh failed for %s", w["scryfall_id"])
            tally["errors"] += 1
    return tally


def _refresh_one(watch) -> None:
    sid = watch["scryfall_id"]
    prev = db.latest_price(sid)
    prices = scryfall.get_card_prices(sid)
    db.record_price(sid, prices)

    for finish in FINISHES:
        new_p = _as_float(prices.get(finish))
        if new_p is None:
            continue
        prev_p = _as_float(prev[finish]) if prev else None
        atl_before = db.atl(sid, finish)
        # all-time low
        if atl_before is None or new_p < atl_before:
            if prev_p is not None:  # don't alert on the very first observation
                db.add_alert(sid, "atl", finish, new_p, prev_p,
                             f"New all-time low: ${new_p:.2f} ({finish})")
        # target hit
        if watch["target_price"] is not None and finish == "usd":
            if new_p <= watch["target_price"] and (prev_p is None or prev_p > watch["target_price"]):
                db.add_alert(sid, "target", finish, new_p, prev_p,
                             f"Hit target ${watch['target_price']:.2f} (now ${new_p:.2f})")
        # big drop
        if prev_p and prev_p > 0:
            drop = (prev_p - new_p) / prev_p
            if drop >= DROP_THRESHOLD:
                db.add_alert(sid, "drop", finish, new_p, prev_p,
                             f"{drop*100:.0f}% drop: ${prev_p:.2f} → ${new_p:.2f} ({finish})")


def _as_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
