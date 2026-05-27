"""Tiny Scryfall API client. Free, no auth, JSON in/out."""
from __future__ import annotations

import httpx

BASE = "https://api.scryfall.com"
UA = {"User-Agent": "mtg-watchdog/0.1 (+local)", "Accept": "application/json"}
TIMEOUT = httpx.Timeout(10.0, connect=5.0)


def search(query: str, limit: int = 10) -> list[dict]:
    """Full-text search (Scryfall syntax). Returns up to `limit` cards."""
    with httpx.Client(timeout=TIMEOUT, headers=UA) as client:
        r = client.get(f"{BASE}/cards/search", params={"q": query, "unique": "prints"})
        if r.status_code == 404:
            return []
        r.raise_for_status()
        return r.json().get("data", [])[:limit]


def get_card(scryfall_id: str) -> dict:
    with httpx.Client(timeout=TIMEOUT, headers=UA) as client:
        r = client.get(f"{BASE}/cards/{scryfall_id}")
        r.raise_for_status()
        return r.json()


def get_card_prices(scryfall_id: str) -> dict:
    card = get_card(scryfall_id)
    return card.get("prices") or {}
