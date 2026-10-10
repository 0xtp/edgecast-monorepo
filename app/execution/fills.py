"""Recording fills: one place that turns an exchange (or simulated) fill into positions and ledger entries."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Fill, Market, OrderIntent
from app.services import books
from app.services.state import alert

log = logging.getLogger(__name__)
TERMINAL = ("FILLED", "CANCELLED", "FAILED", "REJECTED")


def fee_per_share(market: Market, price: float) -> float:
    """Taker fee per share. Raises if the market's fee data is unknown (we never assume zero)."""
    if market.fee_rate is None or market.fee_exponent is None:
        raise ValueError(f"fee parameters unknown for market {market.id}")
    if market.fee_rate <= 0:
        return 0.0
    return float(market.fee_rate * (price * (1.0 - price)) ** market.fee_exponent)


def record_fill(
    session: Session, intent: OrderIntent, *, trade_id: str, price: float, size: float, ts: datetime | None = None
) -> Fill | None:
    """Idempotent on trade_id. Updates the intent, the position and the book's ledger."""
    if size <= 0:
        return None
    if session.scalar(select(Fill.id).where(Fill.exchange_trade_id == trade_id)):
        return None
    remaining = max(intent.size - intent.filled_size, 0.0)
    size = round(min(size, remaining), 6)
    if size <= 0:
        return None
    market = intent.market
    fee = fee_per_share(market, price)
    fill = Fill(
        intent_id=intent.id,
        exchange_trade_id=trade_id,
        price=price,
        size=size,
        fee_micros=books.to_micros(fee * size),
        ts=ts or datetime.now(UTC),
    )
    session.add(fill)
    if intent.side == "BUY":
        pos = books.get_or_create_position(
            session,
            book=intent.mode,
            market=market,
            outcome=intent.outcome,
            signal_id=intent.signal_id,
            reason=intent.reason,
        )
        books.apply_buy(session, intent.mode, pos, price, size, fee, ref=trade_id)
        intent.position_id = pos.id
    else:
        pos = session.get(books.Position, intent.position_id) if intent.position_id else None
        if pos is None:
            log.error("sell fill %s without a position (intent %s)", trade_id, intent.id)
        else:
            books.apply_sell(session, intent.mode, pos, price, size, fee, ref=trade_id, reason=intent.reason)
    intent.filled_size = round(intent.filled_size + size, 6)
    intent.status = "FILLED" if intent.size - intent.filled_size <= 1e-6 else "PARTIAL"
    q = market.event.title
    alert(
        session,
        "order",
        f"{'✅' if intent.status == 'FILLED' else '◐'} {intent.mode} {intent.side} {intent.outcome} "
        f"{size:g} @ {price:.2f} — {market.bucket_label} · {q}\n"
        f"{'filled' if intent.status == 'FILLED' else f'partial {intent.filled_size:g}/{intent.size:g}'}"
        f" · fee ${fee * size:.4f} · {intent.reason}",
    )
    session.flush()
    return fill
