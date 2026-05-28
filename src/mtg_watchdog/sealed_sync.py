"""Nightly sealed-product EV sync.

Data flow:
  1. Scryfall default-cards bulk → {scryfall_id: (usd, usd_foil)}
  2. MTGJSON SetList.json → which sets have sealed products + release dates
  3. Per-set MTGJSON (cached) → booster blueprints + card UUID↔scryfallId bridge
  4. Build global {mtgjson_uuid: (usd, usd_foil)} dict
  5. Recursively value each sealed product:
       booster_box → contents.sealed (N packs) → contents.pack → blueprint
       bundle      → contents.sealed + contents.card
       booster_pack → contents.pack → blueprint
  6. Write floor/EV/ceiling to sealed_products table

Definitions:
  floor_usd   = guaranteed minimum (worst-case pull from each booster slot)
  ev_usd      = probability-weighted expected value
  ceiling_usd = best-case pull

Run:  mtg-watchdog sync-sealed
Cron: 0 2 * * * .../venv/bin/mtg-watchdog sync-sealed
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
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
MTGJSON_SET_LIST = "https://mtgjson.com/api/v5/SetList.json"
MTGJSON_SET_URL = "https://mtgjson.com/api/v5/{code}.json"

CUTOFF_DATE = "2020-01-01"

FOCUS_CATEGORIES = {
    "booster_box",
    "booster_pack",
    "bundle",
    "commander_deck",
    "prerelease_pack",
    "draft_set",
}

# Categories we store but exclude from the top-N ranking display
_ALL_INGEST_CATEGORIES = FOCUS_CATEGORIES | {
    "booster_case", "bundle_case", "multiple_decks", "deck_box",
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


def _stream_download(url: str, target: Path) -> None:
    tmp = target.with_suffix(target.suffix + ".tmp")
    with _client() as c, c.stream("GET", url) as r:
        r.raise_for_status()
        with tmp.open("wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
    tmp.replace(target)


# ---------------------------------------------------------------------------
# Download / cache
# ---------------------------------------------------------------------------

def download_scryfall_bulk(force: bool = False) -> Path:
    target = CACHE_DIR / "scryfall_default_cards.json"
    if target.exists() and not force:
        log.info("scryfall bulk: using cache")
        return target
    log.info("scryfall bulk: fetching index...")
    with _client() as c:
        idx = c.get(SCRYFALL_BULK_INDEX)
        idx.raise_for_status()
        entry = next(e for e in idx.json()["data"] if e["type"] == "default_cards")
    url = entry["download_uri"]
    log.info("scryfall bulk: downloading %s", url)
    _stream_download(url, target)
    log.info("scryfall bulk: done (%d MB)", target.stat().st_size // 1_000_000)
    return target


def download_set_list(force: bool = False) -> Path:
    target = CACHE_DIR / "SetList.json"
    if target.exists() and not force:
        return target
    log.info("SetList: downloading...")
    _stream_download(MTGJSON_SET_LIST, target)
    return target


def download_set(set_code: str) -> Path:
    """Per-set files are cached permanently — released sets don't change."""
    target = CACHE_DIR / f"set_{set_code.upper()}.json"
    if target.exists():
        return target
    url = MTGJSON_SET_URL.format(code=set_code.upper())
    log.debug("mtgjson: downloading %s", set_code)
    try:
        _stream_download(url, target)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            target.write_text("{}")  # stub so we don't retry
        else:
            raise
    return target


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _iter_json_array(path: Path):
    """Stream a top-level JSON array using ijson when available."""
    try:
        import ijson  # type: ignore
        with path.open("rb") as f:
            yield from ijson.items(f, "item")
    except ImportError:
        with path.open("r", encoding="utf-8") as f:
            yield from json.load(f)


def load_scryfall_prices(path: Path) -> dict[str, tuple]:
    """{scryfall_id: (usd, usd_foil)}"""
    def _f(v):
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    out: dict = {}
    for rec in _iter_json_array(path):
        sid = rec.get("id")
        if not sid:
            continue
        p = rec.get("prices") or {}
        usd = _f(p.get("usd"))
        usd_foil = _f(p.get("usd_foil")) or _f(p.get("usd_etched"))
        out[sid] = (usd, usd_foil)
    log.info("scryfall prices: %d cards", len(out))
    return out


def load_set_list(path: Path) -> list[dict]:
    """Return [{code, name, releaseDate, sealedProduct: [...]}] filtered to sets
    that have sealed products and were released after CUTOFF_DATE."""
    with path.open("rb") as f:
        doc = json.load(f)
    sets = doc.get("data", [])
    return [
        s for s in sets
        if (s.get("sealedProduct") or [])
        and (s.get("releaseDate") or "") >= CUTOFF_DATE
    ]


def load_set_file(set_code: str) -> dict:
    path = download_set(set_code)
    with path.open("rb") as f:
        doc = json.load(f)
    return doc.get("data", doc)


# ---------------------------------------------------------------------------
# Build UUID price lookup across all sets
# ---------------------------------------------------------------------------

def build_uuid_prices(
    set_data: dict,
    scryfall_prices: dict,
    into: dict,
) -> None:
    """Add {mtgjson_uuid: (usd, usd_foil)} entries into `into` for cards in set_data."""
    for card in set_data.get("cards", []) or []:
        uuid = card.get("uuid")
        if not uuid:
            continue
        ids = card.get("identifiers") or {}
        scry_id = ids.get("scryfallId")
        if scry_id and scry_id in scryfall_prices:
            usd, usd_foil = scryfall_prices[scry_id]
            into[uuid] = (usd or 0.0, usd_foil or usd or 0.0)
        else:
            into[uuid] = (0.0, 0.0)


# ---------------------------------------------------------------------------
# Valuation math
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
    cards = sheet.get("cards") or {}
    total = sheet.get("totalWeight") or sum(cards.values()) or 1
    foil = bool(sheet.get("foil") or sheet.get("isFoil"))
    ev = 0.0
    nonzero: list[float] = []
    for uuid, weight in cards.items():
        usd, usd_foil = uuid_prices.get(uuid, (0.0, 0.0))
        price = usd_foil if foil else usd
        ev += (weight / total) * price
        if price > 0:
            nonzero.append(price)
    floor = min(nonzero) if nonzero else 0.0
    ceiling = max(nonzero) if nonzero else 0.0
    return ev, floor, ceiling


def _value_booster_blueprint(blueprint: dict, uuid_prices: dict) -> _Val:
    configs = blueprint.get("boosters") or []
    sheets = blueprint.get("sheets") or {}
    total_w = sum(b.get("weight", 1) for b in configs) or 1

    ev = 0.0
    floor = 0.0
    ceiling = 0.0
    count = 0
    first = True

    for cfg in configs:
        w = cfg.get("weight", 1) / total_w
        cfg_ev = cfg_floor = cfg_ceiling = 0.0
        cfg_count = 0
        for sheet_name, slots in (cfg.get("contents") or {}).items():
            sheet = sheets.get(sheet_name) or {}
            s_ev, s_floor, s_ceiling = _sheet_stats(sheet, uuid_prices)
            cfg_ev += s_ev * slots
            cfg_floor += s_floor * slots
            cfg_ceiling += s_ceiling * slots
            cfg_count += slots
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
    default_set_code: str,
    set_cache: dict,        # set_code -> parsed MTGJSON set data
    uuid_prices: dict,      # global mtgjson_uuid -> (usd, usd_foil)
    sealed_by_uuid: dict,   # uuid -> {contents, set_code}
    depth: int = 0,
) -> _Val:
    if depth > 6 or not contents:
        return _Val()

    total = _Val()

    # 1. Explicit fixed card inclusions (promos, Secret Lair singles, etc.)
    for c in contents.get("card", []) or []:
        uuid = c.get("uuid")
        qty = int(c.get("count", 1) or 1)
        foil = bool(c.get("foil"))
        if not uuid:
            continue
        usd, usd_foil = uuid_prices.get(uuid, (0.0, 0.0))
        price = usd_foil if foil else usd
        total += _Val(ev=price * qty, floor=price * qty, ceiling=price * qty, count=qty)

    # 2. Direct booster blueprint references (booster_pack → this is the pack itself)
    for pack in contents.get("pack", []) or []:
        pack_set = (pack.get("set") or default_set_code).upper()
        pack_data = set_cache.get(pack_set, {})
        code = pack.get("code", "default")
        blueprints = pack_data.get("booster") or {}
        blueprint = (
            blueprints.get(code)
            or blueprints.get("play")
            or blueprints.get("default")
            or (next(iter(blueprints.values()), None) if blueprints else None)
        )
        if not blueprint:
            continue
        qty = int(pack.get("count", 1) or 1)
        pack_val = _value_booster_blueprint(blueprint, uuid_prices)
        total += pack_val.scale(qty)

    # 3. Nested sealed references (box → N packs, bundle → packs + card)
    for s in contents.get("sealed", []) or []:
        child_uuid = s.get("uuid")
        qty = int(s.get("count", 1) or 1)
        if not child_uuid:
            continue
        child = sealed_by_uuid.get(child_uuid)
        if not child:
            continue
        child_val = _value_contents(
            child.get("contents") or {},
            (child.get("set_code") or default_set_code).upper(),
            set_cache,
            uuid_prices,
            sealed_by_uuid,
            depth=depth + 1,
        )
        total += child_val.scale(qty)

    return total


# ---------------------------------------------------------------------------
# Main sync entry point
# ---------------------------------------------------------------------------

def run_sync(force_download: bool = False) -> dict:
    """Full nightly sealed-product sync. Returns stats dict."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    db.init_db()

    stats: dict = {}

    # --- Step 1: Scryfall bulk prices ---
    log.info("=== Step 1/4: Scryfall bulk prices ===")
    bulk_path = download_scryfall_bulk(force=force_download)
    log.info("Loading prices...")
    scryfall_prices = load_scryfall_prices(bulk_path)
    stats["scryfall_cards"] = len(scryfall_prices)

    # --- Step 2: SetList → which sets have sealed products ---
    log.info("=== Step 2/4: SetList ===")
    set_list_path = download_set_list(force=force_download)
    sets_with_sealed = load_set_list(set_list_path)
    log.info("Sets with sealed products since %s: %d", CUTOFF_DATE, len(sets_with_sealed))

    # Build the full catalogue of sealed products from SetList
    # (SetList already has category/uuid/name/contents for each product)
    all_products: dict[str, dict] = {}  # uuid -> product info
    set_meta: dict[str, dict] = {}      # code -> {name, releaseDate}
    for s in sets_with_sealed:
        code = s["code"].upper()
        set_meta[code] = {"name": s["name"], "release_date": s.get("releaseDate")}
        for p in (s.get("sealedProduct") or []):
            uuid = p.get("uuid")
            if not uuid:
                continue
            all_products[uuid] = {
                "uuid": uuid,
                "name": p.get("name", ""),
                "set_code": code,
                "category": p.get("category"),
                "release_date": p.get("releaseDate") or s.get("releaseDate"),
                "contents": p.get("contents") or {},
            }

    log.info("Total sealed products in catalogue: %d", len(all_products))
    stats["products_in_catalogue"] = len(all_products)

    # --- Step 3: Per-set MTGJSON data ---
    log.info("=== Step 3/4: Per-set MTGJSON data (%d sets) ===", len(sets_with_sealed))
    set_cache: dict[str, dict] = {}
    uuid_prices: dict[str, tuple] = {}  # global mtgjson_uuid -> (usd, usd_foil)

    for i, s in enumerate(sets_with_sealed, 1):
        code = s["code"].upper()
        try:
            set_data = load_set_file(code)
            set_cache[code] = set_data
            build_uuid_prices(set_data, scryfall_prices, into=uuid_prices)
            if i % 30 == 0:
                log.info("  sets loaded: %d/%d (uuid_prices: %d)",
                         i, len(sets_with_sealed), len(uuid_prices))
        except Exception:
            log.exception("Failed to load set %s", code)
            set_cache[code] = {}

    log.info("UUID prices built: %d cards", len(uuid_prices))
    stats["sets_loaded"] = len(set_cache)

    # --- Step 4: Compute valuations ---
    log.info("=== Step 4/4: Computing valuations ===")
    focus = {
        uuid: p for uuid, p in all_products.items()
        if p.get("category") in FOCUS_CATEGORIES
    }
    log.info("Products to value: %d (category filter: %s)", len(focus), sorted(FOCUS_CATEGORIES))

    written = errors = skipped = 0
    for uuid, p in focus.items():
        code = p["set_code"]
        contents = p.get("contents") or {}
        if not contents:
            skipped += 1
            continue
        try:
            val = _value_contents(
                contents, code, set_cache, uuid_prices, all_products
            )
        except Exception:
            log.exception("Valuation failed for %s (%s)", p["name"], uuid)
            errors += 1
            continue

        smeta = set_meta.get(code, {})
        db.upsert_sealed_product({
            "uuid": uuid,
            "name": p["name"],
            "set_code": code,
            "set_name": smeta.get("name") or "",
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

    stats.update({"written": written, "skipped_no_contents": skipped, "errors": errors})
    log.info("=== Sealed sync complete: %s ===", stats)
    return stats
