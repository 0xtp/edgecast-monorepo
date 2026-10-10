"""Open-Meteo: live ensemble members, run metadata, and lead-specific previous-run history."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.http import Http

# Model family -> (ensemble model id, deterministic model id for the Previous Runs API).
FAMILIES: dict[str, tuple[str, str]] = {
    "ecmwf_ifs": ("ecmwf_ifs025_ensemble", "ecmwf_ifs025"),
    "gfs": ("ncep_gefs025", "gfs_seamless"),
    "ecmwf_aifs": ("ecmwf_aifs025_ensemble", "ecmwf_aifs025_single"),
    "icon": ("icon_seamless_eps", "icon_seamless"),
    "gem": ("gem_global_ensemble", "gem_seamless"),
}

MEMBER_RE = re.compile(r"^temperature_2m(?:_member(\d+))?$")
MIN_HOURS_PER_DAY = 20  # a local day needs at least this many hourly values to be reduced


@dataclass
class EnsembleForecast:
    family: str
    model: str
    unit: str
    times: list[datetime]  # local wall-clock (naive) hours
    members: dict[int, list[float | None]]  # member index (0 = control) -> hourly values
    run_init: datetime | None
    fetched_at: datetime

    def daily(self, local_date: date, variable: str) -> list[float]:
        """Reduce every member to its local-day max or min."""
        idx = [i for i, t in enumerate(self.times) if t.date() == local_date]
        out: list[float] = []
        for series in self.members.values():
            vals = [series[i] for i in idx if series[i] is not None]
            if len(vals) >= MIN_HOURS_PER_DAY:
                out.append(float(max(vals) if variable == "max" else min(vals)))  # type: ignore[type-var]
        return out


def unit_param(unit: str) -> str:
    return "fahrenheit" if unit == "F" else "celsius"


def parse_hourly_members(hourly: dict[str, Any]) -> tuple[list[datetime], dict[int, list[float | None]]]:
    times = [datetime.fromisoformat(t) for t in hourly.get("time", [])]
    members: dict[int, list[float | None]] = {}
    for key, values in hourly.items():
        m = MEMBER_RE.match(key)
        if not m:
            continue
        members[int(m.group(1) or 0)] = [None if v is None else float(v) for v in values]
    return times, members


def reduce_daily(times: Iterable[datetime], values: Iterable[float | None], variable: str) -> dict[date, float]:
    by_day: dict[date, list[float]] = {}
    for t, v in zip(times, values, strict=False):
        if v is not None:
            by_day.setdefault(t.date(), []).append(float(v))
    return {d: (max(vs) if variable == "max" else min(vs)) for d, vs in by_day.items() if len(vs) >= MIN_HOURS_PER_DAY}


class OpenMeteo:
    def __init__(
        self,
        ensemble_base: str,
        previous_runs_base: str,
        single_runs_base: str = "",
        http: Http | None = None,
        prev_http: Http | None = None,
        single_http: Http | None = None,
    ):
        self.http = http or Http("open_meteo", ensemble_base)
        self.prev_http = prev_http or Http("open_meteo_prev", previous_runs_base)
        self.single_http = single_http or (Http("open_meteo_single", single_runs_base) if single_runs_base else None)

    def run_init(self, model: str) -> datetime | None:
        """Last run initialisation time from the model's public metadata file, if available."""
        try:
            meta = self.http.get(f"/data/{model}/static/meta.json")
            ts = meta.get("last_run_initialisation_time")
            return datetime.fromtimestamp(int(ts), UTC) if ts else None
        except Exception:
            return None

    def ensemble(
        self, lat: float, lon: float, tz: str, family: str, unit: str, forecast_days: int = 5
    ) -> EnsembleForecast:
        model = FAMILIES[family][0]
        data = self.http.get(
            "/v1/ensemble",
            latitude=lat,
            longitude=lon,
            hourly="temperature_2m",
            models=model,
            timezone=tz,
            forecast_days=forecast_days,
            past_days=1,
            temperature_unit=unit_param(unit),
        )
        times, members = parse_hourly_members(data.get("hourly") or {})
        if not members:
            raise ValueError(f"no ensemble members returned for {model}")
        return EnsembleForecast(family, model, unit, times, members, self.run_init(model), datetime.now(UTC))

    def previous_runs_daily(
        self,
        lat: float,
        lon: float,
        tz: str,
        family: str,
        unit: str,
        variable: str,
        start: date,
        end: date,
        leads: range = range(1, 8),
    ) -> dict[int, dict[date, float]]:
        """Deterministic forecast reduced to local-day max/min, per lead day (1..7)."""
        model = FAMILIES[family][1]
        names = [f"temperature_2m_previous_day{n}" for n in leads]
        out: dict[int, dict[date, float]] = {}
        # The API limits request size; walk the range in 90-day chunks.
        cursor = start
        while cursor <= end:
            chunk_end = min(end, cursor + timedelta(days=89))
            data = self.prev_http.get(
                "/v1/forecast",
                latitude=lat,
                longitude=lon,
                hourly=",".join(names),
                models=model,
                timezone=tz,
                start_date=cursor.isoformat(),
                end_date=chunk_end.isoformat(),
                temperature_unit=unit_param(unit),
            )
            hourly = data.get("hourly") or {}
            times = [datetime.fromisoformat(t) for t in hourly.get("time", [])]
            for n, name in zip(leads, names, strict=True):
                out.setdefault(n, {}).update(reduce_daily(times, hourly.get(name) or [], variable))
            cursor = chunk_end + timedelta(days=1)
        return out

    def single_run(
        self, lat: float, lon: float, tz: str, model: str, unit: str, run: datetime, forecast_days: int = 4
    ) -> tuple[list[datetime], dict[int, list[float | None]]]:
        """A specific historical run (for leakage-free backtests). Ensemble support is unverified."""
        if self.single_http is None:
            raise RuntimeError("single-runs API not configured")
        data = self.single_http.get(
            "/v1/forecast",
            latitude=lat,
            longitude=lon,
            hourly="temperature_2m",
            models=model,
            timezone=tz,
            run=run.strftime("%Y-%m-%dT%H:%M"),
            forecast_days=forecast_days,
            temperature_unit=unit_param(unit),
        )
        return parse_hourly_members(data.get("hourly") or {})
