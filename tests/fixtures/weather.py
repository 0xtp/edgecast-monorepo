"""Synthetic payloads in the documented API shapes (see README.md in this folder)."""

from __future__ import annotations

import json
import math
import random
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.forecast.openmeteo import EnsembleForecast
from app.polymarket.clob import ClobMarketInfo, FeeInfo, Level, OrderBook

RULES = (
    "This market will resolve to the temperature range that contains the highest temperature recorded at the "
    "{station} in degrees {unit_word} on {d}. The resolution source for this market will be information from "
    "{source}, specifically the highest temperature recorded for all times on this day once information is "
    "finalized. The resolution source for this market measures temperatures to whole degrees {unit_word} "
    "(eg, 21°{u}). Thus, this is the level of precision that will be used when resolving the market."
)


def f_labels(lo: int, hi: int, width: int = 2) -> list[str]:
    labels = [f"{lo - 1}°F or below"]
    t = lo
    while t + width - 1 <= hi:
        labels.append(f"{t}-{t + width - 1}°F")
        t += width
    labels.append(f"{t}°F or higher")
    return labels


def c_labels(lo: int, hi: int) -> list[str]:
    return [f"{lo - 1}°C or below", *[f"{t}°C" for t in range(lo, hi + 1)], f"{hi + 1}°C or higher"]


def make_event(
    event_id: str,
    *,
    title: str,
    labels: list[str],
    station: str,
    icao: str,
    source_url: str,
    unit: str = "F",
    local_date: date,
    closed: bool = False,
    winner: int | None = None,
    fees_enabled: bool = True,
) -> dict:
    d = local_date
    unit_word = "Fahrenheit" if unit == "F" else "Celsius"
    source = "Wunderground" if "wunderground" in source_url else "NOAA"
    rules = RULES.format(station=station, unit_word=unit_word, d=d.strftime("%-d %b '%y"), source=source, u=unit)
    end = datetime.combine(d + timedelta(days=1), datetime.min.time(), UTC) + timedelta(hours=12)
    markets = []
    for i, lab in enumerate(labels):
        prices = ["0.5", "0.5"]
        if closed and winner is not None:
            prices = ["1", "0"] if i == winner else ["0", "1"]
        markets.append(
            {
                "id": f"{event_id}{i:02d}",
                "conditionId": f"0xcond{event_id}{i:02d}",
                "question": f"Will the highest temperature be {lab} on {d.strftime('%B %-d')}?",
                "groupItemTitle": lab,
                "slug": f"m-{event_id}-{i}",
                "clobTokenIds": json.dumps([f"tok{event_id}{i:02d}y", f"tok{event_id}{i:02d}n"]),
                "outcomes": json.dumps(["Yes", "No"]),
                "outcomePrices": json.dumps(prices),
                "negRisk": True,
                "orderPriceMinTickSize": 0.01,
                "orderMinSize": 5,
                "feesEnabled": fees_enabled,
                "active": not closed,
                "closed": closed,
                "acceptingOrders": not closed,
                "endDate": end.isoformat().replace("+00:00", "Z"),
                "description": rules,
                "resolutionSource": source_url,
            }
        )
    return {
        "id": event_id,
        "slug": title.lower().replace(" ", "-").replace("?", ""),
        "title": title,
        "description": rules,
        "resolutionSource": source_url,
        "endDate": end.isoformat().replace("+00:00", "Z"),
        "negRisk": True,
        "closed": closed,
        "markets": markets,
    }


def nyc_event(event_id: str, local_date: date, **kw) -> dict:
    return make_event(
        event_id,
        title=f"Highest temperature in NYC on {local_date.strftime('%B %-d')}?",
        labels=f_labels(70, 79),
        station="LaGuardia Airport Station",
        icao="KLGA",
        source_url="https://www.wunderground.com/history/daily/us/ny/new-york-city/KLGA",
        unit="F",
        local_date=local_date,
        **kw,
    )


def tomorrow_local(tz: str = "America/New_York") -> date:
    return (datetime.now(UTC).astimezone(ZoneInfo(tz)) + timedelta(days=1)).date()


def book(token: str, bids: list[tuple[float, float]], asks: list[tuple[float, float]], ts: str = "1") -> OrderBook:
    return OrderBook(
        token,
        [Level(p, s) for p, s in sorted(bids, reverse=True)],
        [Level(p, s) for p, s in sorted(asks)],
        0.01,
        5,
        True,
        ts,
    )


def ensemble(
    family: str, local_date: date, center: float, spread: float, n: int, unit: str = "F", seed: int = 0
) -> EnsembleForecast:
    rng = random.Random(seed)
    times = [datetime.combine(local_date, datetime.min.time()) + timedelta(hours=h) for h in range(-24, 48)]
    members = {}
    for k in range(n):
        peak = rng.gauss(center, spread)
        members[k] = [peak - 8 * (1 - math.cos(2 * math.pi * ((t.hour - 15) % 24) / 24)) / 2 for t in times]
    return EnsembleForecast(
        family, f"{family}_ens", unit, times, members, datetime.now(UTC) - timedelta(hours=6), datetime.now(UTC)
    )


class FakeGamma:
    def __init__(self, live: list[dict], closed: list[dict] | None = None):
        self.live, self.closed = live, closed or []

    def weather_events(self, *, closed: bool = False, max_pages: int = 10) -> list[dict]:
        return self.closed if closed else self.live


class FakeClob:
    def __init__(self, fee: FeeInfo | None = FeeInfo(0.0, 1.0)):
        self.book_map: dict[str, OrderBook] = {}
        self.fee = fee
        self.calls = 0

    def book(self, token_id: str) -> OrderBook:
        self.calls += 1
        return self.book_map.get(token_id) or OrderBook(token_id, [], [], 0.01, 5, True, "0")

    def books(self, token_ids: list[str]) -> dict[str, OrderBook]:
        return {t: self.book(t) for t in token_ids}

    def market_info(self, condition_id: str) -> ClobMarketInfo:
        return ClobMarketInfo(condition_id, (), 0.01, True, self.fee, 5)


class FakeOpenMeteo:
    def __init__(self, forecasts: dict[str, EnsembleForecast]):
        self.forecasts = forecasts

    def ensemble(self, lat, lon, tz, family, unit, forecast_days=5) -> EnsembleForecast:
        if family not in self.forecasts:
            raise RuntimeError("no data")
        return self.forecasts[family]

    def previous_runs_daily(self, *a, **k):
        return {}


class FakeNws:
    def daily_high_low(self, lat, lon):
        return {}


class FakeIem:
    def observations(self, *a, **k):
        return []


class FakeDataApi:
    def __init__(self, positions: list[dict] | None = None):
        self.rows = positions or []

    def positions(self, user: str) -> list[dict]:
        return self.rows
