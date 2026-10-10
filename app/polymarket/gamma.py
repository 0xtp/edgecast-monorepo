"""Gamma API: market discovery and metadata (public, no auth)."""

from __future__ import annotations

from typing import Any

from app.http import Http
from app.markets.parser import TITLE_RE

WEATHER_TAGS = ("weather", "temperature", "climate")


class Gamma:
    def __init__(self, base_url: str, http: Http | None = None):
        self.http = http or Http("gamma", base_url)

    def events(self, *, tag_slug: str, closed: bool, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        data = self.http.get(
            "/events",
            tag_slug=tag_slug,
            closed=str(closed).lower(),
            limit=limit,
            offset=offset,
            order="endDate",
            ascending=str(not closed).lower(),
        )
        return list(data or [])

    def event(self, event_id: str) -> dict[str, Any]:
        return dict(self.http.get(f"/events/{event_id}"))

    def event_by_slug(self, slug: str) -> dict[str, Any] | None:
        data = self.http.get("/events", slug=slug)
        return data[0] if data else None

    def weather_events(self, *, closed: bool = False, max_pages: int = 10) -> list[dict[str, Any]]:
        """All temperature events across the weather-ish tags, de-duplicated by id."""
        seen: dict[str, dict[str, Any]] = {}
        for tag in WEATHER_TAGS:
            for page in range(max_pages):
                try:
                    batch = self.events(tag_slug=tag, closed=closed, offset=page * 100)
                except Exception:
                    break
                for ev in batch:
                    if TITLE_RE.match(ev.get("title") or ""):
                        seen[str(ev["id"])] = ev
                if len(batch) < 100:
                    break
        return list(seen.values())
