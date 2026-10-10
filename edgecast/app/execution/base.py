"""Executor interface and the order-submission flow shared by PAPER and LIVE."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Market, OrderIntent
from app.services.state import alert


class Executor(Protocol):
    book: str  # PAPER | LIVE

    def submit(self, session: Session, intent: OrderIntent) -> None:
        """Send a stored PENDING intent. Must leave it PENDING (never resend) if the outcome is unknown."""

    def sync(self, session: Session) -> int:
        """Advance open orders (simulate or poll fills). Returns number of fills recorded."""

    def cancel(self, session: Session, intent: OrderIntent) -> None: ...

    def cancel_all(self, session: Session) -> int: ...


def place_order(
    sessions: sessionmaker[Session] | Callable[[], Session],
    executor: Executor,
    *,
    market: Market,
    outcome: str,
    side: str,
    price: float,
    size: float,
    reason: str,
    signal_id: int | None = None,
    position_id: int | None = None,
) -> str:
    """Persist the intent first (committed), then send it in a second transaction."""
    token = market.token_yes if outcome == "YES" else market.token_no
    with sessions() as s:
        intent = OrderIntent(
            mode=executor.book,
            market_id=market.id,
            token_id=token,
            outcome=outcome,
            side=side,
            price=price,
            size=size,
            reason=reason,
            signal_id=signal_id,
            position_id=position_id,
        )
        s.add(intent)
        s.commit()
        intent_id = intent.id
    with sessions() as s:
        intent = s.get(OrderIntent, intent_id)
        assert intent is not None
        intent.attempts += 1
        try:
            executor.submit(s, intent)
        finally:
            s.commit()
        mk = intent.market
        if intent.status in ("OPEN", "PARTIAL", "FILLED", "PENDING"):
            alert(
                s,
                "order",
                f"📝 {executor.book} {side} {outcome} {size:g} @ {price:.2f} — {mk.bucket_label} · "
                f"{mk.event.title}\nstatus {intent.status} · {reason}",
            )
        elif intent.status in ("REJECTED", "FAILED"):
            alert(s, "order", f"⚠️ {executor.book} order {intent.status}: {intent.error[:200]}")
        s.commit()
    return intent_id
