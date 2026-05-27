"""Command-line entry point."""
from __future__ import annotations

import argparse
import json
import sys

from . import db, refresh, scryfall, seed


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mtg-watchdog", description="MTG card price watchdog")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Initialise the database (and seed demo data)")
    sub.add_parser("refresh", help="Refresh prices for every watched card")
    sub.add_parser("list", help="Print every watched card and its latest price")

    sa = sub.add_parser("add", help="Add a card to the watchlist (by Scryfall query)")
    sa.add_argument("query", help='e.g. "Lightning Bolt set:lea"')
    sa.add_argument("--target", type=float, default=None)

    sr = sub.add_parser("remove", help="Remove a watch by scryfall_id")
    sr.add_argument("scryfall_id")

    ss = sub.add_parser("serve", help="Run the FastAPI web UI")
    ss.add_argument("--port", type=int, default=8000)
    ss.add_argument("--host", default="127.0.0.1")
    ss.add_argument("--reload", action="store_true")

    args = p.parse_args(argv)

    if args.cmd == "init":
        db.init_db()
        n = seed.seed()
        print(f"DB ready. Seeded {n} demo cards.")
        return 0

    if args.cmd == "refresh":
        db.init_db()
        print(json.dumps(refresh.refresh_all(), indent=2))
        return 0

    if args.cmd == "list":
        for w in db.list_watches():
            print(f"{w['scryfall_id']:20} {w['name'][:30]:30} {w['set_code']:6} "
                  f"usd={w['usd']}  atl={w['atl_usd']}")
        return 0

    if args.cmd == "add":
        results = scryfall.search(args.query, limit=1)
        if not results:
            print("No matches.", file=sys.stderr)
            return 1
        card = results[0]
        db.add_watch(card, target_price=args.target)
        db.record_price(card["id"], card.get("prices") or {})
        print(f"Watching: {card['name']} [{card.get('set')}] {card['id']}")
        return 0

    if args.cmd == "remove":
        db.remove_watch(args.scryfall_id)
        print("Removed.")
        return 0

    if args.cmd == "serve":
        import uvicorn
        uvicorn.run("mtg_watchdog.main:app", host=args.host, port=args.port,
                    reload=args.reload)
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
