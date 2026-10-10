"""Station observations (the ground truth markets resolve on), from the Iowa Environmental Mesonet.

The reduction mirrors each market's resolution rule:
* WU   - every report in the station's local day (routine hourly METARs and specials);
* NOAA - routine hourly reports only.
The displayed value is a whole degree in the market's unit.
"""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from app.http import Http
from app.markets.parser import Station

REPORT_TYPES = {"WU": (3, 4), "NOAA": (3,)}  # IEM: 3 = routine, 4 = special


def round_half_up(x: float) -> int:
    return math.floor(x + 0.5)


def display_value(tmpf: float, unit: str) -> int:
    """Whole-degree display. IEM's tmpf is derived from the METAR (tenths °C in the US, whole °C elsewhere),
    so converting back recovers the reported °C exactly."""
    if unit == "F":
        return round_half_up(tmpf)
    return round_half_up((tmpf - 32.0) * 5.0 / 9.0)


@dataclass(frozen=True)
class Obs:
    local_time: datetime
    tmpf: float


def parse_iem_csv(text: str) -> list[Obs]:
    rows = csv.DictReader(io.StringIO("\n".join(ln for ln in text.splitlines() if not ln.startswith("#"))))
    out: list[Obs] = []
    for r in rows:
        v = (r.get("tmpf") or "").strip()
        if not v or v == "M":
            continue
        out.append(Obs(datetime.strptime(r["valid"].strip(), "%Y-%m-%d %H:%M"), float(v)))
    return out


def daily_extremes(obs: list[Obs], unit: str, variable: str, min_reports: int = 18) -> dict[date, int]:
    by_day: dict[date, list[int]] = {}
    for o in obs:
        by_day.setdefault(o.local_time.date(), []).append(display_value(o.tmpf, unit))
    return {d: (max(v) if variable == "max" else min(v)) for d, v in by_day.items() if len(v) >= min_reports}


class Iem:
    def __init__(self, base_url: str, http: Http | None = None):
        self.http = http or Http("iem", base_url, timeout=60)

    def observations(self, station: Station, start: date, end: date, source: str) -> list[Obs]:
        params: list[tuple[str, str | int]] = [
            ("station", station.iem_id),
            ("data", "tmpf"),
            ("tz", station.tz),
            ("format", "onlycomma"),
            ("latlon", "no"),
            ("elev", "no"),
            ("missing", "M"),
            ("trace", "T"),
            ("direct", "no"),
            ("year1", start.year),
            ("month1", start.month),
            ("day1", start.day),
        ]
        end_x = end + timedelta(days=1)
        params += [("year2", end_x.year), ("month2", end_x.month), ("day2", end_x.day)]
        params += [("report_type", t) for t in REPORT_TYPES[source]]
        text = self.http.request("GET", "/cgi-bin/request/asos.py", params=params)
        return parse_iem_csv(str(text))
