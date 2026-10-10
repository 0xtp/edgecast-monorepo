"""Turn a Gamma weather event into a structured, tradeable spec, or refuse.

Rules of the game (see docs/market-anatomy.md):
* one event per city/date/variable, one binary market per temperature bucket;
* buckets are whole degrees in the unit the market states, open-ended at both tails;
* the station is named in the rules and its ICAO appears in the resolution-source URL;
* anything ambiguous raises `ParseError` and the event is stored as UNPARSED (never traded).
"""

from __future__ import annotations

import itertools
import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import yaml

STATIONS_PATH = Path(__file__).with_name("stations.yaml")

MONTHS = {
    m: i
    for i, names in enumerate(
        [
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ],
        start=1,
    )
    for m in names
}

TITLE_RE = re.compile(
    r"^\s*(?P<var>highest|lowest|high|low)\s+temp(?:erature)?\s+in\s+(?P<city>.+?)\s+on\s+"
    r"(?P<month>[A-Za-z]+)\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(?P<year>\d{4}))?\s*\??\s*$",
    re.I,
)
NUM = r"(-?\d+)"
DEG = r"\s*°\s*(?P<unit>[FC])"
BUCKET_RES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(rf"^{NUM}\s*(?:-|–|—|to)\s*{NUM}{DEG}$", re.I), "range"),
    (re.compile(rf"^{NUM}{DEG}\s+or\s+(?:higher|above|more|greater)$", re.I), "ge"),
    (re.compile(rf"^(?:≥|>=)\s*{NUM}{DEG}$", re.I), "ge"),
    (re.compile(rf"^{NUM}{DEG}\s+or\s+(?:lower|below|less|under)$", re.I), "le"),
    (re.compile(rf"^(?:≤|<=)\s*{NUM}{DEG}$", re.I), "le"),
    (re.compile(rf"^{NUM}{DEG}$", re.I), "exact"),
]
QUESTION_BUCKET_RE = re.compile(r"\bbe\s+(?:between\s+)?(?P<label>.+?)\s+on\s+[A-Za-z]+\.?\s+\d", re.I)
UNIT_WORD_RE = re.compile(r"degrees\s+(fahrenheit|celsius)", re.I)
DECIMAL_PRECISION_RE = re.compile(r"\b(tenths?|decimal|one decimal|0\.1)\b", re.I)
URL_RE = re.compile(r"https?://[^\s)\"'>]+")


class ParseError(ValueError):
    """The market cannot be parsed with high confidence."""


class UnknownStation(ParseError):
    """The resolution station is not known yet; the registry may resolve it and retry."""

    def __init__(self, icao: str, city: str):
        super().__init__(f"station {icao} is not known (curated or resolved)")
        self.icao, self.city = icao, city


@dataclass(frozen=True)
class Station:
    icao: str
    name: str
    aliases: tuple[str, ...]
    city: str
    city_aliases: tuple[str, ...]
    lat: float
    lon: float
    tz: str
    country: str
    iem_id: str

    @property
    def is_us(self) -> bool:
        return self.country == "US"


@dataclass(frozen=True)
class Bucket:
    """Inclusive integer range in the market unit. None means unbounded."""

    label: str
    lo: int | None
    hi: int | None

    def contains(self, value: int) -> bool:
        return (self.lo is None or value >= self.lo) and (self.hi is None or value <= self.hi)

    @property
    def continuous(self) -> tuple[float, float]:
        """Half-open interval on the latent continuous temperature that rounds into this bucket."""
        lo = -math.inf if self.lo is None else self.lo - 0.5
        hi = math.inf if self.hi is None else self.hi + 0.5
        return lo, hi


@dataclass
class MarketSpec:
    condition_id: str
    gamma_id: str
    slug: str
    question: str
    bucket: Bucket
    token_yes: str
    token_no: str
    neg_risk: bool
    tick_size: float
    min_order_size: float
    fees_enabled: bool | None
    active: bool
    closed: bool
    accepting_orders: bool
    end_date: datetime | None
    resolved_yes: bool | None


@dataclass
class EventSpec:
    event_id: str
    slug: str
    title: str
    city: str
    station: Station
    local_date: date
    variable: str  # "max" | "min"
    unit: str  # "F" | "C"
    source: str  # "WU" | "NOAA"
    source_url: str
    precision: int
    end_date: datetime | None
    closed: bool
    markets: list[MarketSpec] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["local_date"] = self.local_date.isoformat()
        d["end_date"] = self.end_date.isoformat() if self.end_date else None
        for m in d["markets"]:
            m["end_date"] = m["end_date"].isoformat() if m["end_date"] else None
        return d


# --------------------------------------------------------------------------- stations
_RESOLVED: dict[str, Station] = {}


def register_stations(stations: dict[str, Station]) -> None:
    """Add stations resolved at runtime (from the DB). Curated entries always take precedence."""
    _RESOLVED.update(stations)


def load_stations() -> dict[str, Station]:
    return {**_RESOLVED, **curated_stations()}


@lru_cache(maxsize=1)
def curated_stations(path: Path = STATIONS_PATH) -> dict[str, Station]:
    raw = yaml.safe_load(path.read_text())
    return {
        icao: Station(
            icao=icao,
            name=v["name"],
            aliases=tuple(a.lower() for a in v["aliases"]),
            city=v["city"],
            city_aliases=tuple(a.lower() for a in v["city_aliases"]),
            lat=float(v["lat"]),
            lon=float(v["lon"]),
            tz=v["tz"],
            country=v["country"],
            iem_id=v["iem_id"],
        )
        for icao, v in raw.items()
    }


def normalise_city(name: str) -> str:
    n = re.sub(r"\s+", " ", name.strip().lower().rstrip("?").strip())
    for st in load_stations().values():
        if n == st.city or n in st.city_aliases:
            return st.city
    return n


# --------------------------------------------------------------------------- helpers
def parse_bucket(label: str) -> tuple[Bucket, str]:
    text = re.sub(r"\s+", " ", label.strip())
    for rx, kind in BUCKET_RES:
        m = rx.match(text)
        if not m:
            continue
        unit = m.group("unit").upper()
        nums = [int(g) for g in m.groups() if g is not None and re.fullmatch(r"-?\d+", g)]
        if kind == "range":
            lo, hi = nums
            if hi < lo:
                raise ParseError(f"inverted bucket {label!r}")
            return Bucket(text, lo, hi), unit
        if kind == "ge":
            return Bucket(text, nums[0], None), unit
        if kind == "le":
            return Bucket(text, None, nums[0]), unit
        return Bucket(text, nums[0], nums[0]), unit
    raise ParseError(f"unrecognised bucket label {label!r}")


def _jsonish(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as e:
            raise ParseError(f"bad JSON list {value!r}") from e
        return list(parsed)
    return list(value)


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    s = str(value).replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _icao_from_url(url: str) -> str | None:
    try:
        u = urlparse(url)
    except ValueError:
        return None
    host = u.netloc.lower()
    if "wunderground.com" in host:
        last = u.path.rstrip("/").split("/")[-1]
        # /history/daily/us/ny/new-york-city/KLGA[/date/2026-7-27]
        parts = [p for p in u.path.split("/") if p]
        for p in reversed(parts):
            if re.fullmatch(r"[A-Z]{4}", p):
                return p
        return last.upper() if re.fullmatch(r"[A-Za-z]{4}", last) else None
    if "weather.gov" in host or "noaa.gov" in host:
        qs = parse_qs(u.query)
        for key in ("site", "sid", "station", "stid"):
            if key in qs and re.fullmatch(r"[A-Za-z]{4}", qs[key][0]):
                return qs[key][0].upper()
        m = re.search(r"/stations?/([A-Z]{4})\b", u.path)
        if m:
            return m.group(1)
    return None


def _mentions(text: str, alias: str) -> bool:
    """Whole-word/phrase match ("national airport" must not match "international airport")."""
    return bool(alias) and re.search(rf"(?<![\w]){re.escape(alias)}(?![\w])", text) is not None


def _source_kind(url: str, rules: str) -> str:
    host = urlparse(url).netloc.lower() if url else ""
    if "wunderground" in host:
        return "WU"
    if "weather.gov" in host or "noaa.gov" in host:
        return "NOAA"
    low = rules.lower()
    if "wunderground" in low:
        return "WU"
    if "noaa" in low or "national weather service" in low:
        return "NOAA"
    raise ParseError("resolution source is neither Wunderground nor NOAA")


def _infer_year(month: int, day: int, explicit: str | None, end: datetime | None) -> int:
    if explicit:
        return int(explicit)
    if end is None:
        raise ParseError("no year in title and no endDate to infer it from")
    candidates = [end.year - 1, end.year, end.year + 1]
    best = min(
        candidates,
        key=lambda y: abs((datetime(y, month, day, tzinfo=UTC) - end).total_seconds()),
    )
    return best


# --------------------------------------------------------------------------- main entry
def parse_event(event: dict[str, Any]) -> EventSpec:
    """Parse a Gamma event (with nested `markets`). Raises ParseError on any ambiguity."""
    title = (event.get("title") or "").strip()
    tm = TITLE_RE.match(title)
    if not tm:
        raise ParseError(f"title does not match the weather pattern: {title!r}")
    variable = "max" if tm.group("var").lower().startswith("high") else "min"
    month = MONTHS.get(tm.group("month").lower())
    if month is None:
        raise ParseError(f"unknown month {tm.group('month')!r}")
    day = int(tm.group("day"))
    end_date = _dt(event.get("endDate"))
    year = _infer_year(month, day, tm.group("year"), end_date)
    try:
        local_date = date(year, month, day)
    except ValueError as e:
        raise ParseError(f"invalid date in title: {e}") from e

    raw_markets = event.get("markets") or []
    if len(raw_markets) < 2:
        raise ParseError("event has fewer than two bucket markets")
    rules = event.get("description") or raw_markets[0].get("description") or ""
    if not rules:
        raise ParseError("no rules text")
    if DECIMAL_PRECISION_RE.search(rules):
        raise ParseError("rules mention sub-degree precision")

    source_url = event.get("resolutionSource") or raw_markets[0].get("resolutionSource") or ""
    urls = [source_url, *URL_RE.findall(rules)]
    icaos = {i for i in (_icao_from_url(u) for u in urls if u) if i}
    if len(icaos) != 1:
        raise ParseError(f"could not identify exactly one station ICAO from the source (got {sorted(icaos)})")
    icao = icaos.pop()
    stations = load_stations()
    station = stations.get(icao)
    if station is None:
        raise UnknownStation(icao, normalise_city(tm.group("city")))
    rules_low = rules.lower()
    # The station named in the rules must be this one, and no other curated station may be named.
    named = {s.icao for s in stations.values() if any(_mentions(rules_low, a) for a in s.aliases)}
    if named and named != {icao}:
        raise ParseError(f"rules name station(s) {sorted(named)} but source points to {icao}")
    source_kind = _source_kind(source_url or next(iter(URL_RE.findall(rules)), ""), rules)

    city = normalise_city(tm.group("city"))
    if city != station.city:
        raise ParseError(f"title city {city!r} does not match station city {station.city!r}")

    unit_word = UNIT_WORD_RE.search(rules)
    rules_unit = unit_word.group(1)[0].upper() if unit_word else None

    markets: list[MarketSpec] = []
    units: set[str] = set()
    for m in raw_markets:
        label = (m.get("groupItemTitle") or "").strip()
        if not label:
            qm = QUESTION_BUCKET_RE.search(m.get("question") or "")
            if not qm:
                raise ParseError(f"no bucket label for market {m.get('id')}")
            label = qm.group("label")
        bucket, unit = parse_bucket(label)
        units.add(unit)
        tokens = _jsonish(m.get("clobTokenIds"))
        outcomes = [str(o).lower() for o in _jsonish(m.get("outcomes"))]
        if len(tokens) != 2 or outcomes != ["yes", "no"]:
            raise ParseError(f"market {m.get('id')} is not a YES/NO pair")
        prices = [float(p) for p in _jsonish(m.get("outcomePrices"))] if m.get("outcomePrices") else []
        closed = bool(m.get("closed"))
        resolved_yes = None
        if closed and len(prices) == 2 and max(prices) >= 0.99:
            resolved_yes = prices[0] >= 0.99
        markets.append(
            MarketSpec(
                condition_id=str(m.get("conditionId") or ""),
                gamma_id=str(m.get("id") or ""),
                slug=str(m.get("slug") or ""),
                question=str(m.get("question") or ""),
                bucket=bucket,
                token_yes=str(tokens[0]),
                token_no=str(tokens[1]),
                neg_risk=bool(m.get("negRisk", event.get("negRisk", False))),
                tick_size=float(m.get("orderPriceMinTickSize") or m.get("minimumTickSize") or 0.01),
                min_order_size=float(m.get("orderMinSize") or m.get("minimumOrderSize") or 5),
                fees_enabled=m.get("feesEnabled"),
                active=bool(m.get("active", True)),
                closed=closed,
                accepting_orders=bool(m.get("acceptingOrders", True)),
                end_date=_dt(m.get("endDate")) or end_date,
                resolved_yes=resolved_yes,
            )
        )
        if not markets[-1].condition_id:
            raise ParseError(f"market {m.get('id')} has no conditionId")

    if len(units) != 1:
        raise ParseError(f"mixed units across buckets: {sorted(units)}")
    unit = units.pop()
    if rules_unit and rules_unit != unit:
        raise ParseError(f"bucket unit °{unit} disagrees with rules (degrees {rules_unit})")

    _check_partition([mk.bucket for mk in markets])
    markets.sort(key=lambda mk: -math.inf if mk.bucket.lo is None else mk.bucket.lo)

    return EventSpec(
        event_id=str(event.get("id") or ""),
        slug=str(event.get("slug") or ""),
        title=title,
        city=city,
        station=station,
        local_date=local_date,
        variable=variable,
        unit=unit,
        source=source_kind,
        source_url=source_url,
        precision=1,
        end_date=end_date,
        closed=bool(event.get("closed")),
        markets=markets,
    )


def _check_partition(buckets: list[Bucket]) -> None:
    """Buckets must tile the integer line exactly: open low tail, contiguous, open high tail."""
    ordered = sorted(buckets, key=lambda b: -math.inf if b.lo is None else b.lo)
    if ordered[0].lo is not None:
        raise ParseError("no open-ended low tail bucket")
    if ordered[-1].hi is not None:
        raise ParseError("no open-ended high tail bucket")
    for prev, nxt in itertools.pairwise(ordered):
        if prev.hi is None or nxt.lo is None:
            raise ParseError("an open-ended bucket sits in the middle of the range")
        if nxt.lo != prev.hi + 1:
            raise ParseError(f"buckets {prev.label!r} and {nxt.label!r} leave a gap or overlap")
