"""Startup / periodic reconciliation of LIVE intents with the exchange. Never re-sends anything.

1. PENDING intents with no exchange id (crash between sign and ack): look for a matching open order or trade
   created after the intent; adopt it if found, otherwise mark FAILED once past the grace period.
2. Exchange orders we do not know about are cancelled and alerted.
3. Fills and terminal states are then synced.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import OrderIntent
from app.execution.live import LiveExecutor, TradingClient
from app.services.state import alert, audit

log = logging.getLogger(__name__)
CLOCK_SKEW = timedelta(seconds=30)
PENDING_GRACE = timedelta(seconds=60)


@dataclass
class ReconcileReport:
    adopted: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    cancelled_unknown: list[str] = field(default_factory=list)
    fills: int = 0


def _same(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol


def reconcile(session: Session, client: TradingClient, *, now: datetime | None = None) -> ReconcileReport:
    now = now or datetime.now(UTC)
    rep = ReconcileReport()
    intents = list(session.scalars(select(OrderIntent).where(OrderIntent.mode == "LIVE")))
    known = {it.exchange_order_id for it in intents if it.exchange_order_id}
    pending = sorted(
        (it for it in intents if it.status == "PENDING" and not it.exchange_order_id), key=lambda it: it.created_at
    )
    ex_open = client.open_orders()
    since = min([it.created_at for it in pending], default=now) - timedelta(minutes=5)
    trades = client.trades(after=since) if pending else []

    for it in pending:
        match = next(
            (
                o
                for o in ex_open
                if o.id not in known
                and o.token_id == it.token_id
                and o.side == it.side
                and _same(o.price, it.price)
                and _same(o.original_size, it.size)
                and o.created_at >= it.created_at - CLOCK_SKEW
            ),
            None,
        )
        order_id = match.id if match else None
        if order_id is None:
            legs: dict[str, float] = {}
            for tr in trades:
                if tr.token_id != it.token_id or tr.matched_at < it.created_at - CLOCK_SKEW:
                    continue
                for oid, price, size in tr.legs:
                    if oid not in known and _same(price, it.price, 1e-9 + 0.5 * 0.01):
                        legs[oid] = legs.get(oid, 0.0) + size
            order_id = next((oid for oid, sz in legs.items() if sz <= it.size + 1e-6), None)
        if order_id:
            it.exchange_order_id, it.status = order_id, "OPEN"
            known.add(order_id)
            rep.adopted.append(it.id)
            audit(session, "WORKER", "RECONCILE_ADOPT", f"intent {it.id} -> order {order_id}")
        elif now - it.created_at > PENDING_GRACE:
            it.status = "FAILED"
            it.error = (it.error + " | " if it.error else "") + "not found on exchange during reconcile; not resent"
            rep.failed.append(it.id)
            audit(session, "WORKER", "RECONCILE_FAIL", f"intent {it.id} not found on exchange")

    for o in ex_open:
        if o.id not in known:
            try:
                client.cancel(o.id)
                rep.cancelled_unknown.append(o.id)
                alert(
                    session,
                    "health",
                    f"⚠️ Cancelled an exchange order this bot has no record of: {o.id} "
                    f"({o.side} {o.original_size:g} @ {o.price:.2f})",
                )
            except Exception as e:
                log.error("could not cancel unknown order %s: %s", o.id, e)
    session.flush()
    rep.fills = LiveExecutor(client).sync(session)
    return rep
