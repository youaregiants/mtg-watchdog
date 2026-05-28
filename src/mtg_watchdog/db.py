"""SQLite repository layer for mtg-watchdog."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "watchdog.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
    scryfall_id   TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    set_code      TEXT NOT NULL,
    set_name      TEXT NOT NULL,
    collector_no  TEXT NOT NULL,
    rarity        TEXT,
    image_uri     TEXT,
    scryfall_uri  TEXT,
    target_price  REAL,
    added_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS price_history (
    scryfall_id  TEXT NOT NULL,
    observed_at  TEXT NOT NULL,
    usd          REAL,
    usd_foil     REAL,
    usd_etched   REAL,
    PRIMARY KEY (scryfall_id, observed_at),
    FOREIGN KEY (scryfall_id) REFERENCES watches(scryfall_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS alerts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scryfall_id  TEXT NOT NULL,
    kind         TEXT NOT NULL,   -- 'atl' | 'target' | 'drop'
    finish       TEXT NOT NULL,   -- 'usd' | 'usd_foil' | 'usd_etched'
    price        REAL NOT NULL,
    prev_price   REAL,
    message      TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    seen         INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (scryfall_id) REFERENCES watches(scryfall_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_history_card ON price_history(scryfall_id);
CREATE INDEX IF NOT EXISTS idx_alerts_card  ON alerts(scryfall_id);

CREATE TABLE IF NOT EXISTS sealed_products (
    uuid          TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    set_code      TEXT NOT NULL,
    set_name      TEXT,
    category      TEXT,
    release_date  TEXT,
    floor_usd     REAL,
    ev_usd        REAL,
    ceiling_usd   REAL,
    card_count    INTEGER,
    valuation_kind TEXT,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sealed_floor    ON sealed_products(floor_usd DESC);
CREATE INDEX IF NOT EXISTS idx_sealed_category ON sealed_products(category);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# --- watches ---------------------------------------------------------------

def add_watch(card: dict, target_price: Optional[float] = None) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO watches
              (scryfall_id, name, set_code, set_name, collector_no, rarity,
               image_uri, scryfall_uri, target_price, added_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(
              (SELECT added_at FROM watches WHERE scryfall_id = ?), ?))
            """,
            (
                card["id"],
                card["name"],
                card.get("set", ""),
                card.get("set_name", ""),
                card.get("collector_number", ""),
                card.get("rarity", ""),
                _image_uri(card),
                card.get("scryfall_uri", ""),
                target_price,
                card["id"],
                now_iso(),
            ),
        )


def _image_uri(card: dict) -> str:
    imgs = card.get("image_uris") or {}
    if not imgs and card.get("card_faces"):
        imgs = card["card_faces"][0].get("image_uris", {})
    return imgs.get("normal") or imgs.get("small") or ""


def remove_watch(scryfall_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM watches WHERE scryfall_id = ?", (scryfall_id,))


def update_target(scryfall_id: str, target_price: Optional[float]) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE watches SET target_price = ? WHERE scryfall_id = ?",
            (target_price, scryfall_id),
        )


def list_watches() -> list[sqlite3.Row]:
    with connect() as conn:
        return list(
            conn.execute(
                """
                SELECT w.*,
                  (SELECT usd        FROM price_history p WHERE p.scryfall_id = w.scryfall_id
                     ORDER BY observed_at DESC LIMIT 1) AS usd,
                  (SELECT usd_foil   FROM price_history p WHERE p.scryfall_id = w.scryfall_id
                     ORDER BY observed_at DESC LIMIT 1) AS usd_foil,
                  (SELECT usd_etched FROM price_history p WHERE p.scryfall_id = w.scryfall_id
                     ORDER BY observed_at DESC LIMIT 1) AS usd_etched,
                  (SELECT MIN(usd)        FROM price_history p WHERE p.scryfall_id = w.scryfall_id AND usd        IS NOT NULL) AS atl_usd,
                  (SELECT MIN(usd_foil)   FROM price_history p WHERE p.scryfall_id = w.scryfall_id AND usd_foil   IS NOT NULL) AS atl_foil,
                  (SELECT MIN(usd_etched) FROM price_history p WHERE p.scryfall_id = w.scryfall_id AND usd_etched IS NOT NULL) AS atl_etched
                FROM watches w
                ORDER BY w.name COLLATE NOCASE
                """
            )
        )


def get_watch(scryfall_id: str) -> Optional[sqlite3.Row]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM watches WHERE scryfall_id = ?", (scryfall_id,)
        ).fetchone()
        return row


def history_for(scryfall_id: str, limit: int = 365) -> list[sqlite3.Row]:
    with connect() as conn:
        return list(
            conn.execute(
                """
                SELECT observed_at, usd, usd_foil, usd_etched
                FROM price_history
                WHERE scryfall_id = ?
                ORDER BY observed_at DESC
                LIMIT ?
                """,
                (scryfall_id, limit),
            )
        )


def latest_price(scryfall_id: str) -> Optional[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            """
            SELECT * FROM price_history WHERE scryfall_id = ?
            ORDER BY observed_at DESC LIMIT 1
            """,
            (scryfall_id,),
        ).fetchone()


def atl(scryfall_id: str, column: str) -> Optional[float]:
    assert column in {"usd", "usd_foil", "usd_etched"}
    with connect() as conn:
        row = conn.execute(
            f"SELECT MIN({column}) AS m FROM price_history WHERE scryfall_id = ? AND {column} IS NOT NULL",
            (scryfall_id,),
        ).fetchone()
        return row["m"] if row else None


# --- prices ---------------------------------------------------------------

def record_price(scryfall_id: str, prices: dict) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO price_history
              (scryfall_id, observed_at, usd, usd_foil, usd_etched)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                scryfall_id,
                now_iso(),
                _as_float(prices.get("usd")),
                _as_float(prices.get("usd_foil")),
                _as_float(prices.get("usd_etched")),
            ),
        )


def _as_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# --- alerts ---------------------------------------------------------------

def add_alert(scryfall_id: str, kind: str, finish: str, price: float,
              prev_price: Optional[float], message: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO alerts (scryfall_id, kind, finish, price, prev_price, message, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (scryfall_id, kind, finish, price, prev_price, message, now_iso()),
        )


def list_alerts(limit: int = 100) -> list[sqlite3.Row]:
    with connect() as conn:
        return list(
            conn.execute(
                """
                SELECT a.*, w.name, w.set_code, w.image_uri
                FROM alerts a JOIN watches w ON w.scryfall_id = a.scryfall_id
                ORDER BY a.created_at DESC LIMIT ?
                """,
                (limit,),
            )
        )


def mark_alerts_seen() -> None:
    with connect() as conn:
        conn.execute("UPDATE alerts SET seen = 1 WHERE seen = 0")


def unseen_alert_count() -> int:
    with connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM alerts WHERE seen = 0").fetchone()
        return row["c"] if row else 0


# --- sealed products -------------------------------------------------------

def upsert_sealed_product(row: dict) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO sealed_products
              (uuid, name, set_code, set_name, category, release_date,
               floor_usd, ev_usd, ceiling_usd, card_count, valuation_kind, updated_at)
            VALUES (:uuid, :name, :set_code, :set_name, :category, :release_date,
               :floor_usd, :ev_usd, :ceiling_usd, :card_count, :valuation_kind, :updated_at)
            ON CONFLICT(uuid) DO UPDATE SET
              name=excluded.name, set_name=excluded.set_name,
              floor_usd=excluded.floor_usd, ev_usd=excluded.ev_usd,
              ceiling_usd=excluded.ceiling_usd, card_count=excluded.card_count,
              valuation_kind=excluded.valuation_kind, updated_at=excluded.updated_at
            """,
            row,
        )


def list_sealed_top(category: Optional[str] = None, limit: int = 100) -> list[sqlite3.Row]:
    with connect() as conn:
        if category:
            return list(conn.execute(
                "SELECT * FROM sealed_products WHERE category=? AND floor_usd > 0 "
                "ORDER BY floor_usd DESC LIMIT ?",
                (category, limit),
            ))
        return list(conn.execute(
            "SELECT * FROM sealed_products WHERE floor_usd > 0 "
            "ORDER BY floor_usd DESC LIMIT ?",
            (limit,),
        ))


def sealed_categories() -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT category FROM sealed_products "
            "WHERE floor_usd > 0 ORDER BY category"
        ).fetchall()
        return [r["category"] for r in rows if r["category"]]


def sealed_last_synced() -> Optional[str]:
    with connect() as conn:
        row = conn.execute(
            "SELECT MAX(updated_at) AS t FROM sealed_products"
        ).fetchone()
        return row["t"] if row else None
