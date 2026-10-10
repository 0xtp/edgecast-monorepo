"""Resolve stations that are not in the curated table, so any city Polymarket lists can be traded.

Coordinates and name come from the Aviation Weather Center station metadata (official NOAA/NWS source,
covers worldwide ICAO stations); the time zone comes from Open-Meteo's `timezone=auto` lookup at those
coordinates. Results are stored in the DB (`resolved_station`) and shown on the Model page.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ResolvedStation
from app.http import Http
from app.markets.parser import Station, register_stations

log = logging.getLogger(__name__)


class StationLookupError(RuntimeError):
    pass


def to_station(row: ResolvedStation) -> Station:
    us = row.country.upper() == "US"
    return Station(
        icao=row.icao,
        name=row.name,
        aliases=(row.name.lower(),) if row.name else (),
        city=row.city,
        city_aliases=(row.city,),
        lat=row.lat,
        lon=row.lon,
        tz=row.tz,
        country=row.country.upper(),
        iem_id=row.icao[1:] if us and row.icao.startswith("K") else row.icao,
    )


def load_db_stations(session: Session) -> int:
    rows = session.scalars(select(ResolvedStation)).all()
    register_stations({r.icao: to_station(r) for r in rows})
    return len(rows)


class StationResolver:
    def __init__(self, awc_base: str, open_meteo_base: str, awc: Http | None = None, om: Http | None = None):
        self.awc = awc or Http("aviationweather", awc_base)
        self.om = om or Http("open_meteo_tz", open_meteo_base)

    def lookup(self, icao: str) -> dict[str, Any]:
        data = self.awc.get("/api/data/stationinfo", ids=icao, format="json")
        rows = [r for r in (data or []) if str(r.get("icaoId", "")).upper() == icao]
        if not rows:
            raise StationLookupError(f"{icao} not found in Aviation Weather Center station metadata")
        r = rows[0]
        lat, lon = float(r["lat"]), float(r["lon"])
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise StationLookupError(f"{icao}: implausible coordinates {lat},{lon}")
        return {"name": str(r.get("site") or icao), "lat": lat, "lon": lon, "country": str(r.get("country") or "")}

    def timezone(self, lat: float, lon: float) -> str:
        data = self.om.get(
            "/v1/ensemble",
            latitude=lat,
            longitude=lon,
            hourly="temperature_2m",
            models="ncep_gefs025",
            timezone="auto",
            forecast_days=1,
        )
        tz = str((data or {}).get("timezone") or "")
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise StationLookupError(f"no valid time zone for {lat},{lon}: {tz!r}") from e
        return tz

    def resolve(self, session: Session, icao: str, city: str) -> ResolvedStation:
        if not re.fullmatch(r"[A-Z]{4}", icao):
            raise StationLookupError(f"not an ICAO code: {icao!r}")
        meta = self.lookup(icao)
        tz = self.timezone(meta["lat"], meta["lon"])
        row = session.get(ResolvedStation, icao) or ResolvedStation(icao=icao)
        row.name, row.city, row.lat, row.lon = meta["name"], city, meta["lat"], meta["lon"]
        row.tz, row.country = tz, meta["country"]
        session.merge(row)
        session.flush()
        register_stations({icao: to_station(row)})
        log.info("resolved new station %s (%s, %s) at %.4f,%.4f tz=%s", icao, row.name, city, row.lat, row.lon, tz)
        return row
