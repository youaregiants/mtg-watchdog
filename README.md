# mtg-watchdog

A local price-alert dashboard for individual **Magic: The Gathering** cards.

Search any printing on Scryfall, set an optional target price, and the built-in
refresh worker polls Scryfall every 12 hours, records a price snapshot, and
raises an alert when a card:

- hits a **new all-time low (ATL)** since you started watching it
- crosses your **target price** from above
- drops **>10 % day-over-day** in any finish (USD, foil, etched)

No accounts, no cloud, no API keys — just a local FastAPI web app backed by one
SQLite file.

> Sibling project: [mtg-sealed-value](https://github.com/youaregiants/mtg-sealed-value)
> tracks sealed product EV; this one tracks individual card prices.

---

## Quick start

```bash
git clone https://github.com/youaregiants/mtg-watchdog.git
cd mtg-watchdog
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
pip install -e .
mtg-watchdog serve              # → http://localhost:8000
```

On first boot the database is created automatically and seeded with **5 iconic
demo cards** (Lightning Bolt LEA, Ragavan MH3, Orcish Bowmasters, Sheoldred DMU,
Counterspell LEA) plus **30 days of synthetic price history** so the dashboard
and charts are immediately useful.

### Alternative launch (uvicorn directly)

```bash
uvicorn mtg_watchdog.main:app --reload --port 8000
```

---

## Features

| Feature | Detail |
|---------|--------|
| **Watchlist** | Table of every watched card: thumbnail, current USD & foil prices, ATL, Δ vs ATL, target price |
| **Search** | Full Scryfall syntax — `"Ragavan set:mh2"`, `t:planeswalker cmc=4`, etc. Returns up to 20 printings with images and prices |
| **Watch / Unwatch** | Add any printing to the watchlist; set an optional target price at add time or edit it later |
| **Edit target price** | Update or clear a card's target price at any time from its detail page |
| **Price history chart** | Responsive line chart (USD / foil / etched) with up to 365 data points per card |
| **Alert feed** | Chronological list of ATL, target-hit, and drop alerts with unseen badge count |
| **Auto-refresh** | Background job polls Scryfall every 12 hours (polite 100 ms delay between cards) |
| **Manual refresh** | "Refresh now" in the nav bar re-polls all watched cards immediately |

---

## Pages

| Path | What it shows |
|------|---------------|
| `/` | Watchlist: current prices, ATL, foil, and Δ for every watched card |
| `/card/{scryfall_id}` | Detail: editable target price, ATL across all finishes, responsive chart, price-history table |
| `/search?q=…` | Scryfall search results with Watch button per printing |
| `/alerts` | Alert feed; visiting the page marks all alerts as seen |

---

## CLI

```bash
# First-time setup (also happens automatically on serve)
mtg-watchdog init

# Launch the web UI
mtg-watchdog serve                          # http://localhost:8000
mtg-watchdog serve --port 9000 --host 0.0.0.0   # custom host/port
mtg-watchdog serve --reload                # auto-reload on code changes

# Add cards from the command line
mtg-watchdog add "Ragavan set:mh2"
mtg-watchdog add "Sheoldred, the Apocalypse" --target 40.00
mtg-watchdog add "Lightning Bolt set:lea"

# List all watched cards + latest prices
mtg-watchdog list

# Trigger a price refresh right now
mtg-watchdog refresh

# Remove a watch (use the scryfall_id from mtg-watchdog list)
mtg-watchdog remove <scryfall_id>
```

---

## Alert types

| Kind | When it fires |
|------|---------------|
| `atl` | New all-time low recorded for a finish, **after** the first observation (so adding a card never fires immediately) |
| `target` | USD price moves at or below your target price from above |
| `drop` | Price falls ≥ 10 % vs. the previous observation in any finish |

---

## Architecture

```
   ┌─────────────────────────┐
   │  Scryfall API           │  HTTPS, no auth required
   │  /cards/search          │
   │  /cards/{id}            │
   └───────────┬─────────────┘
               │  httpx (100 ms delay, 12 h interval)
               ▼
   ┌─────────────────────────┐
   │  refresh.py             │  diff prices → raise alerts
   └───────────┬─────────────┘
               ▼
   ┌─────────────────────────┐
   │  SQLite                 │  watches, price_history, alerts
   │  data/watchdog.db       │
   └───────────┬─────────────┘
               ▼
   ┌─────────────────────────┐
   │  FastAPI + Jinja2       │  templates/ + static/app.css
   │  APScheduler (12 h)     │
   └─────────────────────────┘
```

### Source layout

```
src/mtg_watchdog/
├── main.py          # FastAPI routes + APScheduler setup
├── db.py            # SQLite repository (watches, prices, alerts)
├── scryfall.py      # Scryfall API client (httpx, no auth)
├── refresh.py       # Price refresh + alert logic
├── seed.py          # Demo data seeder (runs once on empty DB)
├── cli.py           # argparse entry point (mtg-watchdog command)
├── templates/       # Jinja2 HTML templates
│   ├── base.html
│   ├── index.html   # watchlist
│   ├── card.html    # per-card detail + chart
│   ├── search.html  # Scryfall search
│   └── alerts.html  # alert feed
└── static/
    └── app.css      # dark-mode CSS, no external dependencies
```

---

## Database schema

```sql
-- Every card you're watching
watches(
    scryfall_id   TEXT PRIMARY KEY,
    name, set_code, set_name, collector_no, rarity,
    image_uri, scryfall_uri,
    target_price  REAL,        -- NULL = no target
    added_at      TEXT         -- ISO 8601 UTC
)

-- One row per card per refresh (up to 365 days shown in chart)
price_history(
    scryfall_id  TEXT,
    observed_at  TEXT,         -- ISO 8601 UTC
    usd          REAL,
    usd_foil     REAL,
    usd_etched   REAL,
    PRIMARY KEY (scryfall_id, observed_at)
)

-- Alert events
alerts(
    id           INTEGER PRIMARY KEY,
    scryfall_id  TEXT,
    kind         TEXT,         -- 'atl' | 'target' | 'drop'
    finish       TEXT,         -- 'usd' | 'usd_foil' | 'usd_etched'
    price        REAL,
    prev_price   REAL,
    message      TEXT,
    created_at   TEXT,
    seen         INTEGER DEFAULT 0
)
```

---

## Development

```bash
# Install in editable mode with dev deps
pip install -e .

# Auto-reload server (templates and static files reload instantly)
mtg-watchdog serve --reload

# Wipe the DB and start fresh with demo data
rm data/watchdog.db && mtg-watchdog init
```

The DB file lives at `data/watchdog.db` (git-ignored). Delete it at any time to
reset to the seeded demo state.

---

## Data source

**[Scryfall API](https://scryfall.com/docs/api)** — card metadata and prices in
USD / USD foil / USD etched. Free, no API key required. The refresh worker
sleeps 100 ms between requests to stay within Scryfall's soft 10 rps cap.

---

## License

MIT
