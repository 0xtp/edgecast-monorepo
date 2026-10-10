"""NWS point forecast (US stations only) - an independent sanity check on the ensemble."""

from __future__ import annotations

from datetime import date, datetime

from app.http import Http


class Nws:
    def __init__(self, base_url: str, user_agent: str, http: Http | None = None):
        self.http = http or Http("nws", base_url, headers={"User-Agent": user_agent, "Accept": "application/geo+json"})
        self._points: dict[tuple[float, float], str] = {}

    def daily_high_low(self, lat: float, lon: float) -> dict[date, dict[str, float]]:
        """{local date: {"max": °F, "min": °F}} from the 7-day period forecast."""
        key = (round(lat, 4), round(lon, 4))
        if key not in self._points:
            pt = self.http.get(f"/points/{key[0]},{key[1]}")
            self._points[key] = pt["properties"]["forecast"]
        fc = self.http.request("GET", self._points[key])
        out: dict[date, dict[str, float]] = {}
        for p in fc["properties"]["periods"]:
            start = datetime.fromisoformat(p["startTime"])
            temp = float(p["temperature"])
            if p.get("temperatureUnit") == "C":
                temp = temp * 9 / 5 + 32
            if p.get("isDaytime"):
                out.setdefault(start.date(), {})["max"] = temp
            else:
                # An overnight period starting in the evening describes the next morning's low.
                d = start.date() if start.hour < 12 else date.fromordinal(start.date().toordinal() + 1)
                out.setdefault(d, {})["min"] = temp
        return out
