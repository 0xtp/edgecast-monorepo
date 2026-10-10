"""Any city Polymarket lists: unknown stations are resolved from official metadata, then traded like curated ones."""

from __future__ import annotations

import httpx
import pytest
import respx
from sqlalchemy import select

from app.db.models import ResolvedStation, WeatherEvent
from app.http import Http
from app.markets import parser, registry
from app.markets.parser import UnknownStation, parse_event
from app.markets.stations import StationLookupError, StationResolver, load_db_stations
from tests.fixtures.weather import c_labels, make_event, tomorrow_local

AWC = "https://awc.test"
OM = "https://om.test"


def cape_town():
    return make_event(
        "900",
        title=f"Highest temperature in Cape Town on {tomorrow_local().strftime('%B %-d')}?",
        labels=c_labels(20, 26),
        station="Cape Town International Airport Station",
        icao="FACT",
        source_url="https://www.wunderground.com/history/daily/za/cape-town/FACT",
        unit="C",
        local_date=tomorrow_local(),
    )


@pytest.fixture(autouse=True)
def _clean_registry():
    parser._RESOLVED.clear()
    yield
    parser._RESOLVED.clear()


def resolver() -> StationResolver:
    return StationResolver(AWC, OM, awc=Http("awc", AWC, retries=0), om=Http("omtz", OM, retries=0))


def mock_ok():
    respx.get(f"{AWC}/api/data/stationinfo").mock(
        return_value=httpx.Response(
            200, json=[{"icaoId": "FACT", "site": "Cape Town Intl", "lat": -33.965, "lon": 18.602, "country": "ZA"}]
        )
    )
    respx.get(f"{OM}/v1/ensemble").mock(return_value=httpx.Response(200, json={"timezone": "Africa/Johannesburg"}))


def test_unknown_station_raises_specific_error():
    with pytest.raises(UnknownStation) as e:
        parse_event(cape_town())
    assert e.value.icao == "FACT" and e.value.city == "cape town"


@respx.mock
def test_registry_resolves_and_parses(sessions):
    mock_ok()
    with sessions() as s:
        assert registry.sync(s, [cape_town()], resolver()) == (1, 0)
        s.commit()
        ev = s.scalar(select(WeatherEvent))
        assert ev.parse_status == "PARSED" and ev.icao == "FACT" and ev.city == "cape town" and ev.unit == "C"
        row = s.get(ResolvedStation, "FACT")
        assert row.tz == "Africa/Johannesburg" and row.country == "ZA"
    # A fresh process picks the station up from the DB
    parser._RESOLVED.clear()
    with sessions() as s:
        assert load_db_stations(s) == 1
    st = parser.load_stations()["FACT"]
    assert st.tz == "Africa/Johannesburg" and st.iem_id == "FACT" and not st.is_us


@respx.mock
def test_lookup_failure_leaves_market_unparsed(sessions):
    respx.get(f"{AWC}/api/data/stationinfo").mock(return_value=httpx.Response(200, json=[]))
    with sessions() as s:
        assert registry.sync(s, [cape_town()], resolver()) == (0, 1)
        ev = s.scalar(select(WeatherEvent))
        assert ev.parse_status == "UNPARSED" and "could not be resolved" in ev.parse_error


@respx.mock
def test_bad_timezone_and_codes_rejected(sessions):
    respx.get(f"{AWC}/api/data/stationinfo").mock(
        return_value=httpx.Response(
            200, json=[{"icaoId": "FACT", "site": "x", "lat": -33.9, "lon": 18.6, "country": "ZA"}]
        )
    )
    respx.get(f"{OM}/v1/ensemble").mock(return_value=httpx.Response(200, json={"timezone": "Mars/Olympus"}))
    with sessions() as s, pytest.raises(StationLookupError):
        resolver().resolve(s, "FACT", "cape town")
    with sessions() as s, pytest.raises(StationLookupError):
        resolver().resolve(s, "fact!", "x")


def test_without_resolver_unknown_station_is_unparsed(sessions):
    with sessions() as s:
        assert registry.sync(s, [cape_town()]) == (0, 1)


@respx.mock
def test_us_station_resolved_uses_iem_three_letter_id(sessions):
    respx.get(f"{AWC}/api/data/stationinfo").mock(
        return_value=httpx.Response(
            200, json=[{"icaoId": "KMCI", "site": "Kansas City Intl", "lat": 39.297, "lon": -94.731, "country": "US"}]
        )
    )
    respx.get(f"{OM}/v1/ensemble").mock(return_value=httpx.Response(200, json={"timezone": "America/Chicago"}))
    with sessions() as s:
        resolver().resolve(s, "KMCI", "kansas city")
    st = parser.load_stations()["KMCI"]
    assert st.iem_id == "MCI" and st.is_us


def test_curated_stations_take_precedence():
    from app.markets.parser import Station

    parser.register_stations({"KLGA": Station("KLGA", "fake", (), "nyc", ("nyc",), 0, 0, "UTC", "US", "LGA")})
    assert parser.load_stations()["KLGA"].tz == "America/New_York"
