"""Backtest replay on a synthetic data source; checks no-lookahead, metrics and the honest small-sample verdict."""

from __future__ import annotations

import math
import random
from datetime import UTC, date, datetime, timedelta

import pytest

from app.markets.parser import EventSpec
from app.strategy.edge import FeeModel
from backtest import runner
from tests.fixtures.weather import nyc_event

START = date(2026, 3, 1)
DAYS = 40


class FakeSource:
    """Truth is a seasonal series; forecasts are truth + 1°F warm bias + noise, known per lead."""

    def __init__(self, seed: int = 1, leak_marker: float | None = None):
        rng = random.Random(seed)
        self.truth_series = {
            START - timedelta(days=400) + timedelta(d): round(75 + 6 * math.sin(d / 9)) for d in range(400 + DAYS)
        }
        self.fc = {
            n: {d: v + 1.0 + rng.gauss(0, 1.2 + 0.3 * n) for d, v in self.truth_series.items()} for n in range(1, 8)
        }
        if leak_marker is not None:  # a value that must never be used: only available at lead 1
            for d in self.fc[1]:
                self.fc[1][d] = leak_marker
        self.events = []
        for i in range(DAYS):
            d = START + timedelta(days=i)
            t = self.truth_series[d]
            winner = 0 if t <= 69 else min(6, 1 + (t - 70) // 2)
            self.events.append(nyc_event(f"{i:03d}", d, closed=True, winner=winner))
        self.rng = random.Random(seed + 1)

    def resolved_events(self, since: date):
        return self.events

    def price_history(self, token_id: str, start: datetime, end: datetime):
        # Naive market: flat 1/7 per bucket.
        return [(int((end - timedelta(hours=1)).timestamp()), round(1 / 7, 2))]

    def fee(self, condition_id: str):
        return FeeModel(0.25, 1.0)

    def previous_runs(self, spec: EventSpec, family: str, start: date, end: date):
        return {n: {d: v for d, v in s.items() if start <= d <= end} for n, s in self.fc.items()}

    def truth(self, spec: EventSpec, start: date, end: date):
        return {d: v for d, v in self.truth_series.items() if start <= d <= end}


def test_replay_produces_bets_metrics_and_report(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "REPORTS", tmp_path)
    res = runner.run(FakeSource(), START, runner.Params(leads=(36,)))
    s = res.summary()
    assert s["bets"] > 0 and s["events_parsed"] == DAYS
    assert 0 <= s["hit_rate"] <= 1 and s["model_brier"] is not None and s["market_brier"] is not None
    assert s["model_brier"] < s["market_brier"]  # informative forecasts beat a flat market
    assert "too small" in s["verdict"]  # 40 days < 60: must say so plainly
    assert all(b.stake <= 10 + 1e-9 for b in res.bets)
    per_city_day: dict[str, float] = {}
    for b in res.bets:
        per_city_day[b.local_date] = per_city_day.get(b.local_date, 0) + b.stake
    assert max(per_city_day.values()) <= 30 + 1e-6
    path = runner.save(res, "t.html")
    html = path.read_text()
    assert "Sample too small" in html and "Method &amp; limitations" in html and "Bets" in html
    assert (tmp_path / "t.json").exists()


def test_no_lookahead_lead_selection():
    # 36h before day end -> must use a run >= 42h old -> lead 2, never lead 1
    assert runner.lead_days_for(36) == 2
    assert runner.lead_days_for(18) == 1
    assert runner.lead_days_for(60) == 3
    res = runner.run(FakeSource(leak_marker=999.0), START, runner.Params(leads=(36, 48)))
    # If lead-1 values (999°F) leaked in, the top bucket ("80°F or higher", every 7th prediction) would get ~all
    # the mass on every decision.
    top_bucket = [p for i, (p, _, _, _) in enumerate(res.predictions) if i % 7 == 6]
    assert top_bucket and sum(top_bucket) / len(top_bucket) < 0.5


def test_bias_history_is_strictly_before_decision():
    """Corrupting every forecast after the first event's date must not change that event's predictions."""
    params = runner.Params(leads=(48,))
    clean = runner.run(FakeSource(), START, params).predictions[:7]
    src = FakeSource()
    for series in src.fc.values():
        for d in series:
            if d > START:
                series[d] += 50.0
    corrupted = runner.run(src, START, params).predictions[:7]
    assert clean == corrupted


def test_helpers():
    t = datetime(2026, 3, 1, tzinfo=UTC)
    hist = [(int((t - timedelta(hours=2)).timestamp()), 0.2), (int((t + timedelta(hours=1)).timestamp()), 0.9)]
    assert runner.price_at(hist, t) == 0.2 and runner.price_at([], t) is None
    assert runner.bootstrap_roi([]) is None
    empty = runner.Result(runner.Params(), [], [], {}, 0, 0, "", "")
    assert empty.summary()["verdict"].startswith("No bets")
    assert "Not enough" in runner._svg_curve([])


@pytest.mark.parametrize(
    ("bets", "days", "ci", "expect"),
    [
        (300, 90, (0.01, 0.2), "above zero"),
        (300, 90, (-0.2, -0.01), "below zero"),
        (300, 90, (-0.1, 0.1), "includes zero"),
    ],
)
def test_verdicts(bets, days, ci, expect):
    r = runner.Result(runner.Params(), [], [], {}, 0, 0, "", "")
    assert expect in r.verdict(bets, days, ci)
