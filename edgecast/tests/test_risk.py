"""risk.py: caps can never be exceeded, whatever the order sequence."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from app.risk import OrderRequest, RiskLimits, RiskState, breach, check, drawdown_fraction_of_limit

L = RiskLimits(
    max_stake_per_order=10,
    max_exposure_per_market=20,
    max_exposure_per_city_day=30,
    max_total_exposure=100,
    max_daily_loss=20,
    max_drawdown_pct=15,
)

order = st.tuples(
    st.sampled_from(["m1", "m2", "m3", "m4", "m5", "m6"]),
    st.sampled_from(["nyc|d1", "nyc|d2", "lon|d1"]),
    st.floats(0.01, 0.99),
    st.floats(0.01, 500),
    st.floats(0, 0.05),
)


@settings(max_examples=300, deadline=None)
@given(st.lists(order, min_size=1, max_size=80))
def test_caps_never_exceeded(seq):
    by_m: dict[str, float] = {}
    by_c: dict[str, float] = {}
    total = 0.0
    for m, c, price, size, fee in seq:
        state = RiskState(
            exposure_by_market=dict(by_m),
            exposure_by_city_day=dict(by_c),
            total_exposure=total,
            equity=200,
            equity_peak=200,
        )
        d = check(OrderRequest(m, c, "BUY", price, size, fee, min_size=0.01), state, L)
        if d.approved:
            cost = d.size * (price + fee)
            assert d.size <= size + 1e-9
            assert cost <= L.max_stake_per_order + 1e-6
            by_m[m] = by_m.get(m, 0) + cost
            by_c[c] = by_c.get(c, 0) + cost
            total += cost
        assert by_m.get(m, 0) <= L.max_exposure_per_market + 1e-6
        assert by_c.get(c, 0) <= L.max_exposure_per_city_day + 1e-6
        assert total <= L.max_total_exposure + 1e-6


def test_gates():
    req = OrderRequest("m", "c", "BUY", 0.5, 4, 0.0)
    assert not check(req, RiskState(halted=True), L).approved
    assert not check(req, RiskState(paused=True), L).approved
    assert not check(req, RiskState(geoblocked=True), L).approved
    assert "breach" in check(req, RiskState(realized_today=-25), L).reason
    sell = OrderRequest("m", "c", "SELL", 0.5, 4, 0.0)
    assert check(sell, RiskState(halted=True, paused=True), L).approved  # exits allowed when halted
    assert not check(sell, RiskState(geoblocked=True), L).approved
    assert not check(OrderRequest("m", "c", "BUY", 1.2, 4, 0.0), RiskState(), L).approved
    clipped = check(OrderRequest("m", "c", "BUY", 0.5, 100, 0.0), RiskState(), L)
    assert clipped.approved and clipped.size == 20 and "max stake per order" in clipped.clipped_by
    full = check(OrderRequest("m", "c", "BUY", 0.5, 10, 0.0, min_size=5), RiskState(exposure_by_market={"m": 19}), L)
    assert not full.approved and "market" in full.reason


def test_breach_and_drawdown_fraction():
    assert breach(RiskState(equity=170, equity_peak=200), L) is not None
    assert breach(RiskState(equity=190, equity_peak=200), L) is None
    assert breach(RiskState(realized_today=-20), L) is not None
    assert drawdown_fraction_of_limit(RiskState(realized_today=-10, equity=200, equity_peak=200), L) == 0.5
    assert drawdown_fraction_of_limit(RiskState(equity=176, equity_peak=200), L) == 0.8
