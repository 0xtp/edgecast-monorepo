"""The single gate every order passes. Pure functions over a snapshot; caps can only shrink an order."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field

EPS = 1e-9


@dataclass(frozen=True)
class RiskLimits:
    max_stake_per_order: float
    max_exposure_per_market: float
    max_exposure_per_city_day: float
    max_total_exposure: float
    max_daily_loss: float
    max_drawdown_pct: float


@dataclass(frozen=True)
class RiskState:
    halted: bool = False
    paused: bool = False
    geoblocked: bool = False
    exposure_by_market: Mapping[str, float] = field(default_factory=dict)
    exposure_by_city_day: Mapping[str, float] = field(default_factory=dict)
    total_exposure: float = 0.0
    realized_today: float = 0.0  # P&L realised today (negative = loss)
    equity: float = 0.0
    equity_peak: float = 0.0


@dataclass(frozen=True)
class OrderRequest:
    market_id: str
    city_day: str
    side: str  # BUY | SELL
    price: float
    size: float
    fee_per_share: float
    min_size: float = 0.0
    size_step: float = 0.01


@dataclass(frozen=True)
class Decision:
    approved: bool
    size: float
    reason: str
    clipped_by: tuple[str, ...] = ()


def breach(state: RiskState, limits: RiskLimits) -> str | None:
    """A daily-loss or drawdown breach that must halt trading, or None."""
    if -state.realized_today >= limits.max_daily_loss - EPS:
        return f"daily loss ${-state.realized_today:.2f} >= limit ${limits.max_daily_loss:.2f}"
    if state.equity_peak > 0:
        dd = 100.0 * (state.equity_peak - state.equity) / state.equity_peak
        if dd >= limits.max_drawdown_pct - EPS:
            return f"drawdown {dd:.1f}% >= limit {limits.max_drawdown_pct:.1f}%"
    return None


def drawdown_fraction_of_limit(state: RiskState, limits: RiskLimits) -> float:
    """How close we are to the tighter of the two loss limits (0..1+)."""
    loss = max(0.0, -state.realized_today) / limits.max_daily_loss
    dd = 0.0
    if state.equity_peak > 0:
        dd = (state.equity_peak - state.equity) / state.equity_peak * 100.0 / limits.max_drawdown_pct
    return max(loss, dd)


def _floor(size: float, step: float) -> float:
    return round(math.floor(size / step + EPS) * step, 8)


def check(req: OrderRequest, state: RiskState, limits: RiskLimits) -> Decision:
    if req.size <= 0 or not (0.0 < req.price < 1.0):
        return Decision(False, 0.0, "invalid order")
    if state.geoblocked:
        return Decision(False, 0.0, "geoblocked")
    if req.side == "SELL":
        return Decision(True, req.size, "exit")
    if state.halted:
        return Decision(False, 0.0, "halted")
    if state.paused:
        return Decision(False, 0.0, "paused (no new entries)")
    reason = breach(state, limits)
    if reason:
        return Decision(False, 0.0, f"risk breach: {reason}")

    cost = req.price + max(req.fee_per_share, 0.0)
    headroom = {
        "max stake per order": limits.max_stake_per_order,
        "max exposure per market": limits.max_exposure_per_market - state.exposure_by_market.get(req.market_id, 0.0),
        "max exposure per city/day": limits.max_exposure_per_city_day
        - state.exposure_by_city_day.get(req.city_day, 0.0),
        "max total exposure": limits.max_total_exposure - state.total_exposure,
    }
    size = req.size
    clipped: list[str] = []
    for name, room in headroom.items():
        cap = _floor(max(room, 0.0) / cost, req.size_step)
        if cap < size - EPS:
            size = cap
            clipped.append(name)
    size = _floor(size, req.size_step)
    if size <= 0 or size < req.min_size - EPS:
        why = clipped[-1] if clipped else "size"
        return Decision(False, 0.0, f"cap reached: {why}", tuple(clipped))
    return Decision(True, size, "ok" if not clipped else "clipped", tuple(clipped))
