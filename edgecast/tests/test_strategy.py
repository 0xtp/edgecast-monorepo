from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.strategy.edge import (
    Book,
    EdgeParams,
    FeeModel,
    Level,
    best_opportunity,
    ceil_tick,
    edge_at,
    evaluate,
    family_agreement,
    floor_tick,
    max_limit_price,
)
from app.strategy.exits import ExitKind, decide_exit
from app.strategy.sizing import floor_size, kelly_fraction, size_order

P = EdgeParams(min_edge=0.06, sanity_max_edge=0.4, max_spread=0.06, max_slippage=0.01, min_liquidity_usd=50, tick=0.01)
FEE = FeeModel(0.25, 1.0)
ZERO = FeeModel(0.0, 1.0)


def bk(bids, asks):
    return Book([Level(p, s) for p, s in bids], [Level(p, s) for p, s in asks])


def test_fee_formula_matches_sdk():
    assert FEE.per_share(0.5) == pytest.approx(0.25 * 0.25)
    assert FeeModel(0.1, 2).per_share(0.2) == pytest.approx(0.1 * 0.16**2)
    assert ZERO.per_share(0.4) == 0


def test_edge_and_max_limit():
    assert edge_at(0.40, 0.30, FEE) == pytest.approx(0.40 - 0.30 - 0.25 * 0.21)
    x = max_limit_price(0.40, 0.06, 0.01, FEE)
    assert x is not None and edge_at(0.40, x, FEE) >= 0.06 - 1e-9
    assert edge_at(0.40, x + 0.01, FEE) < 0.06
    assert max_limit_price(0.05, 0.06, 0.01, ZERO) is None
    assert floor_tick(0.239, 0.01) == 0.23 and ceil_tick(0.231, 0.01) == 0.24


@given(p=st.floats(0.01, 0.99), rate=st.floats(0, 0.5), e=st.floats(0.5, 2))
def test_max_limit_is_the_highest_qualifying_tick(p, rate, e):
    fee = FeeModel(rate, e)
    x = max_limit_price(p, 0.06, 0.01, fee)
    if x is None:
        assert edge_at(p, 0.01, fee) < 0.06 + 1e-9
    else:
        assert edge_at(p, x, fee) >= 0.06 - 1e-9
        assert x + 0.01 >= 1 - 1e-9 or edge_at(p, x + 0.01, fee) < 0.06 + 1e-9


def test_evaluate_tradeable():
    o = evaluate(0.30, "YES", bk([(0.13, 500)], [(0.15, 500), (0.16, 300)]), ZERO, P)
    assert o.tradeable and o.limit_price == pytest.approx(0.16) and o.depth_shares == 800
    assert o.edge == pytest.approx(0.15)


@pytest.mark.parametrize(
    ("p", "bids", "asks", "reason"),
    [
        (0.3, [(0.1, 10)], [], "no asks"),
        (0.3, [], [(0.15, 500)], "no bids"),
        (0.3, [(0.05, 500)], [(0.15, 500)], "spread"),
        (0.9, [(0.13, 500)], [(0.15, 500)], "sanity"),
        (0.18, [(0.13, 500)], [(0.15, 500)], "< min"),
        (0.30, [(0.13, 500)], [(0.15, 10)], "liquidity"),
    ],
)
def test_evaluate_skip_reasons(p, bids, asks, reason):
    o = evaluate(p, "YES", bk(bids, asks), ZERO, P)
    assert not o.tradeable and reason in o.skip_reason


def test_best_opportunity_prefers_tradeable_side():
    yes = bk([(0.70, 500)], [(0.72, 500)])
    no = bk([(0.20, 500)], [(0.22, 500)])
    best, other = best_opportunity(0.60, yes, no, ZERO, P)  # NO prob 0.40 vs ask 0.22 -> edge 0.18
    assert best.outcome == "NO" and best.tradeable and not other.tradeable


def test_family_agreement():
    assert family_agreement({"a": 0.30, "b": 0.28}, 0.15, ZERO, 0.06)
    assert not family_agreement({"a": 0.30}, 0.15, ZERO, 0.06)
    assert not family_agreement({"a": 0.40, "b": 0.16}, 0.15, ZERO, 0.06)


def test_kelly_and_sizing():
    assert kelly_fraction(0.5, 0.4, 0.0) == pytest.approx(1 / 6)
    assert kelly_fraction(0.3, 0.4, 0.0) == 0 and kelly_fraction(0.9, 1.0, 0.0) == 0
    r = size_order(
        p=0.3,
        price=0.15,
        fee_per_share=0.0,
        bankroll=200,
        kelly_mult=0.15,
        max_stake_usd=10,
        depth_shares=1000,
        max_depth_pct=25,
        min_order_size=5,
    )
    assert r.shares == pytest.approx(floor_size(0.15 * (0.15 / 0.85) * 200 / 0.15)) and r.reason == "kelly"
    capped = size_order(
        p=0.6,
        price=0.15,
        fee_per_share=0.0,
        bankroll=10_000,
        kelly_mult=0.5,
        max_stake_usd=10,
        depth_shares=1e6,
        max_depth_pct=25,
        min_order_size=5,
    )
    assert capped.stake_usd <= 10 + 1e-9 and capped.reason == "max stake"
    thin = size_order(
        p=0.6,
        price=0.15,
        fee_per_share=0.0,
        bankroll=10_000,
        kelly_mult=0.5,
        max_stake_usd=100,
        depth_shares=40,
        max_depth_pct=25,
        min_order_size=5,
    )
    assert thin.shares == 10 and thin.reason == "book depth"
    tiny = size_order(
        p=0.3,
        price=0.15,
        fee_per_share=0.0,
        bankroll=5,
        kelly_mult=0.15,
        max_stake_usd=10,
        depth_shares=1000,
        max_depth_pct=25,
        min_order_size=5,
    )
    assert tiny.shares == 0 and "min order" in tiny.reason
    none = size_order(
        p=0.1,
        price=0.15,
        fee_per_share=0.0,
        bankroll=5,
        kelly_mult=0.15,
        max_stake_usd=10,
        depth_shares=1000,
        max_depth_pct=25,
        min_order_size=5,
    )
    assert none.shares == 0


def test_exits():
    assert (
        decide_exit(p_model=0.3, best_bid=None, tp_buffer=0.02, exit_edge=0.03, new_forecast=True).kind == ExitKind.HOLD
    )
    assert (
        decide_exit(p_model=0.3, best_bid=0.29, tp_buffer=0.02, exit_edge=0.03, new_forecast=False).kind
        == ExitKind.TAKE_PROFIT
    )
    flip = decide_exit(p_model=0.1, best_bid=0.2, tp_buffer=0.02, exit_edge=0.03, new_forecast=True)
    assert flip.kind == ExitKind.EDGE_REVERSAL and "flipped" in flip.reason
    # Without a new run the same numbers read as "market caught up"
    assert (
        decide_exit(p_model=0.1, best_bid=0.2, tp_buffer=0.02, exit_edge=0.03, new_forecast=False).kind
        == ExitKind.TAKE_PROFIT
    )
    assert (
        decide_exit(p_model=0.4, best_bid=0.2, tp_buffer=0.02, exit_edge=0.03, new_forecast=True).kind == ExitKind.HOLD
    )
