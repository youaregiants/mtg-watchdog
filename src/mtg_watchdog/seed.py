"""Seed the DB with a small demo set of iconic cards + a sprinkle of history.

Lets a fresh clone run with no network calls. Real data arrives on the first
refresh job.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from . import db

DEMO_CARDS = [
    {
        "id": "demo-bolt-lea",
        "name": "Lightning Bolt",
        "set": "lea",
        "set_name": "Limited Edition Alpha",
        "collector_number": "161",
        "rarity": "common",
        "image_uris": {"normal": "https://cards.scryfall.io/normal/front/c/e/ce711943-c1a1-43a0-8b89-8d169cfb8e06.jpg"},
        "scryfall_uri": "https://scryfall.com/card/lea/161/lightning-bolt",
        "prices": {"usd": "550.00"},
        "target": 500.0,
    },
    {
        "id": "demo-counter-lea",
        "name": "Counterspell",
        "set": "lea",
        "set_name": "Limited Edition Alpha",
        "collector_number": "55",
        "rarity": "uncommon",
        "image_uris": {"normal": "https://cards.scryfall.io/normal/front/8/9/89cb8c5f-8f9d-4c8a-92b8-9b3e8a39b8f8.jpg"},
        "scryfall_uri": "https://scryfall.com/card/lea/55/counterspell",
        "prices": {"usd": "1200.00"},
        "target": 1000.0,
    },
    {
        "id": "demo-rag-mh3",
        "name": "Ragavan, Nimble Pilferer",
        "set": "mh3",
        "set_name": "Modern Horizons 3",
        "collector_number": "138",
        "rarity": "mythic",
        "image_uris": {"normal": "https://cards.scryfall.io/normal/front/d/2/d2e76e2c-1a8a-4c8e-9f8b-2d4b8e9a3b4c.jpg"},
        "scryfall_uri": "https://scryfall.com/card/mh2/138/ragavan-nimble-pilferer",
        "prices": {"usd": "65.00", "usd_foil": "180.00"},
        "target": 50.0,
    },
    {
        "id": "demo-orcish-bw",
        "name": "Orcish Bowmasters",
        "set": "ltr",
        "set_name": "The Lord of the Rings: Tales of Middle-earth",
        "collector_number": "103",
        "rarity": "rare",
        "image_uris": {"normal": "https://cards.scryfall.io/normal/front/0/3/03f43c70-cbf6-4d4f-8b8a-2e3f3c8a5a4f.jpg"},
        "scryfall_uri": "https://scryfall.com/card/ltr/103/orcish-bowmasters",
        "prices": {"usd": "32.00", "usd_foil": "78.00"},
        "target": 25.0,
    },
    {
        "id": "demo-sheoldred-dmu",
        "name": "Sheoldred, the Apocalypse",
        "set": "dmu",
        "set_name": "Dominaria United",
        "collector_number": "107",
        "rarity": "mythic",
        "image_uris": {"normal": "https://cards.scryfall.io/normal/front/b/a/ba9fbf02-3bbb-4dca-9046-1ea84a35fa7c.jpg"},
        "scryfall_uri": "https://scryfall.com/card/dmu/107/sheoldred-the-apocalypse",
        "prices": {"usd": "55.00", "usd_foil": "120.00"},
        "target": 40.0,
    },
]


def seed() -> int:
    """Idempotent: only seeds if `watches` is empty. Returns rows inserted."""
    db.init_db()
    existing = db.list_watches()
    if existing:
        return 0

    rng = random.Random(42)
    now = datetime.now(timezone.utc)

    for card in DEMO_CARDS:
        db.add_watch(card, target_price=card.get("target"))
        # Synthesize 30 days of history that drifts around current price
        base_usd = float(card["prices"]["usd"])
        base_foil = float(card["prices"].get("usd_foil") or 0) or None
        for days_ago in range(30, -1, -1):
            t = (now - timedelta(days=days_ago)).isoformat(timespec="seconds")
            drift = 1 + rng.uniform(-0.18, 0.22)
            usd = round(base_usd * drift, 2)
            usd_foil = round(base_foil * drift, 2) if base_foil else None
            with db.connect() as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO price_history
                       (scryfall_id, observed_at, usd, usd_foil, usd_etched)
                       VALUES (?, ?, ?, ?, ?)""",
                    (card["id"], t, usd, usd_foil, None),
                )

    # Add a couple of sample alerts so the alerts page isn't empty
    db.add_alert("demo-rag-mh3", "atl", "usd", 65.00, 72.00,
                 "New all-time low: $65.00 (usd)")
    db.add_alert("demo-sheoldred-dmu", "drop", "usd", 55.00, 64.00,
                 "14% drop: $64.00 → $55.00 (usd)")
    return len(DEMO_CARDS)
