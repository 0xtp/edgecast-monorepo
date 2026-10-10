"""Fractional Kelly sizing for a binary contract bought at a limit price (fees included)."""

from __future__ import annotations

import math
from dataclasses import dataclass

SIZE_STEP = 0.01  # share size increment


def kelly_fraction(p: float, price: float, fee_per_share: float) -> float:
    """Full-Kelly bankroll fraction for paying (price + fee) to win 1 with probability p."""
    cost = price + fee_per_share
    if cost <= 0 or cost >= 1 or p <= cost:
        return 0.0
    return (p - cost) / (1.0 - cost)


def floor_size(size: float, step: float = SIZE_STEP) -> float:
    return round(math.floor(size / step + 1e-9) * step, 8)


@dataclass(frozen=True)
class SizeResult:
    shares: float
    stake_usd: float  # shares * (price + fee)
    kelly_full: float
    reason: str  # why it is zero, or what clipped it


def size_order(
    *,
    p: float,
    price: float,
    fee_per_share: float,
    bankroll: float,
    kelly_mult: float,
    max_stake_usd: float,
    depth_shares: float,
    max_depth_pct: float,
    min_order_size: float,
    round_up_to_min: bool = False,
) -> SizeResult:
    """Fractional Kelly, clipped by max stake and book depth, floored to the size step.

    With `round_up_to_min`, a positive-edge order that comes out below the exchange minimum is bumped up to the
    minimum, but only if the minimum still fits under `max_stake_usd` and the depth cap. For tiny bankrolls.
    """
    f = kelly_fraction(p, price, fee_per_share)
    if f <= 0 or bankroll <= 0:
        return SizeResult(0.0, 0.0, f, "no Kelly edge")
    cost = price + fee_per_share
    stake = kelly_mult * f * bankroll
    reason = "kelly"
    if stake > max_stake_usd:
        stake, reason = max_stake_usd, "max stake"
    shares = stake / cost
    depth_cap = depth_shares * max_depth_pct / 100.0
    if shares > depth_cap:
        shares, reason = depth_cap, "book depth"
    shares = floor_size(shares)
    if shares < min_order_size:
        fits = min_order_size * cost <= max_stake_usd + 1e-9 and min_order_size <= depth_cap + 1e-9
        if round_up_to_min and fits:
            return SizeResult(min_order_size, round(min_order_size * cost, 6), f, "rounded up to min order")
        return SizeResult(0.0, 0.0, f, f"size {shares:.2f} < min order {min_order_size:g}")
    return SizeResult(shares, round(shares * cost, 6), f, reason)
