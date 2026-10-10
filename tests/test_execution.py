"""Execution: LIVE executor + reconciliation against a fake exchange, PAPER fill model, accounting invariants."""

from __future__ import annotations

import contextlib
import itertools
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import select

from app.db.models import Fill, LedgerEntry, Market, OrderIntent, Position
from app.execution.base import place_order
from app.execution.live import ExOrder, ExTrade, LiveExecutor, load_creds, save_creds
from app.execution.paper import PaperExecutor
from app.execution.reconcile import reconcile
from app.markets import registry
from app.services import books
from tests.fixtures.weather import book, nyc_event, tomorrow_local


class Crash(Exception):
    pass


@dataclass
class FakeExchange:
    """Minimal CLOB: records every post; orders rest until `match` is called."""

    crash_before_post: bool = False
    crash_after_post: bool = False
    reject: bool = False

    def __post_init__(self) -> None:
        self.orders: dict[str, ExOrder] = {}
        self.posted: list[str] = []
        self.trades_list: list[ExTrade] = []
        self.cancelled: list[str] = []
        self._ids = itertools.count(1)

    def sign_limit(self, token_id: str, price: float, size: float, side: str) -> tuple[Any, str]:
        return {"token_id": token_id, "price": price, "size": size, "side": side}, f"salt{next(self._ids)}"

    def post(self, signed: Any) -> dict[str, Any]:
        if self.crash_before_post:
            raise Crash("network down before post")
        if self.reject:
            return {"ok": False, "error": "not_enough_balance: insufficient"}
        oid = f"0xorder{len(self.posted) + 1}"
        self.posted.append(oid)
        self.orders[oid] = ExOrder(
            oid, signed["token_id"], signed["side"], signed["price"], signed["size"], 0.0, datetime.now(UTC)
        )
        if self.crash_after_post:
            raise Crash("connection reset after post")
        return {"ok": True, "order_id": oid, "status": "live"}

    def match(self, oid: str, size: float, maker: bool = False) -> None:
        o = self.orders[oid]
        o.size_matched += size
        leg = (oid, o.price, size)
        self.trades_list.append(
            ExTrade(f"t{len(self.trades_list) + 1}", o.token_id, "MATCHED", datetime.now(UTC), [leg], o.side)
        )
        if o.size_matched >= o.original_size - 1e-9:
            del self.orders[oid]

    def open_orders(self) -> list[ExOrder]:
        return list(self.orders.values())

    def trades(self, after: datetime | None) -> list[ExTrade]:
        return list(self.trades_list)

    def cancel(self, order_id: str) -> None:
        self.cancelled.append(order_id)
        self.orders.pop(order_id, None)

    def cancel_all(self) -> None:
        for oid in list(self.orders):
            self.cancel(oid)

    def collateral_balance(self) -> float:
        return 1000.0

    def redeem(self, condition_id: str) -> str:
        return "0xtx"


@pytest.fixture
def market(sessions):
    with sessions() as s:
        registry.sync(s, [nyc_event("100", tomorrow_local())])
        m = s.get(Market, "0xcond10003")
        m.fee_rate, m.fee_exponent = 0.25, 1.0
        s.commit()
    with sessions() as s:
        return s.get(Market, "0xcond10003")


def wipe(sessions) -> None:
    with sessions() as s:
        for model in (Fill, OrderIntent, LedgerEntry, Position):
            s.query(model).delete()
        s.commit()


# ------------------------------------------------------------------ LIVE executor
def test_live_happy_path_fill_and_cancel(sessions, market):
    ex = FakeExchange()
    live = LiveExecutor(ex)
    iid = place_order(sessions, live, market=market, outcome="YES", side="BUY", price=0.2, size=10, reason="t")
    with sessions() as s:
        it = s.get(OrderIntent, iid)
        assert it.status == "OPEN" and it.exchange_order_id == "0xorder1" and it.signed_salt
    ex.match("0xorder1", 4)
    with sessions() as s:
        assert live.sync(s) == 1
        s.commit()
        it = s.get(OrderIntent, iid)
        assert it.status == "PARTIAL" and it.filled_size == 4
        assert live.sync(s) == 0  # idempotent on trade id
        live.cancel_all(s)
        s.commit()
        assert s.get(OrderIntent, iid).status == "CANCELLED"
        pos = s.scalar(select(Position))
        assert pos.size == 4 and pos.mode == "LIVE"


def test_live_rejected_and_signing_failure(sessions, market):
    ex = FakeExchange(reject=True)
    iid = place_order(
        sessions, LiveExecutor(ex), market=market, outcome="YES", side="BUY", price=0.2, size=10, reason="t"
    )
    with sessions() as s:
        it = s.get(OrderIntent, iid)
        assert it.status == "REJECTED" and "not_enough_balance" in it.error

    class BadSigner(FakeExchange):
        def sign_limit(self, *a: Any) -> tuple[Any, str]:
            raise ValueError("bad key")

    iid2 = place_order(
        sessions, LiveExecutor(BadSigner()), market=market, outcome="YES", side="BUY", price=0.2, size=10, reason="t"
    )
    with sessions() as s:
        assert s.get(OrderIntent, iid2).status == "FAILED"


def test_unknown_post_outcome_stays_pending_then_reconcile_adopts(sessions, market):
    ex = FakeExchange(crash_after_post=True)
    iid = place_order(
        sessions, LiveExecutor(ex), market=market, outcome="YES", side="BUY", price=0.2, size=10, reason="t"
    )
    with sessions() as s:
        it = s.get(OrderIntent, iid)
        assert it.status == "PENDING" and it.exchange_order_id is None
    ex.crash_after_post = False
    with sessions() as s:
        rep = reconcile(s, ex)
        s.commit()
        assert rep.adopted == [iid] and not rep.cancelled_unknown
        assert s.get(OrderIntent, iid).exchange_order_id == "0xorder1"
    assert len(ex.posted) == 1  # never re-sent


def test_pending_never_sent_is_failed_not_resent(sessions, market):
    ex = FakeExchange(crash_before_post=True)
    iid = place_order(
        sessions, LiveExecutor(ex), market=market, outcome="YES", side="BUY", price=0.2, size=10, reason="t"
    )
    with sessions() as s:
        rep = reconcile(s, ex, now=datetime.now(UTC) + timedelta(minutes=5))
        s.commit()
        assert rep.failed == [iid] and s.get(OrderIntent, iid).status == "FAILED"
    assert ex.posted == []


def test_adopt_from_trades_when_already_fully_filled(sessions, market):
    ex = FakeExchange(crash_after_post=True)
    iid = place_order(
        sessions, LiveExecutor(ex), market=market, outcome="YES", side="BUY", price=0.2, size=10, reason="t"
    )
    ex.match("0xorder1", 10)  # filled and gone from the open list
    with sessions() as s:
        rep = reconcile(s, ex)
        s.commit()
        it = s.get(OrderIntent, iid)
        assert rep.adopted == [iid] and it.status == "FILLED" and it.filled_size == 10


def test_unknown_exchange_orders_are_cancelled(sessions, market):
    ex = FakeExchange()
    ex.orders["0xstranger"] = ExOrder("0xstranger", "tok", "BUY", 0.5, 5, 0, datetime.now(UTC))
    with sessions() as s:
        rep = reconcile(s, ex)
        s.commit()
    assert rep.cancelled_unknown == ["0xstranger"] and ex.cancelled == ["0xstranger"]


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(plan=st.lists(st.sampled_from(["ok", "crash_before", "crash_after", "fill"]), min_size=1, max_size=8))
def test_restart_reconciliation_never_duplicates(sessions, market, plan):
    """Random crash points across a sequence of orders + restarts: one exchange order per intent at most."""
    wipe(sessions)
    ex = FakeExchange()
    for step in plan:
        ex.crash_before_post = step == "crash_before"
        ex.crash_after_post = step == "crash_after"
        with contextlib.suppress(Crash):
            place_order(
                sessions, LiveExecutor(ex), market=market, outcome="YES", side="BUY", price=0.2, size=5, reason="prop"
            )
        if step == "fill" and ex.orders:
            ex.match(next(iter(ex.orders)), 5)
        ex.crash_before_post = ex.crash_after_post = False
        with sessions() as s:  # "restart"
            reconcile(s, ex, now=datetime.now(UTC) + timedelta(minutes=5))
            s.commit()
    with sessions() as s:
        intents = s.scalars(select(OrderIntent).where(OrderIntent.mode == "LIVE")).all()
        linked = [it.exchange_order_id for it in intents if it.exchange_order_id]
        assert len(linked) == len(set(linked))  # no exchange order adopted twice
        assert set(ex.posted) == set(linked)  # every exchange order is owned by exactly one intent
        assert len(ex.posted) <= len(intents)  # nothing was ever re-sent
        assert not ex.cancelled  # nothing we placed was mistaken for a stranger


def test_creds_round_trip_encrypted(sessions):
    with sessions() as s:
        save_creds(s, "secret-1", {"key": "k", "secret": "s", "passphrase": "p"})
        s.commit()
        assert load_creds(s, "secret-1") == {"key": "k", "secret": "s", "passphrase": "p"}
        assert load_creds(s, "wrong-secret") is None


# ------------------------------------------------------------------ PAPER executor
def test_paper_partial_then_no_double_consumption(sessions, market):
    books_ = {"tok10003y": book("tok10003y", [(0.1, 100)], [(0.15, 4), (0.30, 100)], ts="s1")}
    pe = PaperExecutor(lambda t: books_[t])
    iid = place_order(sessions, pe, market=market, outcome="YES", side="BUY", price=0.16, size=10, reason="t")
    with sessions() as s:
        it = s.get(OrderIntent, iid)
        assert it.status == "PARTIAL" and it.filled_size == 4
        assert pe.sync(s) == 0  # same snapshot: displayed liquidity already consumed
        books_["tok10003y"] = book("tok10003y", [(0.1, 100)], [(0.16, 50)], ts="s2")
        assert pe.sync(s) == 1
        s.commit()
        assert s.get(OrderIntent, iid).status == "FILLED"
        fills = s.scalars(select(Fill)).all()
        assert [f.price for f in fills] == [0.15, 0.16]
        pe.cancel_all(s)


def test_paper_book_failure_rests_order(sessions, market):
    def boom(t: str):
        raise RuntimeError("down")

    pe = PaperExecutor(boom)
    iid = place_order(sessions, pe, market=market, outcome="YES", side="BUY", price=0.16, size=10, reason="t")
    with sessions() as s:
        assert s.get(OrderIntent, iid).status == "OPEN"
        assert pe.sync(s) == 0
        assert pe.cancel_all(s) == 1


# ------------------------------------------------------------------ accounting invariants
@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    ops=st.lists(
        st.tuples(st.sampled_from(["buy", "sell"]), st.floats(0.01, 0.99), st.floats(0.01, 50), st.floats(0, 0.05)),
        min_size=1,
        max_size=30,
    ),
    won=st.booleans(),
)
def test_books_reconcile_exactly(sessions, market, ops, won):
    with sessions() as s:
        s.query(LedgerEntry).delete()
        s.query(Position).delete()
        books.ensure_deposit(s, "PAPER", 500)
        m = s.get(Market, market.id)
        pos = books.get_or_create_position(s, book="PAPER", market=m, outcome="YES")
        for kind, price, size, fee in ops:
            if kind == "buy":
                books.apply_buy(s, "PAPER", pos, price, round(size, 2), fee)
            elif pos.size > 0:
                books.apply_sell(s, "PAPER", pos, price, round(min(size, pos.size), 2), fee)
            if pos.status != "OPEN":
                pos = books.get_or_create_position(s, book="PAPER", market=m, outcome="YES")
        books.apply_resolution(s, "PAPER", pos, won)
        s.flush()
        entries = s.scalars(select(LedgerEntry).where(LedgerEntry.book == "PAPER")).all()
        cash = books.cash(s, "PAPER")
        assert cash == sum(e.amount_micros for e in entries)
        realized = sum(p.realized_micros for p in s.scalars(select(Position)))
        assert cash == books.to_micros(500) + realized  # all closed: cash = deposit + realised, exactly
        assert sum(e.pnl_micros for e in entries) == realized
        s.rollback()
