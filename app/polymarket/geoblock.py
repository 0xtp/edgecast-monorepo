"""Geoblock check. Fail-safe: anything other than an explicit "not blocked" counts as blocked."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from app.http import Http


@dataclass(frozen=True)
class GeoStatus:
    blocked: bool
    detail: str
    checked_at: datetime


def check_geoblock(url: str, http: Http | None = None) -> GeoStatus:
    now = datetime.now(UTC)
    client = http or Http("geoblock", "")
    try:
        data = client.request("GET", url)
    except Exception as e:  # network failure: we cannot prove we are allowed
        return GeoStatus(True, f"check failed: {e}", now)
    if not isinstance(data, dict) or "blocked" not in data:
        return GeoStatus(True, f"unrecognised response: {str(data)[:120]}", now)
    where = ", ".join(str(data[k]) for k in ("country", "region") if data.get(k))
    return GeoStatus(bool(data["blocked"]), where or "ok", now)
