"""Nightly sealed-product EV sync.

Downloads Scryfall bulk card prices and MTGJSON per-set booster blueprints,
then computes floor/EV/ceiling for every recent sealed product.

  floor_usd   = guaranteed minimum single-card value (worst-case pull)
  ev_usd      = expected value weighted by sheet probabilities
  ceiling_usd = best-case pull value

Run via:  mtg-watchdog sync-sealed
Cron:     0 2 * * * /path/to/venv/bin/mtg-watchdog sync-sealed
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

from . import db

log = logging.getLogger(__name__)

_PKG = Path(__file__).resolve().parent
DATA_DIR = _PKG.parent.parent.parent / "data"
CACHE_DIR = DATA_DIR / "cache"

UA = {
    "User-Agent": "mtg-watchdog/0.1 (+github.com/youaregiants/mtg-watchdog)",
    "Accept": "application/json",
}
SCRYFALL_BULK_INDEX = "https://api.scryfall.com/bulk-data"
MTGJSON_SEALED_LIST = "https://mtgjson.com/api/v5/SealedList.json"
MTGJSON_SET_URL = "https://mtgjson.com/api/v5/{code}.json"

CUTOFF_DATE = "2020-01-01"  # ignore sealed products from older sets

FOCUS_CATEGORIES = {
    "booster_box",
    "booster_pack",
    "bundle",
    "commander_deck",
    "prerelease_pack",
    "draft_set",
}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _client() -> httpx.Client:
    return httpx.Client(
        headers=UA,
        timeout=httpx.Timeout(30.0, connect=15.0, read=600.0),
        follow_redirects=True,
    )


def _stream_download(url: str, target: Path) -> Path:
    tmp = target.with_suffix(target.suffix + ".tmp")
    with _client() as c, c.stream("GET", url) as r:
        r.raise_for_status()
        with tmp.open("wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
    tmp.replace(target)
    return target


def _get_json(url: str) -> dict | list:
    with _client() as c:
        r = c.get(url)
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------
# Download / cache
# ---------------------------------------------------------------------------

def download_scryfall_bulk(force: bool = False) -> Path:
    target = CACHE_DIR / "scryfall_default_cards.json"
    if target.exists() and not force:
        log.info("scryfall bulk: using cache (%s)", target)
        return target
    log.info("scryfall bulk: fetching index...")
    index = _get_json(SCRYFALL_BULK_INDEX)
    entry = next(e for e in index["data"] if e["type"] == "default_cards")
    url = entry["download_uri"]
    log.info("scryfall bulk: downloading %s ...", url)
    _stream_download(url, target)
    log.info("scryfall bulk: done (%d MB)", target.stat().st_size // 1_000_000)
    return target


def download_sealed_list(force: bool = False) -> Path:
    target = CACHE_DIR / "SealedList.json"
    if target.exists() and not force:
        return target
    log.info("SealedList: downloading...")
    _stream_download(MTGJSON_SEALED_LIST, target)
    return target


def download_set(set_code: str) -> Path:
    """Per-set files are cached permanently — sets don't change after release."""
    target = CACHE_DIR / f"set_{set_code.upper()}.json"
    if target.exists():
        return target
    url = MTGJSON_SET_URL.format(code=set_code.upper())
    log.debug("mtgjson set %s: downloading...", set_code)
    try:
        _stream_download(url, target)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            target.write_text("{}")  # cache empty stub so we don't retry
        else:
            raise
    return target


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _iter_json_array(path: Path):
    """Stream top-level JSON array, using ijson when available."""
    try:
        import ijson  # type: ignore
        with path.open("rb") as f:
            yield from ijson.items(f, "item")
    except ImportError:
        with path.open("r", encoding="utf-8") as f:
            yield from json.load(f)


def load_scryfall_prices(path: Path) -> dict[str, tuple[Optional[float], Optional[float]]]:
    """Return {scryfall_id: (usd, usd_foil)} for every card in the bulk file."""
    def _f(v):
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    prices: dict[str, tuple] = {}
    for rec in _iter_json_array(path):
        sid = rec.get("id")
        if not sid:
            continue
        p = rec.get("prices") or {}
        usd = _f(p.get("usd"))
        usd_foil = _f(p.get("usd_foil")) or _f(p.get("usd_etched"))
        prices[sid] = (usd, usd_foil)
    log.info("scryfall prices loaded: %d cards", len(prices))
    return prices


def load_sealed_list(path: Path) -> list[dict]:
    """Return [{uuid, name, set_code, category, release_date}] from SealedList.json."""
    with path.open("rb") as f:
        doc = json.load(f)
    data = doc.get("data", doc)
    products = []
    for set_code, items in (data.items() if isinstance(data, dict) else []):
        for p in items or []:
            products.append({
                "uuid": p.get("uuid"),
                "name": p.get("name", ""),
                "set_code": set_code.upper(),
                "category": p.get("category"),
                "subtype": p.get("subtype"),
                "release_date": p.get("releaseDate"),
            })
    return products


def load_set(set_code: str) -> dict:
    path = download_set(set_code)
    with path.open("rb") as f:
        doc = json.load(f)
    return doc.get("data", doc)


# ---------------------------------------------------------------------------
# Price bridge: Scryfall ID → MTGJSON UUID
# ---------------------------------------------------------------------------

def build_uuid_prices(
    set_data: dict,
    scryfall_prices: dict[str, tuple],
) -> dict[str, tuple[float, float]]:
    """Return {mtgjson_uuid: (usd, usd_foil)} for every card in this set."""
    result: dict[str, tuple] = {}
    for card in set_data.get("cards", []) or []:
        uuid = card.get("uuid")
        if not uuid:
            continue
        ids = card.get("identifiers") or {}
        scry_id = ids.get("scryfallId")
        if scry_id and scry_id in scryfall_prices:
            usd, usd_foil = scryfall_prices[scry_id]
            result[uuid] = (usd or 0.0, usd_foil or usd or 0.0)
        else:
            result[uuid] = (0.0, 0.0)
    return result


# ---------------------------------------------------------------------------
# Valuation math (ported from mtg-sealed-value)
# ---------------------------------------------------------------------------

@dataclass
class _Val:
    ev: float = 0.0
    floor: float = 0.0
    ceiling: float = 0.0
    count: int = 0
    kind: str = "deterministic"

    def __add__(self, other: "_Val") -> "_Val":
        return _Val(
            ev=self.ev + other.ev,
            floor=self.floor + other.floor,
            ceiling=self.ceiling + other.ceiling,
            count=self.count + other.count,
            kind="probabilistic" if "probabilistic" in (self.kind, other.kind) else "deterministic",
        )

    def scale(self, n: int) -> "_Val":
        return _Val(ev=self.ev * n, floor=self.floor * n,
                    ceiling=self.ceiling * n, count=self.count * n, kind=self.kind)


def _sheet_stats(sheet: dict, uuid_prices: dict) -> tuple[float, float, float]:
    """Return (expected, floor, ceiling) for one booster sheet."""
    cards = sheet.get("cards") or {}
    total = sheet.get("totalWeight") or sum(cards.values()) or 1
    foil = bool(sheet.get("foil") or sheet.get("isFoil"))
    expected = 0.0
    nonzero: list[float] = []
    for uuid, weight in cards.items():
        usd, usd_foil = uuid_prices.get(uuid, (0.0, 0.0))
        price = usd_foil if foil else usd
        expected += (weight / total) * price
        if price > 0:
            nonzero.append(price)
    floor = min(nonzero) if nonzero else 0.0
    ceiling = max(nonzero) if nonzero else 0.0
    return expected, floor, ceiling


def _value_booster_blueprint(blueprint: dict, uuid_prices: dict) -> _Val:
    """Evaluate a MTGJSON booster blueprint (one type under set.booster)."""
    configs = blueprint.get("boosters") or []
    sheets = blueprint.get("sheets") or {}
    total_w = sum(b.get("weight", 1) for b in configs) or 1

    ev = floor = ceiling = 0.0
    count = 0
    first = True

    for cfg in configs:
        w = cfg.get("weight", 1) / total_w
        cfg_ev = cfg_floor = cfg_ceiling = 0.0
        cfg_count = 0
        for sheet_name, slot_count in (cfg.get("contents") or {}).items():
            sheet = sheets.get(sheet_name) or {}
            s_ev, s_floor, s_ceiling = _sheet_stats(sheet, uuid_prices)
            cfg_ev += s_ev * slot_count
            cfg_floor += s_floor * slot_count
            cfg_ceiling += s_ceiling * slot_count
            cfg_count += slot_count
        ev += w * cfg_ev
        if first:
            floor = cfg_floor
            first = False
        else:
            floor = min(floor, cfg_floor)
        ceiling = max(ceiling, cfg_ceiling)
        count = max(count, cfg_count)

    return _Val(ev=ev, floor=floor, ceiling=ceiling, count=count, kind="probabilistic")


def _value_contents(
    contents: dict,
    set_code: str,
    set_cache: dict[str, dict],
    uuid_prices_cache: dict[str, dict],
) -> _Val:
    """Recursively value a MTGJSON sealedProduct.contents tree."""
    total = _Val()
    if not contents:
        return total

    # 1. Fixed card inclusions (Secret Lairs, prerelease promo, etc.)
    for c in contents.get("card", []) or []:
        uuid = c.get("uuid")
        qty = int(c.get("count", 1) or 1)
        foil = bool(c.get("foil"))
        usd, usd_foil = uuid_prices_cache.get(set_code, {}).get(uuid, (0.0, 0.0))
        price = usd_foil if foil else usd
        total += _Val(ev=price * qty, floor=price * qty, ceiling=price * qty, count=qty)

    # 2. Booster packs
    for pack in contents.get("pack", []) or []:
        pack_set = (pack.get("set") or set_code).upper()
        pack_data = set_cache.get(pack_set, {})
        booster_name = pack.get("code", "default")
        blueprints = pack_data.get("booster") or {}
        blueprint = blueprints.get(booster_name) or blueprints.get("default") or blueprints.get("play") or next(iter(blueprints.values()), None)
        if not blueprint:
            continue
        pack_val = _value_booster_blueprint(blueprint, uuid_prices_cache.get(pack_set, {}))
        count = int(pack.get("count", 1) or 1)
        total += pack_val.scale(count)

    return total


# ---------------------------------------------------------------------------
# Main sync entry point
# ---------------------------------------------------------------------------

def run_sync(force_download: bool = False) -> dict:
    """Full nightly sealed-product sync. Returns stats dict."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    db.init_db()  # ensures sealed_products table exists

    stats: dict = {}

    # 1. Scryfall bulk prices
    log.info("=== Sealed sync: Scryfall bulk ===")
    bulk_path = download_scryfall_bulk(force=force_download)
    log.info("Building price lookup...")
    scryfall_prices = load_scryfall_prices(bulk_path)
    stats["scryfall_cards"] = len(scryfall_prices)

    # 2. SealedList
    log.info("=== Sealed sync: SealedList ===")
    sealed_list_path = download_sealed_list(force=force_download)
    all_products = load_sealed_list(sealed_list_path)
    log.info("SealedList: %d total products", len(all_products))

    # 3. Filter to recent sets with meaningful categories
    products = [
        p for p in all_products
        if p.get("category") in FOCUS_CATEGORIES
        and p.get("uuid")
        and (not p.get("release_date") or p["release_date"] >= CUTOFF_DATE)
    ]
    log.info("Filtered to %d products (cat=%s, since %s)", len(products), FOCUS_CATEGORIES, CUTOFF_DATE)
    stats["products_targeted"] = len(products)

    set_codes = sorted({p["set_code"] for p in products})
    stats["sets_targeted"] = len(set_codes)

    # 4. Download and cache per-set MTGJSON files
    log.info("=== Sealed sync: per-set data (%d sets) ===", len(set_codes))
    set_cache: dict[str, dict] = {}
    uuid_prices_cache: dict[str, dict] = {}

    for i, code in enumerate(set_codes, 1):
        try:
            set_data = load_set(code)
            set_cache[code] = set_data
            uuid_prices_cache[code] = build_uuid_prices(set_data, scryfall_prices)
            if i % 25 == 0:
                log.info("  sets loaded: %d/%d", i, len(set_codes))
        except Exception:
            log.exception("Failed to load set %s", code)
            set_cache[code] = {}
            uuid_prices_cache[code] = {}

    log.info("=== Sealed sync: computing valuations ===")

    # 5. For each set, get product contents from per-set data and compute valuations
    product_contents: dict[str, dict] = {}  # uuid -> contents dict
    product_set_names: dict[str, str] = {}  # uuid -> set_name
    for code, set_data in set_cache.items():
        set_name = set_data.get("name", "")
        for sp in set_data.get("sealedProduct", []) or []:
            uuid = sp.get("uuid")
            if uuid:
                product_contents[uuid] = sp.get("contents") or {}
                product_set_names[uuid] = set_name

    written = errors = skipped = 0
    for p in products:
        uuid = p["uuid"]
        code = p["set_code"]
        contents = product_contents.get(uuid)
        if not contents:
            skipped += 1
            continue
        try:
            val = _value_contents(contents, code, set_cache, uuid_prices_cache)
        except Exception:
            log.exception("Valuation failed for %s (%s)", p["name"], uuid)
            errors += 1
            continue

        db.upsert_sealed_product({
            "uuid": uuid,
            "name": p["name"],
            "set_code": code,
            "set_name": product_set_names.get(uuid) or "",
            "category": p.get("category"),
            "release_date": p.get("release_date"),
            "floor_usd": round(val.floor, 2) if val.floor else None,
            "ev_usd": round(val.ev, 2) if val.ev else None,
            "ceiling_usd": round(val.ceiling, 2) if val.ceiling else None,
            "card_count": val.count or None,
            "valuation_kind": val.kind,
            "updated_at": db.now_iso(),
        })
        written += 1

    stats.update({"written": written, "skipped": skipped, "errors": errors})
    log.info("=== Sealed sync complete: %s ===", stats)
    return stats
