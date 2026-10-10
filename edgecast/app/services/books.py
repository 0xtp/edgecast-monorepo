"""Position and cash accounting per book (PAPER, LIVE, MANUAL). All cash in integer micro-USD.

Invariant (tested): cash = deposits - buys - fees + sells + redemptions, exactly, and
equity - deposits = realised P&L + unrealised P&L.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import LedgerEntry, Market, OrderIntent, Position, WeatherEvent
from app.risk import RiskState
from app.services.state import get_state, set_state

MICRO = 1_000_000
SIZE_EPS = 1e-6


def to_micros(usd: float) -> int:
    return round(usd * MICRO)


def usd(micros: int) -> float:
    return micros / MICRO


def track_for(book: str) -> str:
    return "MANUAL" if book == "MANUAL" else "BOT"


# ------------------------------------------------------------------ ledger


def deposits(session: Session, book: str) -> int:
    return int(
        session.scalar(
            select(func.coalesce(func.sum(LedgerEntry.amount_micros), 0)).where(
                LedgerEntry.book == book, LedgerEntry.kind.in_(("DEPOSIT", "WITHDRAW"))
            )
        )
        or 0
    )


def ensure_deposit(session: Session, book: str, amount_usd: float, ref: str = "initial bankroll") -> bool:
    exists = session.scalar(select(LedgerEntry.id).where(LedgerEntry.book == book, LedgerEntry.kind == "DEPOSIT"))
    if exists:
        return False
    session.add(LedgerEntry(book=book, kind="DEPOSIT", amount_micros=to_micros(amount_usd), ref=ref))
    session.flush()
    return True


def cash(session: Session, book: str) -> int:
    return int(
        session.scalar(select(func.coalesce(func.sum(LedgerEntry.amount_micros), 0)).where(LedgerEntry.book == book))
        or 0
    )


def realized_since(session: Session, book: str, since: datetime) -> int:
    return int(
        session.scalar(
            select(func.coalesce(func.sum(LedgerEntry.pnl_micros), 0)).where(
                LedgerEntry.book == book, LedgerEntry.ts >= since
            )
        )
        or 0
    )


def start_of_today(tz: str, now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    local = now.astimezone(ZoneInfo(tz))
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


# ------------------------------------------------------------------ positions


def get_or_create_position(
    session: Session, *, book: str, market: Market, outcome: str, signal_id: int | None = None, reason: str = ""
) -> Position:
    pos = session.scalar(
        select(Position).where(
            Position.mode == book,
            Position.market_id == market.id,
            Position.outcome == outcome,
            Position.status == "OPEN",
        )
    )
    if pos is None:
        pos = Position(
            track=track_for(book),
            mode=book,
            market_id=market.id,
            token_id=market.token_yes if outcome == "YES" else market.token_no,
            outcome=outcome,
            signal_id=signal_id,
            entry_reason=reason,
        )
        session.add(pos)
        session.flush()
    return pos


def apply_buy(
    session: Session, book: str, pos: Position, price: float, size: float, fee_per_share: float, ref: str = ""
) -> None:
    gross = to_micros(price * size)
    fee = to_micros(fee_per_share * size)
    session.add(LedgerEntry(book=book, kind="BUY", amount_micros=-gross, position_id=pos.id, ref=ref))
    if fee:
        session.add(LedgerEntry(book=book, kind="FEE", amount_micros=-fee, position_id=pos.id, ref=ref))
    pos.size = round(pos.size + size, 6)
    pos.cost_micros += gross + fee
    pos.status = "OPEN"


def apply_sell(
    session: Session,
    book: str,
    pos: Position,
    price: float,
    size: float,
    fee_per_share: float,
    ref: str = "",
    reason: str = "",
) -> int:
    """Reduce a position. Returns realised P&L in micros (net of the sell fee and the cost portion)."""
    size = min(size, pos.size)
    if size <= SIZE_EPS:
        return 0
    proceeds = to_micros(price * size)
    fee = to_micros(fee_per_share * size)
    closing = pos.size - size <= SIZE_EPS
    cost_part = pos.cost_micros if closing else round(pos.cost_micros * size / pos.size)
    pnl = proceeds - fee - cost_part
    session.add(
        LedgerEntry(book=book, kind="SELL", amount_micros=proceeds, position_id=pos.id, pnl_micros=pnl, ref=ref)
    )
    if fee:
        session.add(LedgerEntry(book=book, kind="FEE", amount_micros=-fee, position_id=pos.id, ref=ref))
    pos.size = 0.0 if closing else round(pos.size - size, 6)
    pos.cost_micros -= cost_part
    pos.realized_micros += pnl
    if closing:
        pos.status, pos.closed_at = "CLOSED", datetime.now(UTC)
        pos.exit_reason = reason or pos.exit_reason
    return pnl


def apply_resolution(session: Session, book: str, pos: Position, won: bool, ref: str = "resolution") -> int:
    if pos.status != "OPEN" or pos.size <= SIZE_EPS:
        return 0
    payout = to_micros(pos.size) if won else 0
    pnl = payout - pos.cost_micros
    session.add(
        LedgerEntry(book=book, kind="REDEEM", amount_micros=payout, position_id=pos.id, pnl_micros=pnl, ref=ref)
    )
    pos.realized_micros += pnl
    pos.cost_micros = 0
    pos.status, pos.closed_at = "RESOLVED", datetime.now(UTC)
    pos.exit_reason = pos.exit_reason or ("resolved WIN" if won else "resolved LOSS")
    pos.last_mark = 1.0 if won else 0.0
    return pnl


def open_positions(session: Session, book: str) -> list[Position]:
    return list(session.scalars(select(Position).where(Position.mode == book, Position.status == "OPEN")))


# ------------------------------------------------------------------ exposure & equity


@dataclass
class BookSnapshot:
    book: str
    cash: float
    deposits: float
    position_value: float
    exposure: float
    exposure_by_market: dict[str, float]
    exposure_by_city_day: dict[str, float]
    realized_today: float
    realized_total: float
    unrealized: float

    @property
    def equity(self) -> float:
        return self.cash + self.position_value

    @property
    def total_pnl(self) -> float:
        return self.equity - self.deposits


def city_day_key(event: WeatherEvent) -> str:
    return f"{event.city}|{event.local_date}"


def snapshot(session: Session, book: str, tz: str = "UTC") -> BookSnapshot:
    by_market: dict[str, float] = {}
    by_city: dict[str, float] = {}
    value = unreal = 0.0
    for pos in open_positions(session, book):
        cost = usd(pos.cost_micros)
        mark = pos.last_mark if pos.last_mark is not None else pos.avg_price
        value += pos.size * mark
        unreal += pos.size * mark - cost
        by_market[pos.market_id] = by_market.get(pos.market_id, 0.0) + cost
        key = city_day_key(pos.market.event)
        by_city[key] = by_city.get(key, 0.0) + cost
    # Resting BUY orders count against exposure until they are filled or cancelled.
    pending = session.scalars(
        select(OrderIntent).where(
            OrderIntent.mode == book,
            OrderIntent.side == "BUY",
            OrderIntent.status.in_(("PENDING", "OPEN", "PARTIAL")),
        )
    )
    for it in pending:
        remaining = max(it.size - it.filled_size, 0.0)
        fee = _fee(it.market, it.price)
        notional = remaining * (it.price + fee)
        by_market[it.market_id] = by_market.get(it.market_id, 0.0) + notional
        key = city_day_key(it.market.event)
        by_city[key] = by_city.get(key, 0.0) + notional
    realized_total = int(
        session.scalar(select(func.coalesce(func.sum(LedgerEntry.pnl_micros), 0)).where(LedgerEntry.book == book)) or 0
    )
    return BookSnapshot(
        book=book,
        cash=usd(cash(session, book)),
        deposits=usd(deposits(session, book)),
        position_value=value,
        exposure=sum(by_market.values()),
        exposure_by_market=by_market,
        exposure_by_city_day=by_city,
        realized_today=usd(realized_since(session, book, start_of_today(tz))),
        realized_total=usd(realized_total),
        unrealized=unreal,
    )


def _fee(market: Market, price: float) -> float:
    if not market.fee_rate:
        return 0.0
    return float(market.fee_rate * (price * (1 - price)) ** (market.fee_exponent or 0.0))


def equity_peak(session: Session, book: str, equity: float) -> float:
    peak = float(get_state(session, f"equity_peak:{book}", 0.0) or 0.0)
    if equity > peak:
        set_state(session, f"equity_peak:{book}", equity)
        peak = equity
    return peak


def risk_state(
    session: Session, book: str, *, tz: str, halted: bool, paused: bool, geoblocked: bool
) -> tuple[RiskState, BookSnapshot]:
    snap = snapshot(session, book, tz)
    peak = equity_peak(session, book, snap.equity)
    return (
        RiskState(
            halted=halted,
            paused=paused,
            geoblocked=geoblocked,
            exposure_by_market=snap.exposure_by_market,
            exposure_by_city_day=snap.exposure_by_city_day,
            total_exposure=snap.exposure,
            realized_today=snap.realized_today,
            equity=snap.equity,
            equity_peak=peak,
        ),
        snap,
    )
