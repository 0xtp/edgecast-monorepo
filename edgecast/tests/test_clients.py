"""HTTP clients against mocked endpoints (documented response shapes)."""

from __future__ import annotations

import math
from datetime import date, datetime

import httpx
import pytest
import respx

from app.forecast.nws import Nws
from app.forecast.observations import Iem, daily_extremes, display_value, parse_iem_csv, round_half_up
from app.forecast.openmeteo import OpenMeteo, parse_hourly_members, reduce_daily
from app.http import API_HEALTH, Http, HttpError
from app.markets.parser import load_stations
from app.models import cpi_prob
from app.polymarket.clob import Clob, parse_book
from app.polymarket.data_api import DataApi
from app.polymarket.gamma import Gamma
from app.polymarket.geoblock import check_geoblock
from tests.fixtures.weather import nyc_event


@respx.mock
def test_gamma_weather_events_filters_and_dedupes():
    ev = nyc_event("1", date(2026, 10, 12))
    other = {"id": "2", "title": "Will BTC hit 200k?"}
    respx.get("https://gamma.test/events").mock(return_value=httpx.Response(200, json=[ev, other]))
    g = Gamma("https://gamma.test")
    out = g.weather_events(closed=False, max_pages=1)
    assert [e["id"] for e in out] == ["1"]


@respx.mock
def test_clob_books_fees_history():
    respx.get("https://clob.test/book").mock(
        return_value=httpx.Response(
            200,
            json={
                "asset_id": "t1",
                "bids": [{"price": "0.10", "size": "5"}, {"price": "0.12", "size": "3"}],
                "asks": [
                    {"price": "0.20", "size": "4"},
                    {"price": "0.15", "size": "0"},
                    {"price": "0.16", "size": "2"},
                ],
                "tick_size": "0.01",
                "min_order_size": "5",
                "timestamp": "123",
            },
        )
    )
    respx.post("https://clob.test/books").mock(
        return_value=httpx.Response(200, json=[{"asset_id": "t1", "bids": [], "asks": [{"price": "0.5", "size": "1"}]}])
    )
    respx.get("https://clob.test/clob-markets/0xc").mock(
        return_value=httpx.Response(
            200, json={"t": [{"t": "t1"}, {"t": "t2"}], "mts": 0.01, "nr": True, "fd": {"r": 0.25, "e": 1}, "mos": 5}
        )
    )
    respx.get("https://clob.test/clob-markets/0xnofee").mock(
        return_value=httpx.Response(200, json={"t": [{"t": "t1"}], "mts": 0.001})
    )
    respx.get("https://clob.test/prices-history").mock(
        return_value=httpx.Response(200, json={"history": [{"t": 1, "p": 0.2}, {"t": 2, "p": 0.25}]})
    )
    c = Clob("https://clob.test")
    ob = c.book("t1")
    assert ob.best_bid == 0.12 and ob.best_ask == 0.16 and ob.spread == pytest.approx(0.04)
    assert ob.ask_depth(0.16) == 2 and ob.ask_notional(0.2) == pytest.approx(0.16 * 2 + 0.2 * 4)
    assert ob.bid_depth(0.1) == 8 and ob.bid_notional(0.12) == pytest.approx(0.36)
    assert c.books(["t1"])["t1"].best_ask == 0.5
    info = c.market_info("0xc")
    assert info.fee.per_share(0.5) == pytest.approx(0.0625) and info.neg_risk and info.tokens == ("t1", "t2")
    assert c.market_info("0xnofee").fee is None
    assert c.prices_history("t1") == [(1, 0.2), (2, 0.25)]
    assert parse_book({"bids": [], "asks": []}).spread is None


@respx.mock
def test_geoblock_fail_safe():
    url = "https://geo.test/api/geoblock"
    respx.get(url).mock(
        side_effect=[
            httpx.Response(200, json={"blocked": False, "country": "IE"}),
            httpx.Response(200, json={"blocked": True, "country": "US"}),
            httpx.Response(200, json={"weird": 1}),
            httpx.Response(500, text="err"),
            httpx.Response(500, text="err"),
            httpx.Response(500, text="err"),
            httpx.Response(500, text="err"),
        ]
    )
    h = Http("geo", "", retries=0)
    assert check_geoblock(url, h).blocked is False
    assert check_geoblock(url, h).blocked is True
    assert check_geoblock(url, h).blocked is True  # unrecognised -> blocked
    assert check_geoblock(url, h).blocked is True  # error -> blocked


@respx.mock
def test_http_retries_and_health(monkeypatch):
    route = respx.get("https://x.test/a").mock(side_effect=[httpx.Response(503), httpx.Response(200, json={"ok": 1})])
    h = Http("xtest", "https://x.test", retries=2)
    import app.http as http_mod

    monkeypatch.setattr(http_mod.time, "sleep", lambda s: None)
    assert h.get("/a") == {"ok": 1} and route.call_count == 2
    assert "ok" in API_HEALTH["xtest"]
    respx.get("https://x.test/b").mock(return_value=httpx.Response(404, text="nope"))
    with pytest.raises(HttpError):
        h.get("/b")
    assert "error" in API_HEALTH["xtest"]


@respx.mock
def test_data_api_positions_paginates():
    respx.get("https://data.test/positions").mock(return_value=httpx.Response(200, json=[{"asset": "a"}]))
    assert DataApi("https://data.test").positions("0xabc") == [{"asset": "a"}]
    respx.get("https://data.test/trades").mock(return_value=httpx.Response(200, json=[]))
    assert DataApi("https://data.test").trades("0xabc") == []


@respx.mock
def test_openmeteo_ensemble_and_previous_runs():
    times = [f"2026-10-12T{h:02d}:00" for h in range(24)] + [f"2026-10-13T{h:02d}:00" for h in range(24)]
    temps = [60 + 10 * math.sin(math.pi * (h % 24) / 24) for h in range(48)]
    respx.get("https://ens.test/v1/ensemble").mock(
        return_value=httpx.Response(
            200,
            json={
                "hourly": {
                    "time": times,
                    "temperature_2m": temps,
                    "temperature_2m_member01": [t + 1 for t in temps],
                    "temperature_2m_member02": [None] * 48,
                }
            },
        )
    )
    respx.get("https://ens.test/data/ncep_gefs025/static/meta.json").mock(
        return_value=httpx.Response(200, json={"last_run_initialisation_time": 1760000000})
    )
    respx.get("https://prev.test/v1/forecast").mock(
        return_value=httpx.Response(
            200,
            json={
                "hourly": {
                    "time": times,
                    **{f"temperature_2m_previous_day{n}": [t + n for t in temps] for n in range(1, 8)},
                }
            },
        )
    )
    om = OpenMeteo(
        "https://ens.test",
        "https://prev.test",
        http=Http("om", "https://ens.test", retries=0),
        prev_http=Http("omp", "https://prev.test", retries=0),
    )
    fc = om.ensemble(40.7, -73.9, "America/New_York", "gfs", "F")
    assert fc.run_init is not None and len(fc.members) == 3
    daily = fc.daily(date(2026, 10, 12), "max")
    assert len(daily) == 2 and max(daily) == pytest.approx(71.0, abs=0.1)  # member with all None is dropped
    prev = om.previous_runs_daily(
        40.7, -73.9, "America/New_York", "gfs", "F", "min", date(2026, 10, 12), date(2026, 10, 13)
    )
    assert prev[3][date(2026, 10, 12)] == pytest.approx(63.0)


def test_member_parsing_and_reduce():
    t, m = parse_hourly_members(
        {"time": ["2026-01-01T00:00"], "temperature_2m": [1.0], "temperature_2m_member12": [2], "other": [3]}
    )
    assert sorted(m) == [0, 12] and t[0] == datetime(2026, 1, 1)
    assert reduce_daily([datetime(2026, 1, 1, h) for h in range(5)], [1, 2, 3, 4, 5], "max") == {}  # too few hours


def test_iem_parsing_and_display_rounding():
    csv = "#DEBUG\nstation,valid,tmpf\nLGA,2026-10-12 00:51,70.0\nLGA,2026-10-12 01:51,M\n" + "".join(
        f"LGA,2026-10-12 {h:02d}:51,{70 + h * 0.5}\n" for h in range(2, 24)
    )
    obs = parse_iem_csv(csv)
    assert len(obs) == 23
    ext = daily_extremes(obs, "F", "max")
    assert ext[date(2026, 10, 12)] == round_half_up(70 + 23 * 0.5)
    assert daily_extremes(obs, "C", "min")[date(2026, 10, 12)] == 21
    assert display_value(71.6, "F") == 72 and display_value(71.4, "F") == 71
    assert display_value(32.0, "C") == 0 and display_value(69.8, "C") == 21  # 21.0°C reported -> 69.8°F -> 21
    assert round_half_up(-0.5) == 0 and round_half_up(2.5) == 3


@respx.mock
def test_iem_request_shape():
    route = respx.get("https://iem.test/cgi-bin/request/asos.py").mock(
        return_value=httpx.Response(200, text="station,valid,tmpf\n")
    )
    st = load_stations()["KLGA"]
    Iem("https://iem.test", http=Http("iem", "https://iem.test", retries=0)).observations(
        st, date(2026, 1, 1), date(2026, 1, 31), "NOAA"
    )
    q = str(route.calls[0].request.url)
    assert "station=LGA" in q and "report_type=3" in q and "report_type=4" not in q


@respx.mock
def test_nws_high_low():
    respx.get("https://nws.test/points/40.7769,-73.874").mock(
        return_value=httpx.Response(
            200, json={"properties": {"forecast": "https://nws.test/gridpoints/OKX/1,1/forecast"}}
        )
    )
    respx.get("https://nws.test/gridpoints/OKX/1,1/forecast").mock(
        return_value=httpx.Response(
            200,
            json={
                "properties": {
                    "periods": [
                        {
                            "startTime": "2026-10-12T06:00:00-04:00",
                            "isDaytime": True,
                            "temperature": 72,
                            "temperatureUnit": "F",
                        },
                        {
                            "startTime": "2026-10-12T18:00:00-04:00",
                            "isDaytime": False,
                            "temperature": 15,
                            "temperatureUnit": "C",
                        },
                    ]
                }
            },
        )
    )
    out = Nws("https://nws.test", "ua").daily_high_low(40.7769, -73.874)
    assert out[date(2026, 10, 12)]["max"] == 72 and out[date(2026, 10, 13)]["min"] == pytest.approx(59.0)


def test_cpi_model():
    buckets = [(-math.inf, 0.1), (0.2, 0.2), (0.3, 0.3), (0.4, math.inf)]
    p = cpi_prob.bucket_probabilities(0.27, 5, buckets)
    assert abs(sum(p) - 1) < 1e-9 and p.index(max(p)) == 2
    m = cpi_prob.fit_errors([(3, 0.2, 0.25)] * 10 + [(30, 0.3, 0.1)] * 10)
    assert m.sigma(2) >= 0.03 and m.sigma(100) == m.sigma_by_days[40]
    assert cpi_prob._t_cdf(0.0, 5) == pytest.approx(0.5) and cpi_prob._t_cdf(10, 5) > 0.99
    assert cpi_prob._t_cdf(-math.inf, 5) == 0 and cpi_prob._betainc(1, 1, 0) == 0 and cpi_prob._betainc(1, 1, 1) == 1
