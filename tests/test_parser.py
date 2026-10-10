"""Parser: every pattern in docs/market-anatomy.md, and every refusal path (-> UNPARSED)."""

from __future__ import annotations

import copy
import json
import math
from datetime import date
from pathlib import Path

import pytest

from app.markets.parser import (
    Bucket,
    ParseError,
    _icao_from_url,
    load_stations,
    normalise_city,
    parse_bucket,
    parse_event,
)
from tests.fixtures.weather import c_labels, f_labels, make_event, nyc_event

D = date(2026, 10, 12)


def london(**kw):
    return make_event(
        "2",
        title="Highest temperature in London on October 12?",
        labels=c_labels(14, 20),
        station="London City Airport Station",
        icao="EGLC",
        source_url="https://www.wunderground.com/history/daily/gb/london/EGLC",
        unit="C",
        local_date=D,
        **kw,
    )


# ---------------------------------------------------------------- bucket labels
@pytest.mark.parametrize(
    ("label", "lo", "hi", "unit"),
    [
        ("78-79°F", 78, 79, "F"),
        ("78 - 79 °F", 78, 79, "F"),
        ("78–79°F", 78, 79, "F"),
        ("76°F or higher", 76, None, "F"),
        ("76°F or above", 76, None, "F"),
        ("69°F or below", None, 69, "F"),
        ("69°F or lower", None, 69, "F"),
        ("21°C", 21, 21, "C"),
        ("-3°C", -3, -3, "C"),
        ("-3--1°C", -3, -1, "C"),
        ("≥ 30°C", 30, None, "C"),
        ("<= 10°F", None, 10, "F"),
        ("15°c or less", None, 15, "C"),
    ],
)
def test_bucket_labels(label, lo, hi, unit):
    b, u = parse_bucket(label)
    assert (b.lo, b.hi, u) == (lo, hi, unit)


@pytest.mark.parametrize("label", ["78-79", "hot", "78.5°F", "80-78°F", "78°K"])
def test_bad_bucket_labels(label):
    with pytest.raises(ParseError):
        parse_bucket(label)


def test_bucket_continuous_interval_and_contains():
    b = Bucket("78-79°F", 78, 79)
    assert b.continuous == (77.5, 79.5)
    assert b.contains(78) and b.contains(79) and not b.contains(80)
    assert Bucket("x", None, 69).continuous == (-math.inf, 69.5)
    assert Bucket("x", 76, None).continuous == (75.5, math.inf)


# ---------------------------------------------------------------- events
def test_nyc_fahrenheit_wunderground():
    spec = parse_event(nyc_event("1", D))
    assert spec.city == "nyc" and spec.station.icao == "KLGA" and spec.unit == "F"
    assert spec.variable == "max" and spec.source == "WU" and spec.local_date == D
    assert [(m.bucket.lo, m.bucket.hi) for m in spec.markets][:3] == [(None, 69), (70, 71), (72, 73)]
    assert spec.markets[-1].bucket.hi is None
    assert spec.markets[0].token_yes.endswith("y") and spec.markets[0].token_no.endswith("n")
    assert spec.station.tz == "America/New_York"


def test_london_celsius():
    spec = parse_event(london())
    assert spec.unit == "C" and spec.station.icao == "EGLC" and spec.city == "london"
    assert spec.markets[1].bucket.label == "14°C"


def test_lowest_temperature_paris_le_bourget():
    ev = make_event(
        "3",
        title="Lowest temperature in Paris on October 12?",
        labels=c_labels(5, 10),
        station="Paris-Le Bourget Airport Station",
        icao="LFPB",
        source_url="https://www.wunderground.com/history/daily/fr/bonneuil-en-france/LFPB",
        unit="C",
        local_date=D,
    )
    ev["description"] = ev["description"].replace("highest", "lowest")
    spec = parse_event(ev)
    assert spec.variable == "min" and spec.station.icao == "LFPB"


def test_noaa_source_houston():
    ev = make_event(
        "4",
        title="Highest temperature in Houston on October 9, 2026?",
        labels=f_labels(80, 89),
        station="William P. Hobby Airport Station",
        icao="KHOU",
        source_url="https://www.weather.gov/wrh/timeseries?site=KHOU",
        unit="F",
        local_date=date(2026, 10, 9),
    )
    spec = parse_event(ev)
    assert spec.source == "NOAA" and spec.station.icao == "KHOU" and spec.local_date == date(2026, 10, 9)


def test_year_inferred_from_end_date_across_new_year():
    ev = nyc_event("5", date(2026, 12, 31))
    ev["endDate"] = "2027-01-01T12:00:00Z"
    ev["title"] = "Highest temperature in NYC on December 31?"
    assert parse_event(ev).local_date == date(2026, 12, 31)


def test_question_fallback_when_group_title_missing():
    ev = nyc_event("6", D)
    for m in ev["markets"]:
        m["question"] = f"Will the highest temperature in NYC be {m.pop('groupItemTitle')} on October 12?"
    spec = parse_event(ev)
    assert spec.markets[1].bucket.lo == 70


def test_resolved_market_detected():
    spec = parse_event(nyc_event("7", D, closed=True, winner=2))
    assert [m.resolved_yes for m in spec.markets][:3] == [False, False, True]


def test_markets_sorted_even_if_api_order_shuffled():
    ev = nyc_event("8", D)
    ev["markets"].reverse()
    spec = parse_event(ev)
    assert spec.markets[0].bucket.lo is None and spec.markets[-1].bucket.hi is None


# ---------------------------------------------------------------- refusals
def mutate(fn):
    ev = copy.deepcopy(nyc_event("9", D))
    fn(ev)
    return ev


@pytest.mark.parametrize(
    ("name", "fn"),
    [
        ("non-weather title", lambda e: e.update(title="Will it rain in NYC on October 12?")),
        ("unknown month", lambda e: e.update(title="Highest temperature in NYC on Smarch 12?")),
        (
            "unknown station",
            lambda e: (
                e.update(resolutionSource="https://www.wunderground.com/history/daily/ZZZZ"),
                [m.update(resolutionSource="https://www.wunderground.com/history/daily/ZZZZ") for m in e["markets"]],
            ),
        ),
        (
            "unknown source",
            lambda e: (
                e.update(
                    resolutionSource="https://example.com/KLGA",
                    description=e["description"].replace("Wunderground", "a website"),
                ),
                [m.update(resolutionSource="", description="") for m in e["markets"]],
            ),
        ),
        (
            "station named differently",
            lambda e: e.update(description=e["description"].replace("LaGuardia", "JFK Kennedy")),
        ),
        ("city mismatch", lambda e: e.update(title="Highest temperature in Chicago on October 12?")),
        ("gap", lambda e: e["markets"].pop(2)),
        ("overlap", lambda e: e["markets"][2].update(groupItemTitle="71-73°F")),
        ("no low tail", lambda e: e["markets"].pop(0)),
        ("no high tail", lambda e: e["markets"].pop()),
        ("mixed units", lambda e: e["markets"][2].update(groupItemTitle="22-23°C")),
        (
            "unit disagrees with rules",
            lambda e: e.update(description=e["description"].replace("Fahrenheit", "Celsius")),
        ),
        ("tenths precision", lambda e: e.update(description=e["description"] + " Measured to tenths of a degree.")),
        ("not yes/no", lambda e: e["markets"][0].update(outcomes=json.dumps(["Up", "Down"]))),
        ("missing tokens", lambda e: e["markets"][0].update(clobTokenIds="[]")),
        ("no condition id", lambda e: e["markets"][0].update(conditionId="")),
        ("one market", lambda e: e.update(markets=e["markets"][:1])),
        ("no rules", lambda e: (e.update(description=""), [m.update(description="") for m in e["markets"]])),
        ("bad json", lambda e: e["markets"][0].update(clobTokenIds="[oops")),
        ("invalid date", lambda e: e.update(title="Highest temperature in NYC on February 30, 2026?")),
        ("no year and no end", lambda e: e.update(endDate=None)),
    ],
)
def test_refusals(name, fn):
    with pytest.raises(ParseError):
        parse_event(mutate(fn))


# ---------------------------------------------------------------- helpers
def test_icao_from_urls():
    assert _icao_from_url("https://www.wunderground.com/history/daily/us/ny/new-york-city/KLGA") == "KLGA"
    assert (
        _icao_from_url("https://www.wunderground.com/history/daily/us/ny/new-york-city/KLGA/date/2026-7-27") == "KLGA"
    )
    assert _icao_from_url("https://www.weather.gov/wrh/timeseries?site=KLGA") == "KLGA"
    assert _icao_from_url("https://api.weather.gov/stations/KHOU/observations") == "KHOU"
    assert _icao_from_url("https://example.com/KLGA") is None


def test_city_aliases():
    assert normalise_city("New York City") == "nyc"
    assert normalise_city("NYC") == "nyc"
    assert normalise_city("Washington, D.C.") in ("washington dc", "washington, d.c.")
    assert normalise_city("Atlantis") == "atlantis"


def test_station_table_is_consistent():
    stations = load_stations()
    assert len(stations) >= 40
    for icao, st in stations.items():
        assert len(icao) == 4 and -90 <= st.lat <= 90 and -180 <= st.lon <= 180
        assert st.city in st.city_aliases or st.city
        if st.country == "US":
            assert st.iem_id == icao[1:]


# ---------------------------------------------------------------- recorded fixtures (when captured)
RECORDED = Path(__file__).parent / "fixtures" / "gamma"


@pytest.mark.skipif(not RECORDED.exists(), reason="no recorded Gamma fixtures (see tests/fixtures/README.md)")
def test_recorded_events_parse_or_refuse_cleanly():
    for f in RECORDED.glob("*.json"):
        for ev in json.loads(f.read_text()):
            try:
                spec = parse_event(ev)
            except ParseError:
                continue
            assert spec.markets and spec.unit in ("F", "C")
