"""The simple path: one command that finds signals and prints them. No web app, no Telegram, no orders.

    python -m app.cli scan              # sync markets + forecasts, compute signals, print a table
    python -m app.cli scan --json       # same, as JSON (pipe it anywhere, e.g. a Vercel app or a sheet)
    python -m app.cli status            # mode, bankroll and P&L per book

Cron example (every 15 min): */15 * * * * cd /srv/edgecast && python -m app.cli scan --json > /srv/signals.json
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from sqlalchemy import select

from app.config import get_settings
from app.db.models import Signal
from app.db.session import get_sessionmaker, migrate


def signal_rows(session: Any, *, include_expired: bool = False) -> list[dict[str, Any]]:
    q = select(Signal).order_by(Signal.edge.desc())
    if not include_expired:
        q = q.where(Signal.status == "ACTIVE")
    out = []
    for g in session.scalars(q):
        m, ev = g.market, g.market.event
        out.append(
            {
                "id": g.id,
                "status": g.status,
                "action": f"BUY {g.outcome}",
                "city": ev.city,
                "date": str(ev.local_date),
                "variable": "high" if ev.variable == "max" else "low",
                "bucket": m.bucket_label,
                "model_pct": round(100 * g.p_model, 1),
                "market_pct": round(100 * g.ask, 1),
                "edge_pct": round(100 * g.edge, 1),
                "max_limit_price": g.max_limit_price,
                "suggested_stake_usd": g.suggested_stake_usd,
                "confidence": g.confidence,
                "liquidity_usd": round(g.liquidity_usd, 2),
                "forecast_run": g.forecast_run.isoformat() if g.forecast_run else None,
                "resolves_at": g.resolves_at.isoformat() if g.resolves_at else None,
                "url": m.url,
                "question": ev.title,
                "bot_action": g.action,
                "expire_reason": g.expire_reason,
            }
        )
    return out


def print_table(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("No active signals.")
        return
    cols = [
        "id",
        "action",
        "city",
        "date",
        "bucket",
        "model_pct",
        "market_pct",
        "edge_pct",
        "max_limit_price",
        "suggested_stake_usd",
        "confidence",
    ]
    widths = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    for r in rows:
        print("  ".join(str(r[c]).ljust(widths[c]) for c in cols))
    print()
    for r in rows:
        print(f"#{r['id']}: {r['url']}")


def main(argv: list[str] | None = None, engine: Any = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m app.cli", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("scan", help="find signals now and print them")
    sc.add_argument("--json", action="store_true")
    sc.add_argument("--no-refresh", action="store_true", help="only print what the worker already computed")
    sub.add_parser("status", help="mode, bankroll and P&L")
    a = ap.parse_args(argv)
    settings = get_settings()
    migrate()
    sessions = get_sessionmaker()
    if a.cmd == "status":
        from app.alerts import status_text

        with sessions() as s:
            print(status_text(s, settings))
        return 0
    if not a.no_refresh:
        if engine is None:
            from app.services.engine import Clients, Engine

            engine = Engine(sessions, settings, Clients.from_settings(settings))
        engine.startup()
        engine.sync_markets()
        engine.refresh_forecasts()
        engine.run_strategy()  # places orders only if the stored mode is PAPER or LIVE
    with sessions() as s:
        rows = signal_rows(s)
    if a.json:
        json.dump(rows, sys.stdout, indent=2)
        print()
    else:
        print_table(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
