"""End to end: Gamma -> parser -> forecasts -> model -> signal -> PAPER order -> fill -> exit -> resolution.

Runs entirely on synthetic fixtures (see tests/fixtures/README.md) and checks the books to the micro-dollar.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import LedgerEntry, OrderIntent, Position, Signal
from app.polymarket.clob import FeeInfo
from app.services import books, control
from app.services.engine import Clients, Engine
from app.services.state import get_state
from tests.fixtures.weather import (
    FakeClob,
    FakeDataApi,
    FakeGamma,
    FakeIem,
    FakeNws,
    FakeOpenMeteo,
    book,
    ensemble,
    nyc_event,
    tomorrow_local,
)

pytestmark = pytest.mark.integration


def build(sessions, settings, fee=FeeInfo(0.25, 1.0)):
    d = tomorrow_local()
    ev = nyc_event("100", d)
    clob = FakeClob(fee)
    om = FakeOpenMeteo(
        {"ecmwf_ifs": ensemble("ecmwf_ifs", d, 75.0, 1.5, 51, seed=1), "gfs": ensemble("gfs", d, 75.2, 1.5, 31, seed=2)}
    )
    clients = Clients(
        gamma=FakeGamma([ev]), clob=clob, data_api=FakeDataApi(), openmeteo=om, iem=FakeIem(), nws=FakeNws()
    )
    eng = Engine(sessions, settings, clients)
    # 74-75°F bucket is index 3 (69-, 70-71, 72-73, 74-75 ...)
    tok_y, tok_n = "tok10003y", "tok10003n"
    clob.book_map[tok_y] = book(tok_y, bids=[(0.13, 500)], asks=[(0.15, 500), (0.16, 300)], ts="a")
    return eng, clob, ev, tok_y, tok_n


def test_scan_produces_signal_without_orders(sessions, settings):
    eng, clob, ev, tok_y, _ = build(sessions, settings)
    assert eng.sync_markets() == (1, 0)
    assert eng.refresh_forecasts() > 0
    stats = eng.run_strategy()
    assert stats["signals"] >= 1 and stats["orders"] == 0
    with sessions() as s:
        sig = s.scalar(select(Signal).where(Signal.status == "ACTIVE"))
        assert sig is not None and sig.outcome == "YES" and sig.market.bucket_label == "74-75°F"
        assert sig.edge >= settings.MIN_EDGE
        assert 0.15 <= sig.max_limit_price < sig.p_model
        assert 0 < sig.suggested_stake_usd <= settings.MANUAL_MAX_STAKE_USD
        assert sig.action == "signal only (SCAN)"
        assert s.scalar(select(OrderIntent.id)) is None
        texts = [o.text for o in s.scalars(select(__import__("app.db.models", fromlist=["Outbox"]).Outbox))]
        assert any("BUY YES" in t and "polymarket.com/event/" in t for t in texts)


def test_paper_round_trip_reconciles_to_the_micro(sessions, settings):
    eng, clob, ev, tok_y, _ = build(sessions, settings)
    with sessions() as s:
        assert control.switch_mode(s, "PAPER", source="UI").ok
        s.commit()
    eng.sync_markets()
    eng.refresh_forecasts()
    stats = eng.run_strategy()
    assert stats["orders"] == 1

    with sessions() as s:
        it = s.scalar(select(OrderIntent))
        assert it.status == "FILLED" and it.side == "BUY" and it.price == pytest.approx(0.16)
        pos = s.scalar(select(Position).where(Position.status == "OPEN"))
        assert pos.size == it.size
        fee = 0.25 * 0.15 * 0.85
        expected_cost = books.to_micros(0.15 * pos.size) + books.to_micros(fee * pos.size)
        assert pos.cost_micros == expected_cost
        assert books.cash(s, "PAPER") == books.to_micros(settings.BANKROLL_USD) - expected_cost

    # Running the strategy again must not double-enter.
    assert eng.run_strategy()["orders"] == 0

    # Market catches up: bid 0.30 >= p_model - TP_BUFFER -> take profit.
    clob.book_map[tok_y] = book(tok_y, bids=[(0.30, 400)], asks=[(0.32, 400)], ts="b")
    assert eng.run_exits() == 1
    with sessions() as s:
        pos = s.scalar(select(Position))
        assert pos.status == "CLOSED" and "TAKE_PROFIT" in pos.exit_reason
        buy_fee = (
            books.to_micros(0.25 * 0.15 * 0.85 * pos_size)
            if (
                pos_size := sum(e.size for e in s.scalars(select(__import__("app.db.models", fromlist=["Fill"]).Fill)))
                / 2
            )
            else 0
        )
        cash = books.cash(s, "PAPER")
        entries = s.scalars(select(LedgerEntry).where(LedgerEntry.book == "PAPER")).all()
        assert cash == sum(e.amount_micros for e in entries)
        snap = books.snapshot(s, "PAPER")
        # Everything closed: equity == deposits + realised, exactly.
        assert books.to_micros(snap.equity) == books.to_micros(snap.deposits) + pos.realized_micros
        assert pos.realized_micros > 0
        # realised = proceeds - sell fee - (buy cost + buy fee), each rounded to the micro once
        size = pos_size
        expected = (
            books.to_micros(0.30 * size)
            - books.to_micros(0.25 * 0.30 * 0.70 * size)
            - books.to_micros(0.15 * size)
            - buy_fee
        )
        assert pos.realized_micros == expected


def test_resolution_pays_out_and_scores(sessions, settings):
    eng, clob, ev, tok_y, _ = build(sessions, settings)
    with sessions() as s:
        control.switch_mode(s, "PAPER", source="UI")
        s.commit()
    eng.sync_markets()
    eng.refresh_forecasts()
    eng.run_strategy()
    # Resolve the event with the 74-75 bucket winning.
    closed = nyc_event("100", tomorrow_local(), closed=True, winner=3)
    eng.c.gamma = FakeGamma([], [closed])
    eng.sync_markets()
    assert eng.process_resolutions() == 1
    with sessions() as s:
        pos = s.scalar(select(Position))
        assert pos.status == "RESOLVED"
        paid = -sum(
            e.amount_micros
            for e in s.scalars(
                select(LedgerEntry).where(LedgerEntry.position_id == pos.id, LedgerEntry.kind.in_(("BUY", "FEE")))
            )
        )
        assert pos.realized_micros == books.to_micros(pos.size) - paid
        snap = books.snapshot(s, "PAPER")
        assert books.to_micros(snap.equity) == books.to_micros(snap.deposits) + pos.realized_micros
        sig = s.scalar(select(Signal))
        assert sig.won is True and sig.status == "RESOLVED" and sig.roi_at_limit > 0


def test_stale_forecast_blocks_trading(sessions, settings, monkeypatch):
    eng, clob, ev, tok_y, _ = build(sessions, settings)
    eng.sync_markets()
    eng.refresh_forecasts()
    from app.db.models import ForecastSnapshot

    with sessions() as s:
        for snap in s.scalars(select(ForecastSnapshot)):
            snap.fetched_at = snap.fetched_at.replace(year=2020)
        s.commit()
    eng.run_strategy()
    with sessions() as s:
        assert s.scalar(select(Signal).where(Signal.status == "ACTIVE")) is None


def test_unknown_fee_blocks_trading(sessions, settings):
    eng, clob, ev, tok_y, _ = build(sessions, settings, fee=None)
    # feesEnabled=True in the fixture and no fee data from the CLOB -> unknown -> never traded
    eng.sync_markets()
    eng.refresh_forecasts()
    assert eng.run_strategy()["signals"] == 0


def test_kill_switch_halts_and_cancels(sessions, settings):
    eng, clob, ev, tok_y, _ = build(sessions, settings)
    with sessions() as s:
        control.switch_mode(s, "PAPER", source="UI")
        s.commit()
    # A resting order that cannot fill
    clob.book_map[tok_y] = book(tok_y, bids=[(0.13, 500)], asks=[(0.15, 500)], ts="z")
    eng.sync_markets()
    eng.refresh_forecasts()
    with sessions() as s:
        assert control.kill(s, source="UI", settings=settings).ok
        s.commit()
    assert settings.kill_file.exists()
    eng.process_commands()
    eng.check_kill_file()
    assert eng.run_strategy()["orders"] == 0
    with sessions() as s:
        assert get_state(s, "halted") is True
        assert control.resume(s, source="UI", settings=settings).ok
        s.commit()
    assert not settings.kill_file.exists()
