"""Read-only JSON API for an external front-end (e.g. a Vercel app). Bearer-token protected; never writes.

Enable by setting API_TOKEN (>= 24 chars). Allow your front-end's origin with API_CORS_ORIGINS.
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select

from app.cli import signal_rows
from app.config import get_settings
from app.db.models import EquitySnapshot, Position
from app.db.session import session_scope
from app.services import books
from app.web import queries

router = APIRouter(prefix="/api/v1")


def require_token(request: Request) -> None:
    token = get_settings().API_TOKEN.get_secret_value()
    if not token:
        raise HTTPException(404, "API disabled (set API_TOKEN)")
    sent = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not secrets.compare_digest(sent, token):
        raise HTTPException(401, "bad token")


@router.get("/status", dependencies=[Depends(require_token)])
def status() -> dict[str, Any]:
    s = get_settings()
    with session_scope() as db:
        hdr = queries.header(db, s)
        cards = {b: queries.book_card(db, b, s.TZ) for b in ("PAPER", "LIVE", "MANUAL")}
    return {
        "mode": hdr["mode"],
        "halted": hdr["halted"],
        "halt_reason": hdr["halt_reason"],
        "paused": hdr["paused"],
        "geoblocked": hdr["geoblocked"],
        "worker_last_seen": hdr["worker_ts"].isoformat() if hdr["worker_ts"] else None,
        "books": {k: v for k, v in cards.items() if v},
    }


@router.get("/signals", dependencies=[Depends(require_token)])
def signals(include_expired: bool = False) -> list[dict[str, Any]]:
    with session_scope() as db:
        return signal_rows(db, include_expired=include_expired)


@router.get("/positions", dependencies=[Depends(require_token)])
def positions() -> list[dict[str, Any]]:
    with session_scope() as db:
        out = []
        for p in db.scalars(select(Position).where(Position.status == "OPEN")):
            mark = p.last_mark if p.last_mark is not None else p.avg_price
            out.append(
                {
                    "id": p.id,
                    "track": p.track,
                    "book": p.mode,
                    "outcome": p.outcome,
                    "size": p.size,
                    "avg_price": round(p.avg_price, 4),
                    "mark": mark,
                    "cost_usd": books.usd(p.cost_micros),
                    "unrealized_usd": round(p.size * mark - books.usd(p.cost_micros), 4),
                    "bucket": p.market.bucket_label,
                    "question": p.market.event.title,
                    "url": p.market.url,
                    "exit_signal": p.exit_signal or None,
                }
            )
        return out


@router.get("/equity", dependencies=[Depends(require_token)])
def equity(book: str = "PAPER", limit: int = 2000) -> list[dict[str, Any]]:
    with session_scope() as db:
        rows = db.scalars(
            select(EquitySnapshot)
            .where(EquitySnapshot.book == book.upper())
            .order_by(EquitySnapshot.ts.desc())
            .limit(min(limit, 10_000))
        ).all()
        return [
            {"ts": r.ts.isoformat(), "equity": r.equity, "cash": r.cash, "exposure": r.exposure} for r in reversed(rows)
        ]
