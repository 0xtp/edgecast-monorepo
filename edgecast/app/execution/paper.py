"""PAPER: fills simulated against the live order book, with real fees and real P&L accounting.

Conservative fill model: we only fill as a taker, walking visible levels at or better than our limit. A
resting remainder fills later only when a *new* book snapshot shows liquidity at our price, so the same
displayed liquidity is never consumed twice. No maker/queue fills are assumed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import OrderIntent
from app.execution.fills import record_fill
from app.polymarket.clob import OrderBook

EPS = 1e-9
log = logging.getLogger(__name__)


class PaperExecutor:
    book = "PAPER"

    def __init__(self, book_fn: Callable[[str], OrderBook], max_depth_pct: float = 100.0):
        self.book_fn = book_fn
        self.max_depth_pct = max_depth_pct
        self._seen: dict[str, str] = {}  # intent id -> last book snapshot used

    def _match(self, session: Session, intent: OrderIntent, ob: OrderBook) -> int:
        snap = ob.timestamp or str([(lv.price, lv.size) for lv in (ob.asks + ob.bids)[:6]])
        if self._seen.get(intent.id) == snap:
            return 0
        self._seen[intent.id] = snap
        levels = ob.asks if intent.side == "BUY" else ob.bids
        crosses = (lambda p: p <= intent.price + EPS) if intent.side == "BUY" else (lambda p: p >= intent.price - EPS)
        remaining = intent.size - intent.filled_size
        n = 0
        for lv in levels:
            if remaining <= EPS or not crosses(lv.price):
                break
            take = round(min(remaining, lv.size * self.max_depth_pct / 100.0), 2)
            if take <= 0:
                continue
            fill = record_fill(
                session, intent, trade_id=f"paper-{intent.id}-{intent.filled_size:.6f}", price=lv.price, size=take
            )
            if fill is not None:
                n += 1
                remaining -= fill.size
        return n

    def submit(self, session: Session, intent: OrderIntent) -> None:
        intent.exchange_order_id = f"paper-{intent.id}"
        intent.status = "OPEN"
        try:
            ob = self.book_fn(intent.token_id)
        except Exception as e:  # book unavailable: order rests, will be matched on sync
            intent.error = f"book fetch failed: {e}"
            return
        self._match(session, intent, ob)

    def sync(self, session: Session) -> int:
        n = 0
        for intent in session.scalars(
            select(OrderIntent).where(OrderIntent.mode == "PAPER", OrderIntent.status.in_(("OPEN", "PARTIAL")))
        ):
            try:
                ob = self.book_fn(intent.token_id)
            except Exception as e:
                log.warning("paper sync: book fetch failed for %s: %s", intent.token_id, e)
                continue
            n += self._match(session, intent, ob)
        return n

    def cancel(self, session: Session, intent: OrderIntent) -> None:
        if intent.status in ("OPEN", "PARTIAL", "PENDING"):
            intent.status = "CANCELLED"
            self._seen.pop(intent.id, None)

    def cancel_all(self, session: Session) -> int:
        rows = list(
            session.scalars(
                select(OrderIntent).where(
                    OrderIntent.mode == "PAPER", OrderIntent.status.in_(("OPEN", "PARTIAL", "PENDING"))
                )
            )
        )
        for it in rows:
            self.cancel(session, it)
        return len(rows)
