"""Engine paths beyond the happy PAPER flow: LIVE via a fake exchange, manual track, commands, health, soak."""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from app import config as config_mod
from app.db.models import (
    AuditLog,
    Command,
    EquitySnapshot,
    LedgerEntry,
    Market,
    OrderIntent,
    Outbox,
    Position,
    Prediction,
    Signal,
    StationBias,
)
from app.forecast.observations import Obs
from app.polymarket.clob import FeeInfo
from app.polymarket.geoblock import GeoStatus
from app.services import books, control
from app.services.engine import Clients, Engine, compute_confidence, fmt_hours
from app.services.state import get_state, set_state
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
from tests.test_execution import FakeExchange


def make_engine(sessions, settings, *, live_client=None, data_api=None, nws=None, iem=None, om=None):
    d = tomorrow_local()
    clob = FakeClob(FeeInfo(0.25, 1.0))
    om = om or FakeOpenMeteo(
        {"ecmwf_ifs": ensemble("ecmwf_ifs", d, 75.0, 1.5, 51, seed=1), "gfs": ensemble("gfs", d, 75.2, 1.5, 31, seed=2)}
    )
    clients = Clients(
        gamma=FakeGamma([nyc_event("100", d)]),
        clob=clob,
        data_api=data_api or FakeDataApi(),
        openmeteo=om,
        iem=iem or FakeIem(),
        nws=nws or FakeNws(),
    )
    eng = Engine(sessions, settings, clients, live_client_factory=(lambda s: live_client) if live_client else None)
    clob.book_map["tok10003y"] = book("tok10003y", [(0.13, 500)], [(0.15, 500), (0.16, 300)], ts="a")
    return eng, clob


def go_live(sessions, monkeypatch):
    monkeypatch.setenv("LIVE_CONFIRM", "I_ACCEPT_LOSSES")
    monkeypatch.setenv("POLY_PRIVATE_KEY", "0x" + "1" * 64)
    config_mod.get_settings.cache_clear()
    s = config_mod.get_settings()
    with sessions() as db:
        set_state(db, "geoblocked", False)
        books.ensure_deposit(db, "LIVE", 300)
        assert control.switch_mode(db, "LIVE", source="UI", confirm_text="CONFIRM LIVE", settings=s).ok
        db.commit()
    return s


def test_live_entry_fill_exit_and_redeem(sessions, monkeypatch, env):
    s = go_live(sessions, monkeypatch)
    ex = FakeExchange()
    eng, clob = make_engine(sessions, s, live_client=ex)
    eng.startup()
    eng.sync_markets()
    eng.refresh_forecasts()
    assert eng.run_strategy()["orders"] == 1
    assert len(ex.posted) == 1
    oid = ex.posted[0]
    ex.match(oid, ex.orders[oid].original_size)
    eng.sync_orders()
    with sessions() as db:
        pos = db.scalar(select(Position).where(Position.mode == "LIVE"))
        assert pos is not None and pos.status == "OPEN"
    # TP exit posts a SELL; partially matched
    clob.book_map["tok10003y"] = book("tok10003y", [(0.30, 400)], [(0.32, 400)], ts="b")
    assert eng.run_exits() == 1
    sell = ex.posted[-1]
    ex.match(sell, ex.orders[sell].original_size)
    eng.sync_orders()
    with sessions() as db:
        assert db.scalar(select(Position).where(Position.mode == "LIVE")).status == "CLOSED"
    eng.process_commands()
    eng.reconcile_live()


def test_live_geoblocked_refuses_entries(sessions, monkeypatch, env):
    s = go_live(sessions, monkeypatch)
    ex = FakeExchange()
    eng, _ = make_engine(sessions, s, live_client=ex)
    eng.sync_markets()
    eng.refresh_forecasts()
    with sessions() as db:
        set_state(db, "geoblocked", True)
        db.commit()
    assert eng.run_strategy()["orders"] == 0 and ex.posted == []
    with sessions() as db:
        sig = db.scalar(select(Signal))
        assert "geoblocked" in sig.action


def test_resolution_redeems_live_winners(sessions, monkeypatch, env):
    s = go_live(sessions, monkeypatch)
    ex = FakeExchange()
    eng, _ = make_engine(sessions, s, live_client=ex)
    eng.sync_markets()
    eng.refresh_forecasts()
    eng.run_strategy()
    ex.match(ex.posted[0], ex.orders[ex.posted[0]].original_size)
    eng.sync_orders()
    eng.c.gamma = FakeGamma([], [nyc_event("100", tomorrow_local(), closed=True, winner=3)])
    eng.sync_markets()
    assert eng.process_resolutions() == 1
    with sessions() as db:
        pos = db.scalar(select(Position))
        assert pos.redeemed and pos.status == "RESOLVED"
        assert db.scalar(select(AuditLog).where(AuditLog.action == "REDEEM"))


def test_manual_track_took_exit_signal_and_import(sessions, settings):
    d = tomorrow_local()
    eng, clob = make_engine(sessions, settings)
    eng.startup()
    eng.sync_markets()
    eng.refresh_forecasts()
    eng.run_strategy()
    with sessions() as db:
        sig = db.scalar(select(Signal))
        assert control.took(db, sig.id, 0.15, 6.0, source="TELEGRAM", actor="1").ok
        db.commit()
    eng.process_commands()
    with sessions() as db:
        pos = db.scalar(select(Position).where(Position.track == "MANUAL"))
        assert pos.size == pytest.approx(round(6.0 / (0.15 + 0.25 * 0.15 * 0.85), 2))
    clob.book_map["tok10003y"] = book("tok10003y", [(0.30, 400)], [(0.32, 400)], ts="c")
    eng.run_exits()
    with sessions() as db:
        pos = db.scalar(select(Position).where(Position.track == "MANUAL"))
        assert pos.exit_signal == "TAKE_PROFIT" and pos.status == "OPEN"  # bot never trades manual positions
        assert any("SELL — market caught up" in o.text for o in db.scalars(select(Outbox)))
    # Wallet import adds a NO position on another bucket
    eng.settings = settings.model_copy(update={"MANUAL_WALLET_ADDRESS": "0x" + "a" * 40})
    eng.c.data_api = FakeDataApi(
        [
            {"conditionId": "0xcond10001", "asset": "tok10001n", "size": 12, "avgPrice": 0.8},
            {"conditionId": "0xunknown", "asset": "x", "size": 1, "avgPrice": 0.5},
        ]
    )
    assert eng.import_manual_wallet() == 1
    eng.c.data_api = FakeDataApi(
        [{"conditionId": "0xcond10001", "asset": "tok10001n", "size": 5, "avgPrice": 0.8, "curPrice": 0.85}]
    )
    assert eng.import_manual_wallet() == 1
    with sessions() as db:
        imp = db.scalar(select(Position).where(Position.external_ref == "tok10001n"))
        assert imp.size == 5 and imp.realized_micros > 0
    assert eng.close_position(imp.id) == "manual position marked closed"
    _ = d


def test_commands_and_close_paths(sessions, settings):
    eng, clob = make_engine(sessions, settings)
    with sessions() as db:
        control.switch_mode(db, "PAPER", source="UI")
        db.commit()
    eng.sync_markets()
    eng.refresh_forecasts()
    eng.run_strategy()
    with sessions() as db:
        pid = db.scalar(select(Position.id))
        control.close_all(db, source="UI")
        db.add(Command(kind="BOGUS", source="UI"))
        db.add(Command(kind="CLOSE_POSITION", payload={"position_id": 99999}, source="UI"))
        db.commit()
    assert eng.process_commands() == 3
    with sessions() as db:
        cmds = {c.kind: c for c in db.scalars(select(Command))}
        assert "SELL" in cmds["CLOSE_ALL"].result and cmds["BOGUS"].result.startswith("unknown")
        assert db.get(Position, pid).status == "CLOSED"
    assert eng.close_position(pid) == "position not open"
    assert eng.cancel_all("PAPER") == 0


def test_kill_file_and_heartbeat(sessions, settings):
    eng, _ = make_engine(sessions, settings)
    settings.kill_file.write_text("stop")
    eng.check_kill_file()
    with sessions() as db:
        assert get_state(db, "halted") and get_state(db, "halt_reason") == "KILL file present"
    settings.kill_file.unlink()
    eng.check_kill_file()
    eng.heartbeat("worker", mode="SCAN")
    with sessions() as db:
        assert get_state(db, "kill_file_seen") == "0"
        assert not control.stale_heartbeat(db)


def test_geoblock_job(sessions, settings, monkeypatch):
    eng, _ = make_engine(sessions, settings)
    seq = iter([GeoStatus(True, "US", datetime.now(UTC)), GeoStatus(False, "IE", datetime.now(UTC))])
    monkeypatch.setattr("app.services.engine.check_geoblock", lambda url: next(seq))
    assert eng.check_geoblock() is True
    assert eng.check_geoblock() is False
    with sessions() as db:
        texts = [o.text for o in db.scalars(select(Outbox))]
        assert any("BLOCKED" in t for t in texts) and any("cleared" in t for t in texts)


def test_refit_bias_against_observations(sessions, settings):
    d0 = date(2026, 1, 1)

    class Iem2:
        def observations(self, st, start, end, source):
            out = []
            for i in range((end - start).days + 1):
                day = start + timedelta(days=i)
                for h in range(24):
                    out.append(Obs(datetime(day.year, day.month, day.day, h, 51), 60.0 + h / 2))
            return out

    class Om2(FakeOpenMeteo):
        def previous_runs_daily(self, lat, lon, tz, family, unit, variable, start, end, leads=range(1, 8)):
            days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
            v = 72.5 if variable == "max" else 61.0  # obs max = 72 (71.5 rounds up) / min = 60
            return {n: {d: v for d in days} for n in leads}

    eng, _ = make_engine(sessions, settings, iem=Iem2(), om=Om2({}))
    eng.sync_markets()
    n = eng.refit_bias(days=60)
    assert n == 2 * 2 * 7
    with sessions() as db:
        b = db.scalar(select(StationBias).where(StationBias.variable == "max", StationBias.lead_day == 1))
        assert b.mu > 0 and b.n >= 30 and b.unit == "F"
        assert get_state(db, "last_bias_fit")
    _ = d0


def test_snapshot_equity_and_drawdown_warning(sessions, settings):
    eng, _ = make_engine(sessions, settings)
    with sessions() as db:
        control.switch_mode(db, "PAPER", source="UI")
        db.commit()
    eng.sync_markets()
    eng.refresh_forecasts()
    eng.run_strategy()
    with sessions() as db:
        set_state(db, "equity_peak:PAPER", 250.0)  # pretend we were up: now ~20% below peak
        db.commit()
    eng.snapshot_equity()
    with sessions() as db:
        assert db.scalar(select(EquitySnapshot).where(EquitySnapshot.book == "PAPER"))
        assert any("drawdown" in o.text for o in db.scalars(select(Outbox)))
    # The next strategy run sees the breach and halts.
    eng.run_strategy()
    with sessions() as db:
        assert get_state(db, "halted") is True


def test_auto_disable_city_on_bad_calibration(sessions, settings):
    eng, _ = make_engine(sessions, settings)
    eng.sync_markets()
    with sessions() as db:
        m = db.scalar(select(Market))
        for _ in range(25):
            db.add(Prediction(market_id=m.id, city="nyc", lead_hours=30, p_model=0.95, outcome=0))
        db.commit()
    eng.process_resolutions()
    with sessions() as db:
        assert "nyc" in get_state(db, "disabled_cities")
    eng.refresh_forecasts()
    eng.run_strategy()
    with sessions() as db:
        assert db.scalar(select(Signal).where(Signal.status == "ACTIVE")) is None


def test_nws_conflict_lowers_confidence():
    assert compute_confidence({"a": 0.3, "b": 0.31}, 0.3, 0.1, 0.2, False).value == "HIGH"
    assert compute_confidence({"a": 0.3, "b": 0.31}, 0.3, None, 0.2, False).value == "MED"
    assert compute_confidence({"a": 0.3, "b": 0.31}, 0.3, 0.1, 0.2, True).value == "LOW"
    assert compute_confidence({"a": 0.3}, 0.3, 0.1, 0.2, False).value == "LOW"
    assert compute_confidence({"a": 0.1, "b": 0.5}, 0.3, 0.1, 0.2, False).value == "LOW"
    assert fmt_hours(-1) == "closed" and fmt_hours(25.5) == "1d 1h" and fmt_hours(1.5) == "1h 30m"


def test_paper_24h_soak_reconciles_to_the_cent(sessions, settings):
    """288 five-minute cycles of moving books and forecasts: no errors, books reconcile to the micro-dollar."""
    rng = random.Random(42)
    d = tomorrow_local()
    eng, clob = make_engine(sessions, settings)
    with sessions() as db:
        control.switch_mode(db, "PAPER", source="UI")
        db.commit()
    eng.startup()
    eng.sync_markets()
    fair = [0.02, 0.05, 0.20, 0.38, 0.24, 0.08, 0.03]  # roughly what the model believes per bucket
    mids = [rng.uniform(0.03, 0.5) for _ in fair]
    for cycle in range(288):
        for i in range(7):
            if cycle < 144:  # first half: a noisy market that often misprices buckets
                mids[i] = min(0.95, max(0.02, mids[i] + rng.gauss(0, 0.02)))
            else:  # second half: the market converges on fair value (take-profits should fire)
                mids[i] += 0.15 * (fair[i] - mids[i]) + rng.gauss(0, 0.005)
            m = round(min(0.97, max(0.02, mids[i])), 2)
            ty, tn = f"tok100{i:02d}y", f"tok100{i:02d}n"
            clob.book_map[ty] = book(
                ty,
                [(max(0.01, m - 0.01), rng.uniform(100, 600))],
                [(min(0.99, m + 0.01), rng.uniform(100, 600))],
                ts=str(cycle),
            )
            n = round(1 - m, 2)
            clob.book_map[tn] = book(
                tn,
                [(max(0.01, n - 0.01), rng.uniform(100, 600))],
                [(min(0.99, n + 0.01), rng.uniform(100, 600))],
                ts=str(cycle),
            )
        if cycle % 3 == 0:
            c = 75 + rng.gauss(0, 1.0)
            eng.c.openmeteo = FakeOpenMeteo(
                {
                    "ecmwf_ifs": ensemble("ecmwf_ifs", d, c, 1.5, 51, seed=cycle),
                    "gfs": ensemble("gfs", d, c + 0.3, 1.5, 31, seed=cycle + 1),
                }
            )
            eng.refresh_forecasts()
        eng.run_strategy()
        eng.sync_orders()
        if cycle % 6 == 0:
            eng.run_exits()
        if cycle % 12 == 0:
            eng.snapshot_equity()
        eng.process_commands()
    with sessions() as db:
        entries = db.scalars(select(LedgerEntry).where(LedgerEntry.book == "PAPER")).all()
        cash = books.cash(db, "PAPER")
        assert cash == sum(e.amount_micros for e in entries)
        positions = db.scalars(select(Position).where(Position.mode == "PAPER")).all()
        realized = sum(p.realized_micros for p in positions)
        open_cost = sum(p.cost_micros for p in positions if p.status == "OPEN")
        # cash + remaining cost basis = deposits + realised P&L, exactly (to the micro-dollar)
        assert cash + open_cost == books.deposits(db, "PAPER") + realized
        assert sum(e.pnl_micros for e in entries) == realized
        intents = db.scalars(select(OrderIntent)).all()
        sells = [i for i in intents if i.side == "SELL"]
        closed = [p for p in positions if p.status == "CLOSED"]
        print(
            f"\nsoak: {len(intents)} intents, {len(sells)} sells, {len(positions)} positions "
            f"({len(closed)} closed), realised ${books.usd(realized):+.2f}, cash ${books.usd(cash):.2f}"
        )
        assert intents and sells and closed  # the soak must actually trade and exit
        errors = [o.text for o in db.scalars(select(Outbox)) if "failed" in o.text.lower()]
        assert not errors
