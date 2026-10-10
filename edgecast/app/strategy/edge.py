"""Edge after fees, max limit price, and the pre-trade market-quality filters."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

EPS = 1e-9


@dataclass(frozen=True)
class Level:
    price: float
    size: float


@dataclass(frozen=True)
class Book:
    """One outcome token's book. bids best-first (descending), asks best-first (ascending)."""

    bids: Sequence[Level]
    asks: Sequence[Level]

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    def ask_depth(self, max_price: float) -> float:
        return sum(lv.size for lv in self.asks if lv.price <= max_price + EPS)

    def ask_notional(self, max_price: float) -> float:
        return sum(lv.size * lv.price for lv in self.asks if lv.price <= max_price + EPS)

    def bid_depth(self, min_price: float) -> float:
        return sum(lv.size for lv in self.bids if lv.price >= min_price - EPS)


@dataclass(frozen=True)
class FeeModel:
    rate: float
    exponent: float

    def per_share(self, price: float) -> float:
        if self.rate <= 0:
            return 0.0
        return float(self.rate * (price * (1.0 - price)) ** self.exponent)


@dataclass(frozen=True)
class EdgeParams:
    min_edge: float
    sanity_max_edge: float
    max_spread: float  # in price units (cents / 100)
    max_slippage: float  # in price units
    min_liquidity_usd: float
    tick: float


@dataclass(frozen=True)
class Opportunity:
    outcome: str  # YES | NO
    p: float  # model probability of this outcome
    ask: float | None
    bid: float | None
    fee_at_ask: float
    edge: float  # at the best ask, after fees
    max_limit_price: float | None  # highest tick that still clears min_edge
    limit_price: float | None  # what we would actually post (<= max_limit, <= ask + slippage)
    depth_shares: float  # visible shares at or below limit_price
    liquidity_usd: float  # visible notional at or below limit_price
    skip_reason: str  # "" when tradeable

    @property
    def tradeable(self) -> bool:
        return self.skip_reason == ""


def floor_tick(price: float, tick: float) -> float:
    return round(math.floor(price / tick + EPS) * tick, 10)


def ceil_tick(price: float, tick: float) -> float:
    return round(math.ceil(price / tick - EPS) * tick, 10)


def edge_at(p: float, price: float, fee: FeeModel) -> float:
    return p - price - fee.per_share(price)


def max_limit_price(p: float, min_edge: float, tick: float, fee: FeeModel) -> float | None:
    """Largest tick price x in (0, 1) with p - x - fee(x) >= min_edge."""
    x = floor_tick(min(p, 1.0 - tick), tick)
    while x >= tick - EPS:
        if edge_at(p, x, fee) >= min_edge - EPS:
            return x
        x = round(x - tick, 10)
    return None


def evaluate(p: float, outcome: str, book: Book, fee: FeeModel, params: EdgeParams) -> Opportunity:
    ask, bid = book.best_ask, book.best_bid
    mlp = max_limit_price(p, params.min_edge, params.tick, fee)

    def result(edge: float, limit: float | None, reason: str) -> Opportunity:
        depth = book.ask_depth(limit) if limit is not None else 0.0
        liq = book.ask_notional(limit) if limit is not None else 0.0
        fee_ask = fee.per_share(ask) if ask is not None else 0.0
        return Opportunity(outcome, p, ask, bid, fee_ask, edge, mlp, limit, depth, liq, reason)

    if ask is None:
        return result(-1.0, None, "no asks")
    edge = edge_at(p, ask, fee)
    if bid is None:
        return result(edge, None, "no bids (spread undefined)")
    if ask - bid > params.max_spread + EPS:
        return result(edge, None, f"spread {100 * (ask - bid):.0f}c > max")
    if edge > params.sanity_max_edge:
        return result(edge, None, f"edge {edge:.2f} > sanity max (possible parsing bug)")
    if edge < params.min_edge - EPS or mlp is None or mlp < ask - EPS:
        return result(edge, None, f"edge {100 * edge:.1f}% < min")
    limit = floor_tick(min(mlp, ask + params.max_slippage), params.tick)
    opp = result(edge, limit, "")
    if opp.liquidity_usd < params.min_liquidity_usd:
        return result(edge, limit, f"liquidity ${opp.liquidity_usd:.0f} < min")
    return opp


def family_agreement(p_by_family: Mapping[str, float], ask: float, fee: FeeModel, min_edge: float) -> bool:
    """True only if at least two families exist and each alone shows >= half the minimum edge."""
    if len(p_by_family) < 2:
        return False
    return all(edge_at(p, ask, fee) >= min_edge / 2 for p in p_by_family.values())


def best_opportunity(
    p_yes: float,
    book_yes: Book,
    book_no: Book,
    fee: FeeModel,
    params: EdgeParams,
) -> tuple[Opportunity, Opportunity]:
    """Evaluate BUY YES and BUY NO on one bucket; return (best, other)."""
    yes = evaluate(p_yes, "YES", book_yes, fee, params)
    no = evaluate(1.0 - p_yes, "NO", book_no, fee, params)
    ranked = sorted((yes, no), key=lambda o: (o.tradeable, o.edge), reverse=True)
    return ranked[0], ranked[1]
