# mtg-watchdog

A local price-alert dashboard for individual **Magic: The Gathering** cards,
plus a nightly sealed-product EV tracker that shows which sealed products have
the highest **guaranteed** single-card value.

No accounts, no cloud, no API keys — FastAPI web app + SQLite.

Live at **https://watchdog.62-238-41-219.sslip.io**

---

## Features

### Card watchlist
- Search any printing via Scryfall full syntax (`"Ragavan set:mh2"`, `t:planeswalker`, etc.)
- Watch any printing; set an optional target price (editable any time from the card detail page)
- **All-time-low (ATL)** alert when a card hits a new price floor since you started watching
- **Target-hit** alert when USD price drops to or below your target
- **Drop alert** when price falls ≥ 10 % day-over-day in any finish (USD / foil / etched)
- Per-card responsive price-history chart (USD, foil, etched), 365-day rolling window
- Nightly price refresh via cron at **03:30 UTC** (`mtg-watchdog refresh`)

### Sealed product EV (`/sealed`)
- Downloads Scryfall bulk prices + MTGJSON booster blueprints nightly (02:00 UTC)
- Covers **102+ sets** since 2020, **1 000+ products**: booster boxes, bundles,
  commander decks, booster packs, prerelease packs, draft sets
- Three value columns per product:
  - **Floor $** — guaranteed minimum: worst-case pull from every booster slot
  - **EV $** — probability-weighted expected value
  - **Ceiling $** — best-case pull
- Ranked by floor (highest guaranteed value first); filterable by product category
- All values based solely on **current Scryfall prices** — no stale estimates

---

## Quick start

```bash
git clone https://github.com/youaregiants/mtg-watchdog.git
cd mtg-watchdog
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt && pip install -e .
mtg-watchdog serve              # → http://localhost:8000
```

The DB is created automatically on first boot with no demo data.

### First sealed sync (5–15 min, one time)

```bash
mtg-watchdog sync-sealed
```

This downloads ~120 MB from Scryfall and ~50 MB from MTGJSON (per-set files are
cached permanently; subsequent nightly runs only re-download Scryfall bulk and
re-compute valuations, taking ~3–5 min).

---

## CLI

```bash
mtg-watchdog serve                            # web UI on :8000
mtg-watchdog serve --host 0.0.0.0 --port 9000 --reload

mtg-watchdog refresh                          # re-poll Scryfall for all watched cards
mtg-watchdog sync-sealed                      # full sealed EV sync
mtg-watchdog sync-sealed --force              # re-download cached files too

mtg-watchdog add "Ragavan set:mh2"
mtg-watchdog add "Sheoldred, the Apocalypse" --target 40.00
mtg-watchdog list
mtg-watchdog remove <scryfall_id>
mtg-watchdog init                             # initialise / migrate the DB
```

---

## Pages

| Path | What it shows |
|------|---------------|
| `/` | Watchlist with USD, foil, ATL, Δ vs ATL, target price |
| `/card/{id}` | Detail: editable target, ATL per finish, responsive chart, history table |
| `/search?q=…` | Scryfall results with Watch button per printing |
| `/alerts` | Chronological alert feed (ATL / target-hit / drop) |
| `/sealed?category=…` | Sealed EV ranked by floor; filterable by category |

---

## Cron schedule

```
/etc/cron.d/mtg-watchdog

0  2 * * *  mtg-watchdog sync-sealed   # Scryfall bulk + MTGJSON → sealed EV
30 3 * * *  mtg-watchdog refresh       # watchlist price refresh + alert check
```

---

## Alert types

| Kind | Trigger |
|------|---------|
| `atl` | New all-time low for a finish (after the first observation) |
| `target` | USD crosses from above to at-or-below your target |
| `drop` | ≥ 10 % single-day drop in any finish |

---

## Architecture

```
   ┌──────────────────────┐   ┌────────────────────────┐
   │  Scryfall API        │   │  MTGJSON API           │
   │  /cards/search       │   │  SetList.json          │
   │  /cards/{id}         │   │  per-set {CODE}.json   │
   │  bulk default-cards  │   │  (cached permanently)  │
   └──────────┬───────────┘   └──────────┬─────────────┘
              │                          │
              ▼                          ▼
   ┌──────────────────────────────────────────────────┐
   │  refresh.py          sealed_sync.py              │
   │  (watchlist prices)  (booster EV math)           │
   └──────────────────────────┬───────────────────────┘
                              ▼
   ┌──────────────────────────────────────────────────┐
   │  SQLite  data/watchdog.db                        │
   │  watches · price_history · alerts                │
   │  sealed_products                                 │
   └──────────────────────────┬───────────────────────┘
                              ▼
   ┌──────────────────────────────────────────────────┐
   │  FastAPI + Jinja2 · APScheduler                  │
   │  templates/ · static/app.css                     │
   └──────────────────────────────────────────────────┘
```

### Source layout

```
src/mtg_watchdog/
├── main.py          # FastAPI routes
├── db.py            # SQLite repository
├── scryfall.py      # Scryfall card search + price fetch
├── refresh.py       # Watchlist price refresh + alert logic
├── sealed_sync.py   # Nightly sealed EV sync (Scryfall bulk + MTGJSON)
├── cli.py           # CLI entry point
├── templates/
│   ├── base.html
│   ├── index.html   # watchlist
│   ├── card.html    # per-card detail + chart
│   ├── search.html
│   ├── alerts.html
│   └── sealed.html  # sealed EV ranking
└── static/app.css
```

---

## Database schema

```sql
watches(scryfall_id PK, name, set_code, set_name, collector_no, rarity,
        image_uri, scryfall_uri, target_price, added_at)

price_history(scryfall_id, observed_at, usd, usd_foil, usd_etched)

alerts(id, scryfall_id, kind, finish, price, prev_price, message, created_at, seen)

sealed_products(uuid PK, name, set_code, set_name, category, release_date,
                floor_usd, ev_usd, ceiling_usd, card_count, valuation_kind, updated_at)
```

---

## Data sources

- **[Scryfall](https://scryfall.com/docs/api)** — card metadata + USD prices (free, no key)
- **[MTGJSON](https://mtgjson.com/api/v5/)** — booster blueprints + sealed product definitions (free, no key)

---

## License

MIT
